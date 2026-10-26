"""Devices on a track, including devices inside rack chains and Drum Rack pads."""

import difflib

from ..dispatch import command
from ..lom import (
    DEVICE_PROPERTIES,
    PLUGIN_FORMATS,
    device_brief,
    device_properties,
    get_track,
    is_plain_value,
    nested_device_brief,
    param_detail,
    param_display,
    parse_display,
    require_int,
    require_number,
    resolve_device,
    rnd,
    safe,
    set_param_value,
    stepped_items,
    value_for_display,
)
from ..protocol import BridgeError


@command()
def get_device_parameters(ctx, track_index=None, device_index=None, device_path=None, track_type="track", name_filter=None, limit=256, ranges=False):
    """ranges=True adds what Live displays at each parameter's min and max ("10.0 Hz", "22.0 kHz")."""
    track = get_track(ctx.song, track_index, track_type)
    device, path = resolve_device(track, device_index, device_path)
    params = list(device.parameters)
    needle = name_filter.lower() if name_filter else None
    selected = [(i, p) for i, p in enumerate(params) if needle is None or needle in p.name.lower()]
    limit = require_int(limit, "limit")
    details = []
    for i, p in selected[:limit]:
        detail = param_detail(p, i)
        if ranges and "items" not in detail:
            detail["min_display"], detail["max_display"] = param_display(p, p.min), param_display(p, p.max)
        details.append(detail)
    out = {
        "track": track.name,
        "device": _brief(device, device_index, path),
        "parameters": details,
    }
    if len(selected) > limit:
        out["truncated"] = True
        out["matching"] = len(selected)
    properties = _property_details(device)
    if properties:
        out["properties"] = properties
    return out


@command(undo=True)
def set_device_parameter(ctx, parameter, track_index=None, device_index=None, device_path=None, track_type="track", value=None, normalized=None):
    if (value is None) == (normalized is None):
        raise BridgeError("bad_args", "Pass exactly one of value or normalized.")
    track = get_track(ctx.song, track_index, track_type)
    device, path = resolve_device(track, device_index, device_path)
    index, param = _find_parameter(device, parameter)
    previous = {"value": rnd(param.value), "display": param_display(param)}
    lo, hi = float(param.min), float(param.max)

    if normalized is not None:
        n = require_number(normalized, "normalized")
        if not 0.0 <= n <= 1.0:
            raise BridgeError("bad_args", "normalized must be 0-1, got %s." % n)
        target = lo + n * (hi - lo)
        if param.is_quantized:
            target = float(round(target))
    elif isinstance(value, str):
        target = _value_for_text(param, value)
    else:
        target = require_number(value, "value")

    state = set_param_value(param, target)
    out = {"track": track.name, "device": device.name}
    if path is not None:
        out["path"] = path
    out["parameter"] = {"index": index, "name": param.name, "min": rnd(lo), "max": rnd(hi), "value": state["value"], "display": state["display"]}
    out["previous"] = previous
    return out


@command(undo=True)
def set_device_property(ctx, property, value, track_index=None, device_index=None, device_path=None, track_type="track"):
    """Device settings that aren't automatable parameters, such as Simpler's playback mode."""
    track = get_track(ctx.song, track_index, track_type)
    device, path = resolve_device(track, device_index, device_path)
    known = DEVICE_PROPERTIES.get(device.class_name)
    if not known:
        supported = "; ".join("%s: %s" % (cls, ", ".join(props)) for cls, props in sorted(DEVICE_PROPERTIES.items()))
        raise BridgeError("bad_args", "'%s' (%s) has no settable properties. Supported: %s. Use set_device_parameter for its parameters." % (device.name, device.class_name, supported))
    if property not in known:
        raise BridgeError("bad_args", "'%s' has no property %r. It has: %s." % (device.name, property, ", ".join(known)))
    options = known[property]
    previous = device_properties(device).get(property)
    setattr(device, property, _option_index(options, value, property))
    out = {"track": track.name, "device": device.name}
    if path is not None:
        out["path"] = path
    out["property"] = {"name": property, "value": device_properties(device).get(property), "options": list(options)}
    out["previous"] = previous
    return out


@command()
def get_plugin_programs(ctx, track_index=None, device_index=None, device_path=None, track_type="track", offset=0, limit=200):
    """A loaded plugin's programs (PluginDevice.presets): the factory programs the plugin offers to
    the host. Many VST3 plugins keep their presets in their own browser and list none here."""
    track = get_track(ctx.song, track_index, track_type)
    device, path = resolve_device(track, device_index, device_path)
    programs = _programs(device)
    offset, limit = require_int(offset, "offset"), require_int(limit, "limit")
    out = _program_state(track, device, path, programs)
    out["programs"] = [{"index": i, "name": name} for i, name in enumerate(programs)][offset : offset + limit]
    if offset + limit < len(programs):
        out["more"] = True
    return out


@command(undo=True)
def set_plugin_program(ctx, program, track_index=None, device_index=None, device_path=None, track_type="track"):
    """Select a plugin program by index or name (exact, then the only one containing the text)."""
    track = get_track(ctx.song, track_index, track_type)
    device, path = resolve_device(track, device_index, device_path)
    programs = _programs(device)
    if not programs:
        raise BridgeError("invalid_state", "'%s' offers no programs to Live. Load a preset in its own window, or a rack that holds it." % device.name)
    previous = _program_state(track, device, path, programs)["selected"]
    device.selected_preset_index = _program_index(programs, program, device.name)
    out = _program_state(track, device, path, programs)
    out["previous"] = previous
    return out


