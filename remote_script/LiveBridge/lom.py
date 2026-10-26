"""Helpers for reading the Live Object Model into small JSON-ready dicts.

Main thread only. Every helper returns plain data, never LOM objects.
"""

import math
import re

from .protocol import BridgeError

TRACK_TYPES = ("track", "return", "master")


def rnd(x, places=4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return x
    if math.isinf(x) or math.isnan(x):
        return None
    return round(x, places)


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def require_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise BridgeError("bad_args", "%s must be an integer, got %r." % (name, value))
    return value


def require_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeError("bad_args", "%s must be a number, got %r." % (name, value))
    return float(value)


def get_track(song, track_index=None, track_type="track"):
    if track_type == "master":
        return song.master_track
    if track_type == "return":
        tracks = list(song.return_tracks)
        label = "return track"
    elif track_type == "track":
        tracks = list(song.tracks)
        label = "track"
    else:
        raise BridgeError("bad_args", "track_type must be one of %s, got %r." % (", ".join(TRACK_TYPES), track_type))
    if track_index is None:
        raise BridgeError("bad_args", "track_index is required for %s." % label)
    require_int(track_index, "track_index")
    if not 0 <= track_index < len(tracks):
        raise BridgeError("not_found", "No %s %d. The set has %s." % (label, track_index, _count_phrase(len(tracks), label)))
    return tracks[track_index]


def get_clip_slot(track, clip_slot_index):
    require_int(clip_slot_index, "clip_slot_index")
    slots = list(track.clip_slots)
    if not slots:
        raise BridgeError("not_found", "Track '%s' has no clip slots." % track.name)
    if not 0 <= clip_slot_index < len(slots):
        raise BridgeError("not_found", "No clip slot %d on '%s'. It has %s (one per scene)." % (clip_slot_index, track.name, _count_phrase(len(slots), "slot")))
    return slots[clip_slot_index]


def get_clip(track, clip_slot_index):
    slot = get_clip_slot(track, clip_slot_index)
    if not slot.has_clip:
        raise BridgeError("not_found", "Clip slot %d on '%s' is empty." % (clip_slot_index, track.name))
    return slot.clip


def get_device(track, device_index):
    require_int(device_index, "device_index")
    devices = list(track.devices)
    if not 0 <= device_index < len(devices):
        names = ", ".join("%d %s" % (i, d.name) for i, d in enumerate(devices)) or "none"
        raise BridgeError("not_found", "No device %d on '%s'. Devices: %s." % (device_index, track.name, names))
    return devices[device_index]


def resolve_device(track, device_index=None, device_path=None):
    """A device by top-level index, or anywhere inside racks by path.

    A path alternates device and chain indices: [2] is track.devices[2], and
    [2, 0, 1] is track.devices[2].chains[0].devices[1]. Drum Rack pads are chains
    too. Returns (device, path); path is None for a top-level device_index.
    """
    if device_path is None:
        return get_device(track, 0 if device_index is None else device_index), None
    if device_index is not None:
        raise BridgeError("bad_args", "Pass device_index or device_path, not both.")
    if not isinstance(device_path, list) or len(device_path) % 2 == 0:
        raise BridgeError("bad_args", "device_path alternates device and chain indices and has an odd length, like [2] or [2, 0, 1]. Got %r." % (device_path,))
    for step in device_path:
        require_int(step, "each device_path entry")
    device = get_device(track, device_path[0])
    for depth in range(1, len(device_path), 2):
        chain_index, index = device_path[depth], device_path[depth + 1]
        if not safe(lambda: device.can_have_chains, False):
            raise BridgeError("not_found", "'%s' at device_path %s isn't a rack, so it has no chains." % (device.name, device_path[:depth]))
        chains = list(device.chains)
        if not 0 <= chain_index < len(chains):
            names = ", ".join("%d %s" % (i, c.name) for i, c in enumerate(chains)) or "none"
            raise BridgeError("not_found", "Rack '%s' has no chain %d. Chains: %s." % (device.name, chain_index, names))
        chain = chains[chain_index]
        devices = list(chain.devices)
        if not 0 <= index < len(devices):
            names = ", ".join("%d %s" % (i, d.name) for i, d in enumerate(devices)) or "none"
            raise BridgeError("not_found", "Chain '%s' in '%s' has no device %d. Devices: %s." % (chain.name, device.name, index, names))
        device = devices[index]
    return device, list(device_path)


def _count_phrase(n, label):
    if n == 0:
        return "no %ss" % label
    return "%d %s%s (0-%d)" % (n, label, "" if n == 1 else "s", n - 1)


def beats_per_bar(song):
    """Live's arrangement positions are in quarter-note beats: 4 per bar of 4/4, 3 per bar of 6/8."""
    return song.signature_numerator * 4.0 / song.signature_denominator


def color_hex(value):
    if value is None:
        return None
    return "#%06x" % (int(value) & 0xFFFFFF)


def enum_name(value):
    return str(value).rsplit(".", 1)[-1]


# ---- tracks -------------------------------------------------------------------------------------


def track_kind(track, track_type="track"):
    if track_type in ("return", "master"):
        return track_type
    if safe(lambda: track.is_foldable, False):
        return "group"
    if safe(lambda: track.has_midi_input, False):
        return "midi"
    return "audio"


def arm_state(track):
    """{"arm": ...} for tracks that can be armed, plus "implicit_arm" when an auto-arm control
    surface (Komplete Kontrol, Push) has armed it for being selected. Either way it records."""
    if not safe(lambda: track.can_be_armed, False):
        return {}
    out = {"arm": bool(safe(lambda: track.arm, False))}
    if safe(lambda: track.implicit_arm, False):
        out["implicit_arm"] = True
    return out


def track_brief(track, index, track_type="track"):
    """One line per track for overviews."""
    out = {"index": index, "name": track.name, "kind": track_kind(track, track_type)}
    if track_type != "master":
        out["mute"] = bool(safe(lambda: track.mute, False))
        out["solo"] = bool(safe(lambda: track.solo, False))
        out.update(arm_state(track))
    out["volume"] = param_display(track.mixer_device.volume)
    if safe(lambda: track.is_grouped, False):
        out["group"] = safe(lambda: track.group_track.name)
    out["devices"] = [d.name for d in track.devices]
    if track_type == "track":
        out["clips"] = sum(1 for s in track.clip_slots if s.has_clip)
        timeline = len(safe(lambda: track.arrangement_clips, ()) or ())
        if timeline:
            out["arrangement_clips"] = timeline
    return out


def meter_db(value):
    """A track meter reading (Track.output_meter_*) in dBFS.

    The meters are linear in dB, not amplitude: 0 is -70 dBFS (or quieter), 1 is +6 dBFS,
    and 0 dBFS reads 0.921. Measured in Live 12.4.7 with sines of known level.
    """
    if value <= 0.0:
        return float("-inf")
    return value * 76.0 - 70.0


def own_return_index(track, song):
    """A return track's own index. Live lists a send from a return to itself, but it does nothing."""
    return next((i for i, t in enumerate(song.return_tracks) if t == track), None)


def mixer_state(track, song, track_type="track"):
    mixer = track.mixer_device
    out = {"volume": param_state(mixer.volume), "pan": param_state(mixer.panning)}
    returns = list(song.return_tracks)
    own = own_return_index(track, song) if track_type == "return" else None
    sends = []
    for i, send in enumerate(mixer.sends):
        if i == own:
            continue
        entry = {"index": i, "return": returns[i].name if i < len(returns) else None}
        entry.update(param_state(send))
        sends.append(entry)
    if sends:
        out["sends"] = sends
    if track_type != "master":
        out["mute"] = bool(safe(lambda: track.mute, False))
        out["solo"] = bool(safe(lambda: track.solo, False))
    return out


def track_detail(track, index, song, track_type="track"):
    out = {"index": index, "type": track_type, "name": track.name, "kind": track_kind(track, track_type)}
    out["color"] = color_hex(safe(lambda: track.color))
    out.update(arm_state(track))
    if safe(lambda: track.is_grouped, False):
        out["group"] = safe(lambda: track.group_track.name)
    routing_in = safe(lambda: track.input_routing_type.display_name)
    routing_out = safe(lambda: track.output_routing_type.display_name)
    if routing_in is not None or routing_out is not None:
        out["routing"] = {"input": routing_in, "output": routing_out}
    out["mixer"] = mixer_state(track, song, track_type)
    out["devices"] = [device_brief(d, i) for i, d in enumerate(track.devices)]
    if track_type == "track":
        clips = []
        for i, slot in enumerate(track.clip_slots):
            if slot.has_clip:
                clips.append(clip_summary(slot.clip, i, notes=False))
        out["clip_slots"] = len(track.clip_slots)
        out["clips"] = clips
        out["playing_slot"] = _slot_or_none(safe(lambda: track.playing_slot_index, -1))
        out["fired_slot"] = _slot_or_none(safe(lambda: track.fired_slot_index, -1))
        timeline = list(safe(lambda: track.arrangement_clips, ()) or ())
        if timeline:
            bpb = beats_per_bar(song)
            out["arrangement"] = [arrangement_clip_brief(c, bpb, i) for i, c in enumerate(timeline[:ARRANGEMENT_LIMIT])]
            if len(timeline) > ARRANGEMENT_LIMIT:
                out["arrangement_clips"] = len(timeline)
    return out


ARRANGEMENT_LIMIT = 64


def arrangement_clip_brief(clip, bpb, index):
    """A clip on the timeline, positioned in 1-based bars (bar 1 = beat 0). `index` is its place in
    track.arrangement_clips, which set_clip and delete_clip take as arrangement_index."""
    return {
        "index": index,
        "name": clip.name,
        "start_bar": rnd(clip.start_time / bpb + 1, 3),
        "end_bar": rnd(clip.end_time / bpb + 1, 3),
    }


def _slot_or_none(index):
    return index if isinstance(index, int) and index >= 0 else None


def find_track_index(song, track):
    for i, t in enumerate(song.tracks):
        if t == track:
            return "track", i
    for i, t in enumerate(song.return_tracks):
        if t == track:
            return "return", i
    if track == song.master_track:
        return "master", None
    return None, None


# ---- clips --------------------------------------------------------------------------------------


def clip_summary(clip, slot_index, notes=True):
    out = {
        "slot": slot_index,
        "name": clip.name,
        "type": "midi" if clip.is_midi_clip else "audio",
        "length": rnd(clip.length),
        "looping": bool(safe(lambda: clip.looping, False)),
        "playing": bool(safe(lambda: clip.is_playing, False)),
    }
    if safe(lambda: clip.is_triggered, False):
        out["triggered"] = True
    if out["looping"]:
        out["loop"] = [rnd(safe(lambda: clip.loop_start, 0.0)), rnd(safe(lambda: clip.loop_end, 0.0))]
    if notes and clip.is_midi_clip:
        out["note_count"] = len(clip.get_notes_extended(0, 128, 0.0, max(float(clip.length), 0.0) + 1e6))
    if not clip.is_midi_clip:
        path = safe(lambda: clip.file_path)
        if path:
            out["file"] = path
    return out


# ---- devices and parameters ---------------------------------------------------------------------


CHAIN_LIMIT = 64  # chains listed per rack; a Drum Rack can fill all 128 pads
RACK_DEPTH = 4  # racks inside racks are listed this many levels deep

# Device settings Live keeps as properties instead of automatable parameters, by device
# class. Each maps to its options; the property's value is the option's index. The first
# property of a class is also shown in device overviews.
DEVICE_PROPERTIES = {
    "OriginalSimpler": {
        "playback_mode": ("Classic", "One-Shot", "Slicing"),
        "slicing_playback_mode": ("Mono", "Poly", "Thru"),
    },
}


def device_properties(device, headline_only=False):
    """{"playback_mode": "One-Shot"} for devices listed in DEVICE_PROPERTIES, else {}."""
    out = {}
    for name, options in DEVICE_PROPERTIES.get(safe(lambda: device.class_name), {}).items():
        value = safe(lambda: getattr(device, name))
        if isinstance(value, int) and 0 <= value < len(options):
            out[name] = options[value]
        if headline_only:
            break
    return out


def device_brief(device, index):
    """A top-level device. Racks list their chains and the devices inside them, each
    with the device_path that get_device_parameters and set_device_parameter take."""
    out = {
        "index": index,
        "name": device.name,
        "class": device.class_name,
        "type": enum_name(device.type),
        "active": bool(safe(lambda: device.is_active, True)),
        "parameters": len(device.parameters),
    }
    out.update(device_properties(device, headline_only=True))
    _add_chains(out, device, [index], 1)
    return out


def nested_device_brief(device, path, depth=1):
    out = {"path": list(path), "name": device.name, "class": device.class_name}
    if not safe(lambda: device.is_active, True):
        out["active"] = False
    out.update(device_properties(device, headline_only=True))
    _add_chains(out, device, path, depth + 1)
    return out


def _add_chains(out, device, path, depth):
    if not safe(lambda: device.can_have_chains, False):
        return
    chains = list(safe(lambda: device.chains, ()) or ())
    if depth > RACK_DEPTH or not chains:
        out["chain_count"] = len(chains)
        return
    listed = []
    for c, chain in enumerate(chains[:CHAIN_LIMIT]):
        entry = {"index": c, "name": chain.name}
        note = safe(lambda: chain.in_note)  # Drum Rack pads; -1 means "All Notes"
        if isinstance(note, int) and 0 <= note <= 127:
            entry["note"] = note_name(note)
        if safe(lambda: chain.mute, False):
            entry["mute"] = True
        entry["devices"] = [nested_device_brief(d, path + [c, i], depth) for i, d in enumerate(chain.devices)]
        listed.append(entry)
    out["chains"] = listed
    if len(chains) > CHAIN_LIMIT:
        out["chain_count"] = len(chains)


NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(pitch):
    """Live's octave numbering: 60 is C3, 36 is C1."""
    return "%s%d" % (NOTE_NAMES[pitch % 12], pitch // 12 - 2)


def param_display(param, value=None):
    if value is None:
        value = param.value
    text = safe(lambda: param.str_for_value(value))
    if text is None:
        text = safe(lambda: str(param), "")
    return text


def param_state(param):
    return {"value": rnd(param.value), "display": param_display(param)}


def param_detail(param, index):
    out = {
        "index": index,
        "name": param.name,
        "value": rnd(param.value),
        "min": rnd(param.min),
        "max": rnd(param.max),
        "display": param_display(param),
    }
    if param.is_quantized:
        out["quantized"] = True
        items = list(safe(lambda: param.value_items, ()) or ())
        if items and len(items) <= 64:
            out["items"] = [str(i) for i in items]
    else:
        steps = stepped_items(param)
        if steps and len(steps) <= 64:
            out["items"] = steps
    if not safe(lambda: param.is_enabled, True):
        out["enabled"] = False
    return out


_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def parse_db(text):
    """'-6.0 dB' -> -6.0, '-inf dB' -> -inf, anything without a number -> None."""
    if text is None:
        return None
    s = text.replace("−", "-").strip().lower()
    if "inf" in s:
        return float("-inf")
    m = _NUMBER.search(s)
    return float(m.group()) if m else None


def value_for_db(param, db):
    """Find the raw value whose display reads `db` by bisecting str_for_value.

    Live's fader curve isn't published, but the display is monotonic, so ~40
    str_for_value calls land on the exact value Live shows as that dB.
    """
    lo, hi = float(param.min), float(param.max)
    db_lo = parse_db(param_display(param, lo))
    db_hi = parse_db(param_display(param, hi))
    if db_hi is None or db_lo is None:
        raise BridgeError("bad_args", "'%s' doesn't display in dB, set a raw value instead." % param.name)
    if db > db_hi + 1e-6:
        raise BridgeError("bad_args", "%s dB is above the maximum of %s for '%s'." % (rnd(db, 2), param_display(param, hi), param.name))
    if db <= db_lo:
        return lo
    for _ in range(48):
        mid = (lo + hi) / 2.0
        d = parse_db(param_display(param, mid))
        if d is None:
            raise BridgeError("internal", "Unexpected display %r for '%s'." % (param_display(param, mid), param.name))
        if d < db:
            lo = mid
        else:
            hi = mid
    # Below the fader's lowest finite reading (around -70 dB) Live jumps to -inf.
    # Landing far above the target means it's in that gap: silence is closest.
    if parse_db(param_display(param, hi)) - db > 1.0:
        return float(param.min)
    return hi


def stepped_items(param, limit=128):
    """Display strings for a parameter that moves in whole steps but isn't marked quantized.

    Live reports tempo-synced rates (Auto Pan's "Rate", Delay's synced times) as continuous
    0-21 with displays like "1/4", so value_items is empty. A step parameter shows the same
    text halfway between two steps; a continuous one shows something in between.
    """
    lo, hi = float(param.min), float(param.max)
    if lo != int(lo) or hi != int(hi) or not 1 <= hi - lo <= limit:
        return None
    first, second = param_display(param, lo), param_display(param, lo + 1)
    if param_display(param, lo + 0.5) not in (first, second):
        return None
    return [param_display(param, float(v)) for v in range(int(lo), int(hi) + 1)]


# Unit -> (family, factor to the family's base unit), so "950 Hz" and "1.20 kHz" compare.
_UNITS = {
    "k": ("", 1000.0),  # Analog's filter shows "22.0k" above 1000 and a bare "400" below
    "hz": ("hz", 1.0),
    "khz": ("hz", 1000.0),
    "ms": ("ms", 1.0),
    "s": ("ms", 1000.0),
    "sec": ("ms", 1000.0),
    "db": ("db", 1.0),
    "%": ("%", 1.0),
    "st": ("st", 1.0),
    "ct": ("ct", 1.0),
}
_DISPLAY = re.compile(r"^([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*([a-z%°]*)")
_PAN = re.compile(r"^(\d+(?:\.\d+)?)\s*([lr])$")


def parse_display(text):
    """A display string as (number, unit family): '1.20 kHz' -> (1200.0, 'hz'), '2.5 s' -> (2500.0, 'ms'),
    '-inf dB' -> (-inf, 'db'), '25L' -> (-25.0, 'pan'), 'C' -> (0.0, 'pan'). None if there's no number."""
    if text is None:
        return None
    s = str(text).replace("−", "-").strip().lower()
    if s in ("c", "center", "centre"):
        return 0.0, "pan"
    m = _PAN.match(s)
    if m:
        v = float(m.group(1))
        return (-v if m.group(2) == "l" else v), "pan"
    if re.match(r"^[-+]?inf", s):
        unit = re.sub(r"^[-+]?inf\s*", "", s)
        return (float("-inf") if s.startswith("-") else float("inf")), _UNITS.get(unit, (unit, 1.0))[0]
    m = _DISPLAY.match(s)
    if not m:
        return None
    family, factor = _UNITS.get(m.group(2), (m.group(2), 1.0))
    return float(m.group(1)) * factor, family


def is_plain_value(text):
    """'+7 st', '120', '-6 dB': a number with at most a unit, nothing else ('1/4' isn't)."""
    return re.match(r"^\s*[-+]?(\d+(\.\d*)?|\.\d+)\s*[a-z%°]*\s*$", str(text).replace("−", "-").lower()) is not None


def value_for_display(param, text):
    """The raw value whose display reads `text` ("120 Hz", "1.2 kHz", "350 ms", "-12 dB", "40 %").

    Like value_for_db, this bisects str_for_value, so it works for any monotonic display without
    knowing Live's curves. A bare number takes the parameter's own unit.
    """
    target = parse_display(text)
    if target is None or math.isnan(target[0]):
        raise BridgeError("bad_args", "Can't read %r as a value for '%s'. Use what Live displays, like '120 Hz', '350 ms', '-6 dB' or '25 %%'." % (text, param.name))
    lo, hi = float(param.min), float(param.max)
    at_lo, at_hi = parse_display(param_display(param, lo)), parse_display(param_display(param, hi))
    if at_lo is None or at_hi is None or at_lo[1] != at_hi[1]:
        raise BridgeError("bad_args", "'%s' displays values like %r, which can't be targeted by text. Pass a raw number from %s to %s." % (param.name, param_display(param), rnd(lo), rnd(hi)))
    unit = at_lo[1]
    value, wanted_unit = target
    if wanted_unit and wanted_unit != unit:
        raise BridgeError("bad_args", "'%s' is shown like %r; %r is in different units." % (param.name, param_display(param, hi), text))
    rising = at_hi[0] >= at_lo[0]
    bottom, top = (at_lo[0], at_hi[0]) if rising else (at_hi[0], at_lo[0])
    slack = 0.005 * max(abs(bottom), abs(top), 1e-9) if not math.isinf(top - bottom) else 0.0
    if value < bottom - slack or value > top + slack:
        raise BridgeError("bad_args", "%r is outside '%s' (%s to %s)." % (text, param.name, param_display(param, lo), param_display(param, hi)))
    a, b = lo, hi
    for _ in range(60):
        mid = (a + b) / 2.0
        shown = parse_display(param_display(param, mid))
        if shown is None or shown[1] != unit:
            raise BridgeError("bad_args", "'%s' shows %r partway through its range, so it can't be targeted by text. Pass a raw number instead." % (param.name, param_display(param, mid)))
        if (shown[0] < value) == rising:
            a = mid
        else:
            b = mid

    def miss(raw):
        shown = parse_display(param_display(param, raw))[0]
        return 0.0 if shown == value else abs(shown - value)

    return min((a, b), key=miss)


def set_param_value(param, value, label=None):
    """Range-check and assign a raw value. Returns the new state."""
    label = label or param.name
    if not safe(lambda: param.is_enabled, True):
        raise BridgeError("invalid_state", "'%s' is disabled right now (it may be controlled by a macro or another parameter)." % label)
    lo, hi = float(param.min), float(param.max)
    if param.is_quantized:
        rounded = round(value)
        if abs(rounded - value) > 1e-6:
            raise BridgeError("bad_args", "'%s' takes whole steps from %s to %s, got %s." % (label, rnd(lo), rnd(hi), value))
        value = float(rounded)
    if value < lo - 1e-6 or value > hi + 1e-6:
        raise BridgeError("bad_args", "%s is out of range for '%s' (%s to %s)." % (rnd(value), label, rnd(lo), rnd(hi)))
    param.value = min(max(value, lo), hi)
    return param_state(param)
