"""The Arrangement timeline: lay Session View scenes out as sections.

Live's API can't record into the Arrangement or stretch a clip there, but it can create a
MIDI clip of any length on the timeline (Track.create_midi_clip, Live 12) and copy any clip
there (Track.duplicate_clip_to_arrangement, Live 11). A looping MIDI clip becomes one
timeline clip per section with its notes repeated to fill it, like recording the scene into
the Arrangement would. Audio clips are copied end to end.

Everything happens in one call, inside one tick: Live merges a script's changes into a
single undo step only within a tick (see CLAUDE.md), so one undo removes the whole layout.
"""

import math

import Live

from ..dispatch import command
from ..lom import beats_per_bar, require_int, require_number, rnd, safe
from ..protocol import BridgeError

MAX_SECTIONS = 64
MAX_COPIES = 4000  # timeline clips per call, so a typo in "bars" can't flood the set
LAST_BEAT = 1576800.0  # Live's limit for arrangement positions


@command(undo=True)
def arrange_scenes(ctx, sections, start_bar=1, replace=False, show=True):
    """Copy scenes onto the timeline back to back: sections = [{"scene": 0, "bars": 32}, ...]."""
    song = ctx.song
    bpb = beats_per_bar(song)
    plan = _plan(song, sections, start_bar, bpb)
    start, end = plan[0][1], plan[-1][1] + plan[-1][2]
    tracks = [t for t in song.tracks if not safe(lambda t=t: t.is_foldable, False)]

    in_the_way = [(t, c) for t in tracks for c in _timeline(t) if c.start_time < end - 1e-9 and c.end_time > start + 1e-9]
    if in_the_way and not replace:
        names = sorted({t.name for t, _ in in_the_way})
        raise BridgeError(
            "invalid_state",
            "%d clip(s) already on the timeline overlap bars %s-%s (on %s). Pass replace=true to remove them first."
            % (len(in_the_way), rnd(start / bpb + 1, 3), rnd(end / bpb + 1, 3), ", ".join(names[:8]) + (", ..." if len(names) > 8 else "")),
        )
    copies = sum(_copies_needed(t, slot.clip, length) for scene, _, length in plan for t in tracks for slot in _filled_slot(t, scene))
    if copies > MAX_COPIES:
        raise BridgeError("bad_args", "That layout needs %d timeline clips (the limit is %d). Check the section lengths." % (copies, MAX_COPIES))

    # Live moves the playhead to each clip it creates on the timeline (it was left at the last
    # section's start in Live 12.4.7), so put it back where it was.
    position = safe(lambda: song.current_song_time)
    for track, clip in in_the_way:
        track.delete_clip(clip)
    scenes = list(song.scenes)
    warnings, skipped, made, summary = [], [], 0, []
    for scene, at, length in plan:
        placed = 0
        for track in tracks:
            for slot in _filled_slot(track, scene):
                placed += _place(track, slot.clip, at, length, warnings, skipped)
        made += placed
        summary.append({"scene": scene, "name": safe(lambda s=scene: scenes[s].name, ""), "start_bar": rnd(at / bpb + 1, 3), "bars": rnd(length / bpb, 3), "clips": placed})
    if position is not None and not safe(lambda: song.is_playing, False):
        safe(lambda: setattr(song, "current_song_time", position))

    if show:
        safe(lambda: ctx.application.view.show_view("Arranger"))
    out = {"sections": summary, "clips_created": made, "end_bar": rnd(end / bpb + 1, 3)}
    if in_the_way:
        out["removed"] = len(in_the_way)
    if skipped:
        out["skipped_empty"] = skipped[:20]
        out["skipped_note"] = "These clips have no notes inside their section (it's shorter than the clip), so nothing was placed for them."
    if warnings:
        out["warnings"] = warnings[:20]
    if any(safe(lambda t=t: t.playing_slot_index, -1) >= 0 for t in tracks):
        out["note"] = "Some tracks are playing Session clips and ignore the timeline until those stop (or you click Back to Arrangement)."
    return out


