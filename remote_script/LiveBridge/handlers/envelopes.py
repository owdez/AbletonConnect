"""Clip envelopes: automation inside Session clips (filter sweeps, volume rides, send throws).

What Live 12.4.7's API allows (verified in Live):
- A Session clip has one Envelope per parameter: Clip.automation_envelope(param) (None if there
  isn't one), create_automation_envelope(param), clear_envelope(param), clear_all_envelopes(),
  and Clip.automation_envelopes lists them. A new envelope has no events and follows the
  parameter's own value.
- Envelope.create_event(EnvelopeEvent(time, raw value)) adds a breakpoint. Live joins breakpoints
  with straight lines in raw values (quantized parameters step instead). Two events at one time
  make a vertical jump: value_at_time at that time gives the first, just after it the second.
  Before the first event and after the last the value holds. Curve coefficients are accepted but ignored, so curves are straight pieces.
- events_in_range(start, end) is inclusive at both ends and refuses ranges past +-1576800 beats.
  Its event values are in Live's internal units (Hz for a filter, linear gain for volume and
  sends), so values are always read back with value_at_time, which is raw.
- Times are beats from the clip's start, like note times. Values outside min-max raise.
- Arrangement clips can't get envelopes: automation_envelope returns None and
  create_automation_envelope raises "Not a session clip". arrange_scenes carries them over by
  duplicating a Session clip instead (see arrangement.py).
Before Live 12 an Envelope only had insert_step (a flat step) and value_at_time; there shapes are
drawn as small steps.
"""

import re

import Live

from ..dispatch import command
from ..lom import RACK_DEPTH, clip_summary, get_clip, get_track, param_display, parse_display, require_int, require_number, resolve_device, rnd, safe, value_for_db
from ..protocol import BridgeError
from .devices import _find_parameter, _value_for_text

CURVES = ("linear", "hold", "ease_in", "ease_out", "s_curve")
CURVE_PIECES = 16  # straight pieces per curved segment
MAX_BREAKPOINTS = 1000
MAX_STEPS = 512  # per call, when only insert_step exists
LIMIT = 1576800.0  # events_in_range's bounds, and Live's last beat
EPSILON = 1e-6


# ---- which parameter ----------------------------------------------------------------------------


def _target(song, track, parameter, device_index, device_path):
    """(param, description, kind) for a mixer control (no device_index or device_path) or a device
    parameter. kind is "fader" (volume and sends: dB text follows the fader curve), "pan" or None."""
    if device_index is None and device_path is None:
        mixer = track.mixer_device
        key = parameter.strip().lower() if isinstance(parameter, str) else ""
        if key in ("volume", "vol"):
            return mixer.volume, {"device": "Mixer", "parameter": "Volume"}, "fader"
        if key in ("pan", "panning"):
            return mixer.panning, {"device": "Mixer", "parameter": "Pan"}, "pan"
        m = re.match(r"^send\s*([a-l]|\d+)$", key)
        if not m:
            raise BridgeError("bad_args", "Without device_index or device_path, parameter is a mixer control: 'volume', 'pan', or a send like 'send 0' or 'send A'. Pass device_index (or device_path inside a rack) for a device parameter.")
        sends = list(mixer.sends)
        i = int(m.group(1)) if m.group(1).isdigit() else ord(m.group(1)) - ord("a")
        if not 0 <= i < len(sends):
            have = "no sends (the set has no return tracks)" if not sends else "sends 0-%d (A-%s)" % (len(sends) - 1, chr(64 + len(sends)))
            raise BridgeError("not_found", "No send %s on '%s'. It has %s." % (m.group(1).upper(), track.name, have))
        return sends[i], _send_about(song, i), "fader"
    device, path = resolve_device(track, device_index, device_path)
    index, param = _find_parameter(device, parameter)
    return param, _device_about(device, device_index if path is None else path, param, index), None


def _device_about(device, where, param, index):
    """How set_clip_envelope addresses a device parameter: device_index, or device_path in a rack."""
    about = {"device": device.name}
    about["device_index" if isinstance(where, int) else "device_path"] = where
    about.update(parameter=param.name, parameter_index=index)
    return about


def _send_about(song, i):
    about = {"device": "Mixer", "parameter": "Send %s" % chr(65 + i), "send": i}
    returns = list(song.return_tracks)
    if i < len(returns):
        about["return"] = returns[i].name
    return about


