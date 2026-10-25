"""Session overview, transport, tempo and undo."""

from ..dispatch import WAIT, command
from ..lom import find_track_index, get_track, require_number, rnd, safe, track_brief, track_detail
from ..protocol import BridgeError

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


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
