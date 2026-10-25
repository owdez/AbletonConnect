"""Device parameters on a track's top-level device chain (rack macros included)."""

import difflib

from ..dispatch import command
from ..lom import device_brief, get_device, get_track, param_detail, param_display, require_int, require_number, rnd, set_param_value
from ..protocol import BridgeError


@command()
def get_device_parameters(ctx, track_index=None, device_index=0, track_type="track", name_filter=None, limit=256):
    track = get_track(ctx.song, track_index, track_type)
    device = get_device(track, device_index)
    params = list(device.parameters)
    needle = name_filter.lower() if name_filter else None
    selected = [(i, p) for i, p in enumerate(params) if needle is None or needle in p.name.lower()]
    limit = require_int(limit, "limit")
    out = {
        "track": track.name,
        "device": device_brief(device, device_index),
        "parameters": [param_detail(p, i) for i, p in selected[:limit]],
    }
    if len(selected) > limit:
        out["truncated"] = True
        out["matching"] = len(selected)
    return out


@command(undo=True)
def set_device_parameter(ctx, parameter, track_index=None, device_index=0, track_type="track", value=None, normalized=None):
    if (value is None) == (normalized is None):
        raise BridgeError("bad_args", "Pass exactly one of value or normalized.")
    track = get_track(ctx.song, track_index, track_type)
    device = get_device(track, device_index)
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
        target = _value_for_item(param, value)
    else:
        target = require_number(value, "value")

    state = set_param_value(param, target)
    return {
        "track": track.name,
        "device": device.name,
        "parameter": {"index": index, "name": param.name, "min": rnd(lo), "max": rnd(hi), "value": state["value"], "display": state["display"]},
        "previous": previous,
    }


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


def _value_for_item(param, text):
    if not param.is_quantized:
        raise BridgeError("bad_args", "'%s' is continuous. Pass a number from %s to %s (or normalized 0-1)." % (param.name, rnd(param.min), rnd(param.max)))
    items = [str(i) for i in param.value_items]
    for i, item in enumerate(items):
        if item.lower() == text.lower():
            return float(param.min) + i
    raise BridgeError("bad_args", "'%s' isn't an option for '%s'. Options: %s." % (text, param.name, ", ".join(items)))