def _raw(param, value, kind, where):
    """A raw parameter value from a number or what Live displays ("-12 dB", "120 Hz", "25L", "Lowpass")."""
    if isinstance(value, str):
        shown = parse_display(value)
        try:
            if kind == "fader" and shown is not None and shown[1] == "db":
                return value_for_db(param, shown[0])
            if kind == "pan" and shown is not None and shown[1] == "pan" and abs(shown[0]) <= 50:
                return shown[0] / 50.0  # Live's pan reads 50L-C-50R, straight across -1 to 1
            return _value_for_text(param, value)
        except BridgeError as e:
            raise BridgeError(e.type, "%s: %s" % (where, e.message)) from None
    v = require_number(value, where)
    lo, hi = float(param.min), float(param.max)
    if v < lo - EPSILON or v > hi + EPSILON:
        raise BridgeError("bad_args", "%s: %s is outside '%s' (%s to %s). Pass a raw value in that range, or display text like '%s'." % (where, rnd(v), param.name, rnd(lo), rnd(hi), param_display(param, hi)))
    if param.is_quantized:
        v = float(round(v))
    return min(max(v, lo), hi)


# ---- shapes -------------------------------------------------------------------------------------


def _ease(curve, u):
    if curve == "ease_in":
        return u * u
    if curve == "ease_out":
        return 1.0 - (1.0 - u) * (1.0 - u)
    if curve == "s_curve":
        return u * u * (3.0 - 2.0 * u)
    return u


def _parse_shape(param, kind, points, ramp):
    """[(time, raw value, curve to the next point)] from points or a ramp."""
    if (points is None) == (ramp is None):
        raise BridgeError("bad_args", "Pass exactly one of points or ramp.")
    if ramp is not None:
        if not isinstance(ramp, dict) or not {"start", "end", "from", "to"} <= set(ramp) or set(ramp) - {"start", "end", "from", "to", "curve"}:
            raise BridgeError("bad_args", 'ramp must be {"start": beats, "end": beats, "from": value, "to": value, "curve": optional}.')
        points = [{"time": ramp["start"], "value": ramp["from"], "curve": ramp.get("curve", "linear")}, {"time": ramp["end"], "value": ramp["to"]}]
    if not isinstance(points, list) or not points:
        raise BridgeError("bad_args", 'points must be a non-empty list like [{"time": 0, "value": "200 Hz"}, {"time": 16, "value": "8 kHz"}].')
    shape = []
    for i, p in enumerate(points):
        if not isinstance(p, dict) or not {"time", "value"} <= set(p) or set(p) - {"time", "value", "curve"}:
            raise BridgeError("bad_args", 'Point %d must be {"time": beats, "value": number or text, "curve": optional}.' % i)
        t = require_number(p["time"], "time")
        if not 0 <= t <= LIMIT:
            raise BridgeError("bad_args", "Point %d: time must be 0-%d beats, got %s." % (i, LIMIT, rnd(t)))
        if shape and t <= shape[-1][0]:
            raise BridgeError("bad_args", "Point %d: times must increase, but %s comes after %s." % (i, rnd(t), rnd(shape[-1][0])))
        curve = p.get("curve", "linear")
        if curve not in CURVES:
            raise BridgeError("bad_args", "Point %d: curve must be one of %s, got %r." % (i, ", ".join(CURVES), curve))
        shape.append((t, _raw(param, p["value"], kind, "point %d" % i), curve))
    return shape


def _breakpoints(shape):
    """(time, value) events that Live's straight lines turn into `shape`. A hold ends in a
    vertical jump (two events at one time); a curve becomes CURVE_PIECES straight pieces."""
    out = []
    for (t0, v0, curve), (t1, v1, _) in zip(shape, shape[1:]):
        out.append((t0, v0))
        if curve == "hold":
            out.append((t1, v0))
        elif curve != "linear":
            out.extend((t0 + k * (t1 - t0) / CURVE_PIECES, v0 + _ease(curve, k / float(CURVE_PIECES)) * (v1 - v0)) for k in range(1, CURVE_PIECES))
    out.append(shape[-1][:2])
    return out


def _value_at(shape, t):
    for (t0, v0, curve), (t1, v1, _) in zip(shape, shape[1:]):
        if t0 <= t < t1:
            return v0 if curve == "hold" else v0 + _ease(curve, (t - t0) / (t1 - t0)) * (v1 - v0)
    return shape[-1][1] if t >= shape[-1][0] else shape[0][1]


def add_events(env, events):
    for t, v in events:
        env.create_event(Live.Envelope.EnvelopeEvent(t, v))