def _programs(device):
    if device.class_name not in PLUGIN_FORMATS:
        raise BridgeError("bad_args", "'%s' isn't a plugin (it's %s). Only VST and AU plugins have programs." % (device.name, device.class_name))
    return [str(p) for p in (safe(lambda: device.presets, ()) or ())]


def _program_state(track, device, path, programs):
    out = {"track": track.name, "device": device.name, "plugin": PLUGIN_FORMATS[device.class_name]}
    if path is not None:
        out["path"] = path
    index = safe(lambda: device.selected_preset_index, -1)
    out["count"] = len(programs)
    out["selected"] = {"index": index, "name": programs[index]} if isinstance(index, int) and 0 <= index < len(programs) else None
    return out


def _program_index(programs, program, device_name):
    if isinstance(program, int) and not isinstance(program, bool):
        if not 0 <= program < len(programs):
            raise BridgeError("not_found", "'%s' has %d programs (0-%d), no %d." % (device_name, len(programs), len(programs) - 1, program))
        return program
    if not isinstance(program, str) or not program.strip():
        raise BridgeError("bad_args", "program must be an index or a name.")
    wanted = program.strip().lower()
    exact = [i for i, name in enumerate(programs) if name.strip().lower() == wanted]
    if exact:
        return exact[0]
    partial = [i for i, name in enumerate(programs) if wanted in name.lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        listed = ", ".join("%d %s" % (i, programs[i]) for i in partial[:10])
        raise BridgeError("bad_args", "'%s' matches %d programs on '%s': %s. Use one of those names or an index." % (program, len(partial), device_name, listed))
    close = difflib.get_close_matches(program, programs, n=5, cutoff=0.5)
    hint = " Close matches: %s." % ", ".join(close) if close else ""
    raise BridgeError("not_found", "No program '%s' on '%s'.%s" % (program, device_name, hint))


def _brief(device, device_index, path):
    if path is None:
        return device_brief(device, 0 if device_index is None else device_index)
    return nested_device_brief(device, path)


def _property_details(device):
    known = DEVICE_PROPERTIES.get(device.class_name, {})
    current = device_properties(device)
    return [{"name": name, "value": current.get(name), "options": list(options)} for name, options in known.items()]


def _option_index(options, value, label):
    if isinstance(value, int) and not isinstance(value, bool):
        if not 0 <= value < len(options):
            raise BridgeError("bad_args", "%s takes 0-%d (%s), got %d." % (label, len(options) - 1, ", ".join(options), value))
        return value
    if isinstance(value, str):
        key = _normalize(value).replace("-", "")
        for i, option in enumerate(options):
            if _normalize(option).replace("-", "") == key:
                return i
    raise BridgeError("bad_args", "%r isn't an option for %s. Options: %s." % (value, label, ", ".join(options)))


def _find_parameter(device, parameter):
    params = list(device.parameters)
    if isinstance(parameter, bool):
        raise BridgeError("bad_args", "parameter must be an index or a name.")
    if isinstance(parameter, int):
        if not 0 <= parameter < len(params):
            raise BridgeError("not_found", "'%s' has %d parameters (0-%d), no %d." % (device.name, len(params), len(params) - 1, parameter))
        return parameter, params[parameter]
    if not isinstance(parameter, str):
        raise BridgeError("bad_args", "parameter must be an index or a name.")
    for matcher in (lambda p: p.name == parameter, lambda p: p.name.lower() == parameter.lower()):
        hits = [(i, p) for i, p in enumerate(params) if matcher(p)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise BridgeError("bad_args", "'%s' matches %d parameters on '%s' (indices %s). Use an index." % (parameter, len(hits), device.name, ", ".join(str(i) for i, _ in hits)))
    close = difflib.get_close_matches(parameter, [p.name for p in params], n=5, cutoff=0.5)
    hint = " Close matches: %s." % ", ".join(close) if close else ""
    raise BridgeError("not_found", "No parameter '%s' on '%s'.%s" % (parameter, device.name, hint))


def _value_for_text(param, text):
    """An option name ("Lowpass", "1/4") or a display value ("120 Hz", "1.2 kHz", "350 ms", "-6 dB")."""
    if param.is_quantized:
        options = [str(i) for i in param.value_items]
    else:
        options = stepped_items(param)
        if options is None:
            return value_for_display(param, text)
    key = _normalize(text)
    for i, option in enumerate(options):
        if _normalize(option) == key:
            return float(param.min) + i
    wanted = parse_display(text) if is_plain_value(text) else None
    if wanted is not None and not param.is_quantized:  # "+7" for a "+7 st" step
        shown = [parse_display(o) for o in options]
        close = [(abs(v[0] - wanted[0]), i) for i, v in enumerate(shown) if v is not None and wanted[1] in ("", v[1])]
        if close and min(close)[0] < 0.5:
            return float(param.min) + min(close)[1]
    raise BridgeError("bad_args", "'%s' isn't an option for '%s'. Options: %s." % (text, param.name, _list_options(options, param)))


def _list_options(options, param):
    """Every option, or for long lists (a macro over 128 steps) the first ones, the last and the range."""
    unique = [o for i, o in enumerate(options) if i == 0 or o != options[i - 1]]
    if len(unique) <= 32:
        return ", ".join(unique)
    return "%s, ..., %s (%d steps, raw %s to %s)" % (", ".join(unique[:16]), unique[-1], len(options), rnd(param.min), rnd(param.max))


def _normalize(text):
    return "".join(str(text).replace("−", "-").lower().split())
