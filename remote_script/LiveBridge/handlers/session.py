"""Session overview, transport, tempo and undo."""

from ..dispatch import WAIT, command
from ..lom import NOTE_NAMES, find_track_index, get_track, require_number, rnd, safe, track_brief, track_detail
from ..protocol import BridgeError

_FLATS = {"Db": 1, "Eb": 3, "Gb": 6, "Ab": 8, "Bb": 10}


@command()
def ping(ctx):
    return {"pong": True}


@command()
def get_session_info(ctx):
    song = ctx.song
    app = ctx.application
    out = {
        "live_version": "%d.%d.%d" % (app.get_major_version(), app.get_minor_version(), app.get_bugfix_version()),
        "tempo": rnd(song.tempo, 3),
        "signature": "%d/%d" % (song.signature_numerator, song.signature_denominator),
    }
    out.update(_transport(song))
    root = safe(lambda: song.root_note)
    scale = safe(lambda: song.scale_name)
    if isinstance(root, int) and scale:
        out["scale"] = "%s %s" % (NOTE_NAMES[root % 12], scale)
    out["scenes"] = len(song.scenes)
    names = [safe(lambda s=s: s.name, "") or "" for s in song.scenes]
    if any(names):
        out["scene_names"] = names
    selected_type, selected_index = find_track_index(song, song.view.selected_track)
    if selected_type is not None:
        out["selected_track"] = {"type": selected_type, "index": selected_index}
    out["tracks"] = [track_brief(t, i, "track") for i, t in enumerate(song.tracks)]
    out["returns"] = [track_brief(t, i, "return") for i, t in enumerate(song.return_tracks)]
    out["master"] = track_brief(song.master_track, None, "master")
    out["can_undo"] = bool(safe(lambda: song.can_undo, False))
    return out


@command()
def get_track_info(ctx, track_index=None, track_type="track"):
    song = ctx.song
    track = get_track(song, track_index, track_type)
    return track_detail(track, track_index if track_type != "master" else None, song, track_type)


@command(undo=True)
def set_tempo(ctx, bpm):
    bpm = require_number(bpm, "bpm")
    if not 20.0 <= bpm <= 999.0:
        raise BridgeError("bad_args", "Tempo must be between 20 and 999 BPM, got %s." % bpm)
    song = ctx.song
    previous = song.tempo
    song.tempo = bpm
    return {"tempo": rnd(song.tempo, 3), "previous": rnd(previous, 3)}


@command(undo=True)
def set_scale(ctx, root=None, scale=None):
    """Live 12's song scale (the Scale chooser in the control bar): root note and scale name."""
    if root is None and scale is None:
        raise BridgeError("bad_args", "Pass root (like 'E' or 4), scale (like 'Phrygian'), or both.")
    song = ctx.song
    if safe(lambda: song.scale_name) is None:
        raise BridgeError("invalid_state", "This version of Live has no song scale.")
    previous = _scale_text(song)
    if root is not None:
        song.root_note = _root_note(root)
    if scale is not None:
        if not isinstance(scale, str) or not scale.strip():
            raise BridgeError("bad_args", "scale must be a scale name like 'Minor' or 'Phrygian'.")
        try:
            song.scale_name = scale
        except Exception:
            raise BridgeError("bad_args", "Live has no scale called %r. Use a name from Live's Scale chooser, like 'Minor' or 'Phrygian'." % scale) from None
        if song.scale_name.lower() != scale.lower():
            raise BridgeError("bad_args", "Live has no scale called %r (it kept %r). Use a name from Live's Scale chooser." % (scale, song.scale_name))
    return {"scale": _scale_text(song), "previous": previous}


def _root_note(root):
    if isinstance(root, int) and not isinstance(root, bool):
        if not 0 <= root <= 11:
            raise BridgeError("bad_args", "root must be 0-11 (0 = C), got %d." % root)
        return root
    if isinstance(root, str):
        text = root.strip().replace("♯", "#").replace("♭", "b")
        text = text[:1].upper() + text[1:]
        if text in NOTE_NAMES:
            return NOTE_NAMES.index(text)
        if text in _FLATS:
            return _FLATS[text]
    raise BridgeError("bad_args", "root must be a note name like 'E', 'F#' or 'Bb', or 0-11, got %r." % (root,))


def _scale_text(song):
    return "%s %s" % (NOTE_NAMES[song.root_note % 12], song.scale_name)


@command()
def start_playback(ctx, from_start=False):
    song = ctx.song
    if from_start:
        song.stop_playing()
        song.current_song_time = 0.0
    song.start_playing()
    yield WAIT
    return _transport(song)


@command()
def stop_playback(ctx):
    song = ctx.song
    song.stop_playing()
    yield WAIT
    return _transport(song)


@command()
def undo(ctx):
    song = ctx.song
    if not song.can_undo:
        raise BridgeError("invalid_state", "Nothing to undo.")
    song.undo()
    return {"undone": True, "can_undo": bool(song.can_undo), "can_redo": bool(safe(lambda: song.can_redo, False))}


def _transport(song):
    out = {"playing": bool(song.is_playing), "song_time": rnd(song.current_song_time, 3)}
    if safe(lambda: song.loop, False):
        out["loop"] = [rnd(song.loop_start, 3), rnd(song.loop_start + song.loop_length, 3)]
    if safe(lambda: song.metronome, False):
        out["metronome"] = True
    return out