def _draw_breakpoints(env, shape, replace):
    events = _breakpoints(shape)
    if len(events) > MAX_BREAKPOINTS:
        raise BridgeError("bad_args", "That needs %d breakpoints (the limit is %d). Use fewer points or curves." % (len(events), MAX_BREAKPOINTS))
    first, last = shape[0][0], shape[-1][0]
    if replace:
        env.delete_events_in_range(-LIMIT, LIMIT)
    else:
        # Leave the envelope outside [first, last] as it was: jump into the new shape and back out.
        before, after = float(env.value_at_time(first - EPSILON)), float(env.value_at_time(last + EPSILON))
        env.delete_events_in_range(first, last)
        if first > 0 and abs(before - events[0][1]) > EPSILON:
            events.insert(0, (first, before))
        if abs(after - events[-1][1]) > EPSILON:
            events.append((last, after))
    add_events(env, events)


def _draw_steps(env, shape, replace, clip_end):
    """Approximate the shape with flat steps (insert_step), at least 1/16 beat long."""
    first, last = shape[0][0], shape[-1][0]
    size = max((last - first) / MAX_STEPS, 1.0 / 16)
    t = first
    while t < last - EPSILON:
        length = min(size, last - t)
        env.insert_step(t, length, _value_at(shape, t + length / 2.0))
        t += length
    if replace:
        if first > 0:
            env.insert_step(0.0, first, shape[0][1])
        env.insert_step(last, max(clip_end - last, 1.0 / 16), shape[-1][1])


# ---- reading ------------------------------------------------------------------------------------


def envelope_points(env, start, end):
    """(time, raw value) breakpoints from start to end (inclusive). At a jump (two or more events at
    one time, or any event of a quantized parameter, which steps) value_at_time gives the value
    reached at that time and the new value only just after it, so a jump becomes two points."""
    stepped = bool(safe(lambda: env.parameter.is_quantized, False))
    counts = {}
    for e in env.events_in_range(start, end):
        counts[float(e.time)] = counts.get(float(e.time), 0) + 1
    out = []
    for t in sorted(counts):
        value = float(env.value_at_time(t))
        out.append((t, value))
        if counts[t] > 1 or stepped:
            after = float(env.value_at_time(t + EPSILON))
            if abs(after - value) > EPSILON:
                out.append((t, after))
    return out


def _clip_points(env):
    """The envelope from the clip start on, starting with the value at time 0."""
    points = envelope_points(env, 0.0, LIMIT)
    if not points or points[0][0] > 0:
        points.insert(0, (0.0, float(env.value_at_time(0.0))))
    return points


def _point(param, t, v):
    return {"time": rnd(t), "value": rnd(v), "display": param_display(param, v)}


def _describe(clip, param, env, limit, resolution=None):
    """Breakpoints when Live lists them; otherwise values sampled every `resolution` beats."""
    if resolution is None and hasattr(env, "events_in_range"):
        points = _clip_points(env)
        out = {"points": [_point(param, t, v) for t, v in points[:limit]]}
        if len(points) > limit:
            out["truncated"] = True
            out["point_count"] = len(points)
        return out
    length = float(clip.length)
    step = require_number(resolution, "resolution") if resolution is not None else max(length / 64.0, 0.25)
    if step <= 0:
        raise BridgeError("bad_args", "resolution must be > 0 beats.")
    samples, k = [], 0
    while k * step <= length + EPSILON and len(samples) < limit:
        v = float(env.value_at_time(k * step))
        if not samples or abs(samples[-1][1] - v) > EPSILON:
            samples.append((k * step, v))
        k += 1
    return {"samples": [_point(param, t, v) for t, v in samples], "resolution": rnd(step)}


def _rack_devices(devices, path=(), depth=0):
    """(device, device_path) for every device inside racks, RACK_DEPTH levels deep."""
    for i, device in enumerate(devices):
        if depth:
            yield device, list(path) + [i]
        if depth < RACK_DEPTH and safe(lambda d=device: d.can_have_chains, False):
            for c, chain in enumerate(device.chains):
                yield from _rack_devices(list(chain.devices), tuple(path) + (i, c), depth + 1)


