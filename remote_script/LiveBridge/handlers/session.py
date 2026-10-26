"""Session overview, transport, tempo and undo.

Song positions are beats (quarter notes) from the start of the Arrangement; bars are 1-based, so
bar 25 of 4/4 is beat 96.

Transport (verified in Live 12.4.7): start_playing plays from the start marker (song.start_time),
not from current_song_time, so a stopped jump moves both. Setting current_song_time while playing
jumps at once. back_to_arranger is True while Session clips override the Arrangement; setting it
False stops those clips and the tracks play the timeline again, like the button. None of these are
undo steps.
"""

from ..dispatch import WAIT, command
from ..lom import NOTE_NAMES, beats_per_bar, find_track_index, get_track, require_number, rnd, safe, track_brief, track_detail
from ..protocol import BridgeError

LAST_BEAT = 1576800.0  # Live's timeline ends here

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
def start_playback(ctx, from_start=False, bar=None, beat=None):
    """Play from where the transport is, from the start, or from a bar or beat."""
    song = ctx.song
    if from_start and (bar is not None or beat is not None):
        raise BridgeError("bad_args", "Pass from_start or a bar or beat, not both.")
    target = 0.0 if from_start else _position(song, bar, beat, required=False)
    if target is None:
        song.start_playing()
    else:
        _move_to(song, target)
        if not song.is_playing:
            song.start_playing()
    yield WAIT
    return _transport(song)


@command()
def jump_to(ctx, bar=None, beat=None):
    """Move the play position. While playing, playback continues from there."""
    song = ctx.song
    _move_to(song, _position(song, bar, beat))
    yield WAIT
    return _transport(song)


@command()
def set_arrangement_loop(ctx, start_bar=None, start_beat=None, length_bars=None, length_beats=None, enabled=None):
    """Move the Arrangement loop brace and switch looping on or off. Only what's passed changes.

    Verified in Live 12.4.7: loop changes aren't undo steps, song.loop reads back its new value
    only on the next tick, and the loop can't start after the end of the song.
    """
    song = ctx.song
    if all(v is None for v in (start_bar, start_beat, length_bars, length_beats, enabled)):
        raise BridgeError("bad_args", "Nothing to change. Pass start_bar or start_beat, length_bars or length_beats, or enabled.")
    bpb = beats_per_bar(song)
    start = _position(song, start_bar, start_beat, required=False, names=("start_bar", "start_beat"))
    if length_bars is not None and length_beats is not None:
        raise BridgeError("bad_args", "Pass length_bars or length_beats, not both.")
    length = None
    if length_bars is not None:
        length = require_number(length_bars, "length_bars") * bpb
    elif length_beats is not None:
        length = require_number(length_beats, "length_beats")
    if length is not None and length <= 0:
        raise BridgeError("bad_args", "The loop must be longer than 0.")
    if start is not None:
        try:
            song.loop_start = start
        except RuntimeError:  # "Cannot set the Loopstart behind the Songlength"
            end = rnd(song.song_length / bpb + 1, 3)
            raise BridgeError("bad_args", "The loop can't start after the end of the song (bar %g). Put clips there first or start it earlier." % end) from None
    if length is not None:
        song.loop_length = length
    if enabled is not None:
        song.loop = bool(enabled)
    yield WAIT
    return _transport(song)


@command()
def back_to_arrangement(ctx):
    """Like Live's Back to Arrangement button: tracks playing Session clips go back to the timeline."""
    song = ctx.song
    was = bool(song.back_to_arranger)
    song.back_to_arranger = False
    yield WAIT
    out = _transport(song)
    out["was_overridden"] = was
    session = [t.name for t in song.tracks if safe(lambda t=t: t.playing_slot_index, -1) >= 0]
    if session:
        out["session_clips_playing"] = session
    return out


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


def _position(song, bar, beat, required=True, names=("bar", "beat")):
    """A song position in beats from a 1-based bar or a beat, or None when neither is passed."""
    if bar is not None and beat is not None:
        raise BridgeError("bad_args", "Pass %s or %s, not both." % names)
    if bar is not None:
        bar = require_number(bar, names[0])
        if bar < 1:
            raise BridgeError("bad_args", "%s starts at 1 (the start of the Arrangement), got %s." % (names[0], rnd(bar, 3)))
        beats = (bar - 1) * beats_per_bar(song)
    elif beat is not None:
        beats = require_number(beat, names[1])
        if beats < 0:
            raise BridgeError("bad_args", "%s must be 0 or more, got %s." % (names[1], rnd(beats, 3)))
    elif required:
        raise BridgeError("bad_args", "Pass %s (1 = the start) or %s (0 = the start)." % names)
    else:
        return None
    if beats > LAST_BEAT:
        raise BridgeError("bad_args", "That's past the end of Live's timeline (beat %d)." % LAST_BEAT)
    return beats


def _move_to(song, beats):
    # Stopped, Play starts from the start marker (start_time), so move it with the playhead.
    song.current_song_time = beats
    if not song.is_playing and safe(lambda: song.start_time) is not None:
        song.start_time = beats


def _transport(song):
    bpb = beats_per_bar(song)
    now = song.current_song_time
    out = {"playing": bool(song.is_playing), "song_time": rnd(now, 3), "bar": rnd(now / bpb + 1, 3)}
    start, length = safe(lambda: song.loop_start), safe(lambda: song.loop_length)
    if start is not None and length is not None:
        out["loop"] = {"on": bool(safe(lambda: song.loop, False)), "start_bar": rnd(start / bpb + 1, 3), "end_bar": rnd((start + length) / bpb + 1, 3)}
    if safe(lambda: song.back_to_arranger, False):
        out["back_to_arrangement"] = True  # Session clips are playing instead of the Arrangement on some tracks
    if safe(lambda: song.metronome, False):
        out["metronome"] = True
    return out
