"""Session View clips and MIDI notes (Live 11+ note API)."""

import Live

from ..dispatch import WAIT, command
from ..lom import clip_summary, get_clip, get_clip_slot, get_track, require_int, require_number, rnd, safe
from ..protocol import BridgeError

MAX_NOTES = 10000
NOTE_KEYS = ("pitch", "start", "duration", "velocity", "mute", "probability", "velocity_deviation", "release_velocity")


@command(undo=True)
def create_clip(ctx, track_index, clip_slot_index, length=4.0, name=None):
    song = ctx.song
    track = get_track(song, track_index, "track")
    if not track.has_midi_input:
        raise BridgeError("invalid_state", "'%s' is an audio track. Empty clips can only be created on MIDI tracks." % track.name)
    length = require_number(length, "length")
    if length <= 0:
        raise BridgeError("bad_args", "length must be > 0 beats, got %s." % length)
    slot = get_clip_slot(track, clip_slot_index)
    if slot.has_clip:
        raise BridgeError("invalid_state", "Slot %d on '%s' already has clip '%s'." % (clip_slot_index, track.name, slot.clip.name))
    slot.create_clip(length)
    clip = slot.clip
    if name:
        clip.name = name
    return {"track": track.name, "clip": clip_summary(clip, clip_slot_index)}


@command(undo=True)
def add_notes(ctx, track_index, clip_slot_index, notes, replace=False):
    song = ctx.song
    track = get_track(song, track_index, "track")
    clip = get_clip(track, clip_slot_index)
    if not clip.is_midi_clip:
        raise BridgeError("invalid_state", "The clip in slot %d on '%s' is an audio clip." % (clip_slot_index, track.name))
    if not isinstance(notes, list) or not notes:
        raise BridgeError("bad_args", "notes must be a non-empty list.")
    if len(notes) > MAX_NOTES:
        raise BridgeError("bad_args", "At most %d notes per call, got %d." % (MAX_NOTES, len(notes)))
    # Validate everything before touching the clip. Live's MidiNoteSpecification
    # is write-only (no readable start_time), so keep the parsed fields around.
    fields = [_note_fields(n, i) for i, n in enumerate(notes)]
    if replace:
        clip.remove_notes_extended(0, 128, 0.0, float(clip.length) + 1e6)
    clip.add_new_notes(tuple(Live.Clip.MidiNoteSpecification(**f) for f in fields))
    end = float(clip.loop_end) if safe(lambda: clip.looping, False) else float(clip.length)
    beyond = sum(1 for f in fields if f["start_time"] >= end)
    out = {"track": track.name, "added": len(fields), "replaced": bool(replace), "clip": clip_summary(clip, clip_slot_index)}
    if beyond:
        out["warning"] = "%d note(s) start at or after the clip end (%s beats) and won't play." % (beyond, rnd(end))
    return out


@command()
def get_clip_notes(ctx, track_index, clip_slot_index, from_time=0.0, time_span=None, from_pitch=0, pitch_span=128, limit=2000):
    song = ctx.song
    track = get_track(song, track_index, "track")
    clip = get_clip(track, clip_slot_index)
    if not clip.is_midi_clip:
        raise BridgeError("invalid_state", "The clip in slot %d on '%s' is an audio clip." % (clip_slot_index, track.name))
    from_time = require_number(from_time, "from_time")
    span = require_number(time_span, "time_span") if time_span is not None else float(clip.length) + 1e6
    raw = clip.get_notes_extended(require_int(from_pitch, "from_pitch"), require_int(pitch_span, "pitch_span"), from_time, span)
    notes = sorted((_note_out(n) for n in raw), key=lambda n: (n["start"], n["pitch"]))
    limit = require_int(limit, "limit")
    out = {"track": track.name, "clip": clip_summary(clip, clip_slot_index, notes=False), "count": len(notes), "notes": notes[:limit]}
    if len(notes) > limit:
        out["truncated"] = True
    return out