def _plan(song, sections, start_bar, bpb):
    """[(scene index, start beat, length in beats)] for back-to-back sections."""
    if not isinstance(sections, list) or not sections:
        raise BridgeError("bad_args", 'sections must be a non-empty list like [{"scene": 0, "bars": 32}].')
    if len(sections) > MAX_SECTIONS:
        raise BridgeError("bad_args", "At most %d sections per call, got %d." % (MAX_SECTIONS, len(sections)))
    start_bar = require_number(start_bar, "start_bar")
    if start_bar < 1:
        raise BridgeError("bad_args", "start_bar is 1-based, got %s." % start_bar)
    n_scenes = len(song.scenes)
    at, plan = (start_bar - 1) * bpb, []
    for i, section in enumerate(sections):
        if not isinstance(section, dict) or "scene" not in section or "bars" not in section or set(section) - {"scene", "bars"}:
            raise BridgeError("bad_args", 'Section %d must be {"scene": <index>, "bars": <length>}.' % i)
        scene = require_int(section["scene"], "scene")
        bars = require_number(section["bars"], "bars")
        if not 0 <= scene < n_scenes:
            raise BridgeError("not_found", "Section %d: no scene %d. The set has %d scenes (0-%d)." % (i, scene, n_scenes, n_scenes - 1))
        if bars <= 0:
            raise BridgeError("bad_args", "Section %d: bars must be > 0, got %s." % (i, bars))
        plan.append((scene, at, bars * bpb))
        at += bars * bpb
    if at > LAST_BEAT:
        raise BridgeError("bad_args", "That layout ends past the end of Live's timeline.")
    return plan


def _timeline(track):
    return list(safe(lambda: track.arrangement_clips, ()) or ())


def _filled_slot(track, scene):
    slots = list(track.clip_slots)
    return [slots[scene]] if scene < len(slots) and slots[scene].has_clip else []


def _region(clip):
    """(source start, span in beats, repeats?) of what a launched clip plays."""
    if safe(lambda: clip.looping, True):
        return float(clip.loop_start), float(clip.loop_end) - float(clip.loop_start), True
    start = float(safe(lambda: clip.start_marker, 0.0))
    return start, float(safe(lambda: clip.end_marker, clip.length)) - start, False


def _rebuilds(track, clip):
    return bool(clip.is_midi_clip) and hasattr(track, "create_midi_clip")


def _copies_needed(track, clip, length):
    if _rebuilds(track, clip):
        return 1
    span = float(clip.length)
    looping = bool(safe(lambda: clip.looping, True))
    return max(1, int(math.ceil(length / span - 1e-9))) if looping and span > 0 else 1


def _place(track, clip, at, length, warnings, skipped):
    """Put one section's clip on the timeline. Returns how many timeline clips it made."""
    if _rebuilds(track, clip):
        made = _rebuild_midi(track, clip, at, length)
        if not made:
            skipped.append("%s: %s" % (track.name, clip.name))
        return made
    copies = _copies_needed(track, clip, length)
    span = float(clip.length)
    for k in range(copies):
        track.duplicate_clip_to_arrangement(clip, at + k * span)
    overhang = copies * span - length
    if overhang > 1e-6:
        warnings.append("'%s' on '%s' runs %g beats past its section; trim it in Live." % (clip.name, track.name, rnd(overhang, 3)))
    return copies


def _rebuild_midi(track, clip, at, length):
    """One timeline clip for the section, or none when no notes fall inside it (a riser at the end
    of a 32-bar clip in a 24-bar section): an empty clip on the timeline is just clutter."""
    src, span, repeats = _region(clip)
    if span <= 0:
        return 0
    size = length if repeats else min(span, length)
    notes = clip.get_notes_extended(0, 128, src, span)
    fields = []
    for k in range(int(math.ceil(size / span - 1e-9)) if repeats else 1):
        for n in notes:
            start = n.start_time - src + k * span
            if start >= size - 1e-9:
                continue
            fields.append(
                {
                    "pitch": n.pitch,
                    "start_time": start,
                    "duration": min(n.duration, size - start),
                    "velocity": n.velocity,
                    "mute": bool(n.mute),
                    "probability": safe(lambda n=n: n.probability, 1.0),
                    "velocity_deviation": safe(lambda n=n: n.velocity_deviation, 0.0),
                    "release_velocity": safe(lambda n=n: n.release_velocity, 64.0),
                }
            )
    if not fields:
        return 0
    new = track.create_midi_clip(at, size)
    if new is None or not hasattr(new, "add_new_notes"):
        new = next((c for c in _timeline(track) if abs(c.start_time - at) < 1e-6 and c.is_midi_clip), None)
        if new is None:
            raise BridgeError("live_error", "Live didn't show the new clip on '%s' at beat %s." % (track.name, rnd(at, 3)))
    new.name = clip.name
    color = safe(lambda: clip.color)
    if color is not None:
        safe(lambda: setattr(new, "color", color))
    new.add_new_notes(tuple(Live.Clip.MidiNoteSpecification(**f) for f in fields))
    return 1
