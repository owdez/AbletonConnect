"""Track creation and the mixer."""

from ..dispatch import command
from ..lom import (
    get_track,
    mixer_state,
    require_int,
    require_number,
    set_param_value,
    track_brief,
    value_for_db,
)
from ..protocol import BridgeError


def _create_track(ctx, kind, index, name):
    song = ctx.song
    count = len(song.tracks)
    require_int(index, "index")
    if index != -1 and not 0 <= index <= count:
        raise BridgeError("bad_args", "index must be -1 (end) or 0-%d, got %d." % (count, index))
    if kind == "midi":
        song.create_midi_track(index)
    else:
        song.create_audio_track(index)
    new_index = count if index == -1 else index
    track = list(song.tracks)[new_index]
    if name:
        track.name = name
    return {"created": track_brief(track, new_index, "track"), "track_count": len(song.tracks)}


@command(undo=True)
def create_midi_track(ctx, index=-1, name=None):
    return _create_track(ctx, "midi", index, name)


@command(undo=True)
def create_audio_track(ctx, index=-1, name=None):
    return _create_track(ctx, "audio", index, name)


@command(undo=True)
def set_mixer(ctx, track_index=None, track_type="track", volume=None, volume_db=None, pan=None, sends=None, sends_db=None, mute=None, solo=None):
    song = ctx.song
    track = get_track(song, track_index, track_type)
    mixer = track.mixer_device
    if volume is not None and volume_db is not None:
        raise BridgeError("bad_args", "Pass volume or volume_db, not both.")
    if all(v is None for v in (volume, volume_db, pan, sends, sends_db, mute, solo)):
        raise BridgeError("bad_args", "Nothing to change. Pass volume, volume_db, pan, sends, sends_db, mute or solo.")
    if track_type == "master" and (mute is not None or solo is not None):
        raise BridgeError("bad_args", "The master track has no mute or solo.")

    if volume is not None:
        set_param_value(mixer.volume, require_number(volume, "volume"), "volume")
    if volume_db is not None:
        mixer.volume.value = value_for_db(mixer.volume, require_number(volume_db, "volume_db"))
    if pan is not None:
        set_param_value(mixer.panning, require_number(pan, "pan"), "pan")

    send_params = list(mixer.sends)
    for values, in_db in ((sends, False), (sends_db, True)):
        if values is None:
            continue
        if not isinstance(values, dict):
            raise BridgeError("bad_args", "sends must map return index to value, e.g. {\"0\": 0.5}.")
        for key, value in values.items():
            try:
                i = int(key)
            except (TypeError, ValueError):
                raise BridgeError("bad_args", "Send key %r isn't a return index." % (key,)) from None
            if not 0 <= i < len(send_params):
                raise BridgeError("not_found", "No send %d on '%s'. It has %d send(s)." % (i, track.name, len(send_params)))
            label = "send %d" % i
            if in_db:
                send_params[i].value = value_for_db(send_params[i], require_number(value, label))
            else:
                set_param_value(send_params[i], require_number(value, label), label)

    if mute is not None:
        track.mute = bool(mute)
    if solo is not None:
        track.solo = bool(solo)

    out = {"track": track.name}
    out.update(mixer_state(track, song, track_type))
    return out