@command()
def fire_clip(ctx, track_index, clip_slot_index):
    song = ctx.song
    track = get_track(song, track_index, "track")
    slot = get_clip_slot(track, clip_slot_index)
    if not slot.has_clip:
        raise BridgeError("not_found", "Clip slot %d on '%s' is empty." % (clip_slot_index, track.name))
    slot.fire()
    yield WAIT
    return {"track": track.name, "clip": clip_summary(slot.clip, clip_slot_index, notes=False), "transport_playing": bool(song.is_playing)}


@command()
def stop_clip(ctx, track_index, clip_slot_index=None):
    song = ctx.song
    track = get_track(song, track_index, "track")
    if clip_slot_index is None:
        track.stop_all_clips()
    else:
        get_clip_slot(track, clip_slot_index).stop()
    yield WAIT
    playing = safe(lambda: track.playing_slot_index, -1)
    out = {"track": track.name, "stopped": "all" if clip_slot_index is None else clip_slot_index, "playing_slot": playing if isinstance(playing, int) and playing >= 0 else None}
    if out["playing_slot"] is not None and song.is_playing:
        out["note"] = "Live stops clips at the next launch-quantization boundary (usually the next bar)."
    return out


def _note_fields(note, i):
    """Validated keyword arguments for Live.Clip.MidiNoteSpecification."""
    if not isinstance(note, dict):
        raise BridgeError("bad_args", "Note %d must be an object with pitch, start and duration." % i)
    unknown = [k for k in note if k not in NOTE_KEYS]
    if unknown:
        raise BridgeError("bad_args", "Note %d has unknown field(s) %s. Allowed: %s." % (i, ", ".join(unknown), ", ".join(NOTE_KEYS)))
    try:
        pitch = require_int(note["pitch"], "pitch")
        start = require_number(note["start"], "start")
        duration = require_number(note["duration"], "duration")
    except KeyError as e:
        raise BridgeError("bad_args", "Note %d is missing %s." % (i, e.args[0])) from None
    velocity = require_number(note.get("velocity", 100), "velocity")
    probability = require_number(note.get("probability", 1.0), "probability")
    velocity_deviation = require_number(note.get("velocity_deviation", 0.0), "velocity_deviation")
    release_velocity = require_number(note.get("release_velocity", 64), "release_velocity")
    if not 0 <= pitch <= 127:
        raise BridgeError("bad_args", "Note %d: pitch must be 0-127, got %d." % (i, pitch))
    if start < 0:
        raise BridgeError("bad_args", "Note %d: start must be >= 0 beats, got %s." % (i, start))
    if duration <= 0:
        raise BridgeError("bad_args", "Note %d: duration must be > 0 beats, got %s." % (i, duration))
    if not 1 <= velocity <= 127:
        raise BridgeError("bad_args", "Note %d: velocity must be 1-127, got %s." % (i, velocity))
    if not 0 <= probability <= 1:
        raise BridgeError("bad_args", "Note %d: probability must be 0-1, got %s." % (i, probability))
    return {
        "pitch": pitch,
        "start_time": start,
        "duration": duration,
        "velocity": velocity,
        "mute": bool(note.get("mute", False)),
        "probability": probability,
        "velocity_deviation": max(-127.0, min(127.0, velocity_deviation)),
        "release_velocity": max(0.0, min(127.0, release_velocity)),
    }


def _note_out(n):
    out = {"pitch": n.pitch, "start": rnd(n.start_time), "duration": rnd(n.duration), "velocity": rnd(n.velocity, 2)}
    if n.mute:
        out["mute"] = True
    probability = safe(lambda: n.probability, 1.0)
    if probability is not None and probability < 1.0:
        out["probability"] = rnd(probability, 3)
    deviation = safe(lambda: n.velocity_deviation, 0.0)
    if deviation:
        out["velocity_deviation"] = rnd(deviation, 2)
    return out