def _list_envelopes(song, track, clip):
    """Every parameter that has an envelope in this clip, addressed like set_clip_envelope takes it."""
    mixer = track.mixer_device
    known = [(mixer.volume, {"device": "Mixer", "parameter": "Volume"}), (mixer.panning, {"device": "Mixer", "parameter": "Pan"})]
    known += [(s, _send_about(song, i)) for i, s in enumerate(mixer.sends)]
    for d, device in enumerate(track.devices):
        known += [(p, _device_about(device, d, p, i)) for i, p in enumerate(device.parameters)]
    listed = safe(lambda: list(clip.automation_envelopes))
    if listed is None:  # no list in this Live: ask for each parameter
        listed = [env for env in (safe(lambda p=p: clip.automation_envelope(p)) for p, _ in known) if env is not None]
    out = []
    for env in listed:
        param = env.parameter
        about = next((dict(a) for p, a in known if p == param), None)
        if about is None:  # a device inside a rack: look there only when needed, racks can be big
            found = ((device, path, i, p) for device, path in _rack_devices(list(track.devices)) for i, p in enumerate(device.parameters) if p == param)
            hit = next(found, None)
            about = _device_about(hit[0], hit[1], param, hit[2]) if hit else {"device": safe(lambda: param.canonical_parent.name, "?"), "parameter": param.name}
        if hasattr(env, "events_in_range"):
            about["points"] = len(_clip_points(env))
        out.append(about)
    return out


def _session_clip(ctx, track_index, clip_slot_index):
    track = get_track(ctx.song, track_index, "track")
    return track, get_clip(track, clip_slot_index)


# ---- commands -----------------------------------------------------------------------------------


@command(undo=True)
def set_clip_envelope(ctx, track_index, clip_slot_index, parameter, device_index=None, device_path=None, points=None, ramp=None, replace=True):
    """Draw automation inside a Session clip for a mixer control or a device parameter."""
    song = ctx.song
    track, clip = _session_clip(ctx, track_index, clip_slot_index)
    param, about, kind = _target(song, track, parameter, device_index, device_path)
    shape = _parse_shape(param, kind, points, ramp)
    if replace and clip.automation_envelope(param) is not None:
        clip.clear_envelope(param)
    env = clip.automation_envelope(param) or clip.create_automation_envelope(param)
    clip_end = float(clip.loop_end) if safe(lambda: clip.looping, False) else float(clip.length)
    if hasattr(env, "create_event"):
        _draw_breakpoints(env, shape, replace)
    else:
        _draw_steps(env, shape, replace, clip_end)

    out = {"track": track.name, "clip": clip_summary(clip, clip_slot_index, notes=False), "envelope": about}
    about.update(_describe(clip, param, env, limit=32))
    if not hasattr(env, "create_event"):
        out["note"] = "This Live version's API has no envelope breakpoints, so the shape was drawn as small steps."
    if shape[-1][0] > clip_end + EPSILON:
        out["warning"] = "The envelope runs to beat %s but the clip ends at %s; the rest never plays." % (rnd(shape[-1][0]), rnd(clip_end))
    return out


@command()
def get_clip_envelope(ctx, track_index, clip_slot_index, parameter=None, device_index=None, device_path=None, resolution=None, limit=256):
    """One envelope's breakpoints, or every envelope in the clip when parameter is omitted."""
    song = ctx.song
    track, clip = _session_clip(ctx, track_index, clip_slot_index)
    out = {"track": track.name, "clip": clip_summary(clip, clip_slot_index, notes=False)}
    if parameter is None:
        if device_index is not None or device_path is not None:
            raise BridgeError("bad_args", "Pass parameter with a device, or neither to list the clip's envelopes.")
        out["envelopes"] = _list_envelopes(song, track, clip)
        return out
    param, about, _ = _target(song, track, parameter, device_index, device_path)
    env = clip.automation_envelope(param)
    out["envelope"] = about
    if env is None:
        about.update(exists=False, value=param_display(param))
    else:
        about.update(_describe(clip, param, env, require_int(limit, "limit"), resolution))
    return out


@command(undo=True)
def clear_clip_envelope(ctx, track_index, clip_slot_index, parameter=None, device_index=None, device_path=None):
    """Remove one envelope from a Session clip, or all of them when parameter is omitted."""
    song = ctx.song
    track, clip = _session_clip(ctx, track_index, clip_slot_index)
    if parameter is None:
        if device_index is not None or device_path is not None:
            raise BridgeError("bad_args", "Pass parameter with a device, or neither to clear every envelope.")
        cleared = _list_envelopes(song, track, clip)
        clip.clear_all_envelopes()
    else:
        param, about, _ = _target(song, track, parameter, device_index, device_path)
        if clip.automation_envelope(param) is None:
            raise BridgeError("not_found", "The clip has no envelope for %s." % (about["parameter"] if about["device"] == "Mixer" else "%s on %s" % (about["parameter"], about["device"])))
        cleared = [about]
        clip.clear_envelope(param)
    return {"track": track.name, "clip": clip_summary(clip, clip_slot_index, notes=False), "cleared": cleared, "envelopes_left": _list_envelopes(song, track, clip)}
