"""MCP server exposing Ableton Live through the LiveBridge Remote Script.

Runs over stdio (Claude Code starts it as a subprocess). The only network
socket is the client connection to LiveBridge on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from sampleindex import config as sample_config
from sampleindex import search as sample_search

from . import kits as kit_store
from . import taste
from .client import LiveClient, LiveError
from .config import WINDOWS
from .plugins import SCAN_ON_WINDOWS
from .plugins import index as plugin_index

INSTRUCTIONS = """\
Controls the Ableton Live set that's open on this computer, for a producer with their own sound.
Working with them:
- Before creating anything, read get_taste_profile, list_kits and list_plugins. Their kits and plugins come before
  Live's stock devices and presets; reach for stock only when nothing of theirs fits, and say so.
- Don't compose from scratch. Ask for a seed first: a clip they played, a chord progression, a reference track or a
  kit, and build from it within their taste profile (tempo, key, palette, hard rules).
- When they change or delete something you made, ask why, then record the answer with add_taste_note (the Feedback
  log by default). Never rewrite their profile; only add notes.
Using Live:
- Start with get_session_info. All indices are 0-based: tracks, clip slots (one per scene), devices, parameters, sends.
- Every tool returns the state after the change. Each change is one Live undo step; `undo` reverts the last one.
- Clips live in Session View slots (one per scene). Times and lengths are in beats (quarter notes); 4.0 is one bar
  of 4/4. To build a song: one scene per section, name them with set_scene, then arrange_scenes puts the sections
  on the Arrangement timeline.
- Volume: prefer volume_db. Raw fader value 0.85 is 0 dB. Device parameters take what Live displays: '120 Hz', '350 ms', '1/4'.
- Movement over time (filter sweeps, volume rides, rising sends for build-ups) goes in clip envelopes:
  set_clip_envelope on the Session clip, before arrange_scenes, which carries them onto the timeline.
- Their sounds: load_kit for a kit (returns its macros), load_plugin for a plugin by name. Otherwise find sounds
  with search_browser or browse_browser, then load_browser_item with the returned path.
- For samples on disk, use search_samples first: it searches a local index of the sample folders (BPM, key, loop or
  one-shot, root note, brightness) instantly. Load a result with load_browser_item(path=<its browser_path>).
- Transport positions are 1-based bars or beats. To hear or meter one section, start_playback(bar=...) rather than
  playing from bar 1. After firing Session clips, back_to_arrangement hands the tracks back to the timeline.
- Live's API can't save the set. After you build or change something substantial, remind the user to save in Live
  (Cmd+S, or Ctrl+S on Windows).
"""

TrackType = Literal["track", "return", "master"]
TrackIndex = Annotated[
    int | None, Field(description="0-based index into tracks (or returns when track_type='return'). Omit for master.")
]
TrackTypeArg = Annotated[TrackType, Field(description="'track' (default), 'return', or 'master'.")]
SlotIndex = Annotated[int, Field(description="0-based clip slot index (= scene index).")]

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
REPLACE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

mcp = MCPServer("ableton", instructions=INSTRUCTIONS, version="0.1.0")
live = LiveClient()


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


async def request(cmd: str, args: dict[str, Any] | None = None, *, mutating: bool, timeout: float | None = None) -> Any:
    clean = {k: v for k, v in (args or {}).items() if v is not None}
    try:
        return await live.request(cmd, clean, mutating=mutating, timeout=timeout)
    except LiveError as e:
        raise ToolError(str(e)) from e


async def call(cmd: str, args: dict[str, Any] | None = None, *, mutating: bool, timeout: float | None = None) -> str:
    return dumps(await request(cmd, args, mutating=mutating, timeout=timeout))


def _track(track_index: int | None, track_type: str) -> dict[str, Any]:
    if track_type != "master" and track_index is None:
        raise ToolError(f"track_index is required when track_type is '{track_type}'.")
    return {"track_index": track_index, "track_type": track_type}


# ---- session ------------------------------------------------------------------------------------


@mcp.tool(annotations=READ, structured_output=False)
async def get_session_info() -> str:
    """Overview of the open set: tempo, signature, transport, scale, scene count and names, and every
    track, return and master with mute/solo/arm, volume, device names, Session clip count and
    Arrangement clip count."""
    return await call("get_session_info", mutating=False)


@mcp.tool(annotations=READ, structured_output=False)
async def get_track_info(track_index: TrackIndex = None, track_type: TrackTypeArg = "track") -> str:
    """One track in detail: routing, mixer (volume, pan, sends with return names), devices with
    parameter counts, every Session View clip with its slot index, and its Arrangement clips with
    start and end bars. Plugin devices show their format and vendor."""
    result = await request("get_track_info", _track(track_index, track_type), mutating=False)
    return dumps(plugin_index.annotate_devices(result, _plugin_entries(required=False)))


@mcp.tool(annotations=WRITE, structured_output=False)
async def set_tempo(bpm: Annotated[float, Field(ge=20, le=999)]) -> str:
    """Set the song tempo in BPM. Returns the new and previous tempo."""
    return await call("set_tempo", {"bpm": bpm}, mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def set_scale(
    root: Annotated[int | str | None, Field(description="Root note: 'E', 'F#', 'Bb', or 0-11 (0 = C).")] = None,
    scale: Annotated[
        str | None, Field(description="A name from Live's Scale chooser: 'Major', 'Minor', 'Dorian', 'Phrygian'...")
    ] = None,
) -> str:
    """Set the song's scale (Live 12's Scale chooser), which MIDI tools and scale-aware devices follow."""
    return await call("set_scale", {"root": root, "scale": scale}, mutating=True)


BarArg = Annotated[
    float | None,
    Field(ge=1, description="1-based bar: 1 is the start of the Arrangement, 25.5 is halfway into bar 25."),
]
BeatArg = Annotated[float | None, Field(ge=0, description="Beats (quarter notes) from the start of the Arrangement.")]


@mcp.tool(annotations=WRITE, structured_output=False)
async def start_playback(
    from_start: Annotated[bool, Field(description="Play from the start of the Arrangement.")] = False,
    bar: BarArg = None,
    beat: BeatArg = None,
) -> str:
    """Start the transport, from where Live's start marker is, from the start, or from a bar or
    beat (e.g. bar=25 to hear or meter one section without playing the song from bar 1)."""
    return await call("start_playback", {"from_start": from_start or None, "bar": bar, "beat": beat}, mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def jump_to(bar: BarArg = None, beat: BeatArg = None) -> str:
    """Move the play position to a bar or beat. While playing, playback carries on from there;
    stopped, Play will start there. Pass bar or beat."""
    return await call("jump_to", {"bar": bar, "beat": beat}, mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def set_arrangement_loop(
    start_bar: BarArg = None,
    start_beat: BeatArg = None,
    length_bars: Annotated[float | None, Field(gt=0, description="Loop length in bars.")] = None,
    length_beats: Annotated[float | None, Field(gt=0, description="Loop length in beats.")] = None,
    enabled: Annotated[bool | None, Field(description="Switch the Arrangement loop on or off.")] = None,
) -> str:
    """Set the Arrangement loop brace and switch looping on or off, e.g. start_bar=17,
    length_bars=8, enabled=true to loop the drop. Only what you pass changes. Live doesn't
    undo loop changes, so `undo` won't revert this."""
    args = {"start_bar": start_bar, "start_beat": start_beat, "length_bars": length_bars}
    args.update(length_beats=length_beats, enabled=enabled)
    return await call("set_arrangement_loop", args, mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def back_to_arrangement() -> str:
    """Press Live's Back to Arrangement button: stops the Session clips that took over tracks
    and those tracks play the Arrangement again. get_session_info shows back_to_arrangement:
    true while Session clips are overriding the timeline."""
    return await call("back_to_arrangement", mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def stop_playback() -> str:
    """Stop the transport."""
    return await call("stop_playback", mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def undo() -> str:
    """Undo the last change in Live. Each tool call that changes the set is one undo step."""
    return await call("undo", mutating=True)


# ---- tracks and mixer ---------------------------------------------------------------------------


@mcp.tool(annotations=WRITE, structured_output=False)
async def create_midi_track(
    index: Annotated[int, Field(description="Insert position; -1 appends at the end.")] = -1,
    name: str | None = None,
) -> str:
    """Create a MIDI track. Returns the new track and its index."""
    return await call("create_midi_track", {"index": index, "name": name}, mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def create_audio_track(
    index: Annotated[int, Field(description="Insert position; -1 appends at the end.")] = -1,
    name: str | None = None,
) -> str:
    """Create an audio track. Returns the new track and its index."""
    return await call("create_audio_track", {"index": index, "name": name}, mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def set_track(
    track_index: TrackIndex = None,
    track_type: TrackTypeArg = "track",
    name: Annotated[str | None, Field(min_length=1, description="New track name.")] = None,
    arm: Annotated[bool | None, Field(description="Record-arm (audio and MIDI tracks only).")] = None,
) -> str:
    """Rename a track or change its record arm. Only the fields you pass change."""
    args = _track(track_index, track_type)
    args.update(name=name, arm=arm)
    return await call("set_track", args, mutating=True)


@mcp.tool(annotations=REPLACE, structured_output=False)
async def delete_track(
    track_index: Annotated[
        int, Field(ge=0, description="0-based index into tracks, or returns with track_type='return'.")
    ],
    track_type: Annotated[Literal["track", "return"], Field(description="'track' (default) or 'return'.")] = "track",
) -> str:
    """Delete a track or return track with its clips and devices (undoable). Later tracks move up one
    index; the result lists the remaining tracks with their new indices."""
    return await call("delete_track", {"track_index": track_index, "track_type": track_type}, mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def set_mixer(
    track_index: TrackIndex = None,
    track_type: TrackTypeArg = "track",
    volume: Annotated[float | None, Field(ge=0, le=1, description="Raw fader value 0-1 (0.85 = 0 dB).")] = None,
    volume_db: Annotated[
        float | None, Field(description="Fader in dB, e.g. -6. Max +6. Very low values snap to -inf.")
    ] = None,
    pan: Annotated[float | None, Field(ge=-1, le=1, description="-1 = hard left, 0 = center, 1 = hard right.")] = None,
    sends: Annotated[dict[int, float] | None, Field(description="Return index -> raw send level 0-1.")] = None,
    sends_db: Annotated[dict[int, float] | None, Field(description="Return index -> send level in dB (max 0).")] = None,
    mute: bool | None = None,
    solo: bool | None = None,
) -> str:
    """Change a track's mixer: volume, pan, sends, mute, solo. Only the fields you pass change.
    Returns the full mixer state with Live's display strings."""
    args = _track(track_index, track_type)
    args.update(volume=volume, volume_db=volume_db, pan=pan, mute=mute, solo=solo)
    if sends is not None:
        args["sends"] = {str(k): v for k, v in sends.items()}
    if sends_db is not None:
        args["sends_db"] = {str(k): v for k, v in sends_db.items()}
    return await call("set_mixer", args, mutating=True)


# ---- clips and notes ----------------------------------------------------------------------------


class Note(BaseModel):
    pitch: Annotated[int, Field(ge=0, le=127, description="MIDI note number, 60 = C3 in Live.")]
    start: Annotated[float, Field(ge=0, description="Start in beats from the clip start.")]
    duration: Annotated[float, Field(gt=0, description="Length in beats.")]
    velocity: Annotated[float, Field(ge=1, le=127)] = 100
    mute: bool = False
    probability: Annotated[float, Field(ge=0, le=1)] = 1.0
    velocity_deviation: Annotated[float, Field(ge=-127, le=127)] = 0.0
    release_velocity: Annotated[float, Field(ge=0, le=127)] = 64


@mcp.tool(annotations=WRITE, structured_output=False)
async def create_clip(
    track_index: Annotated[int, Field(description="0-based MIDI track index.")],
    clip_slot_index: SlotIndex,
    length: Annotated[float, Field(gt=0, description="Length in beats; 4.0 = one bar of 4/4.")] = 4.0,
    name: str | None = None,
) -> str:
    """Create an empty MIDI clip in a Session View slot. The slot must be empty."""
    return await call(
        "create_clip",
        {"track_index": track_index, "clip_slot_index": clip_slot_index, "length": length, "name": name},
        mutating=True,
    )


@mcp.tool(annotations=REPLACE, structured_output=False)
async def add_notes(
    track_index: int,
    clip_slot_index: SlotIndex,
    notes: Annotated[list[Note], Field(min_length=1, max_length=10000)],
    replace: Annotated[bool, Field(description="Delete the clip's existing notes first.")] = False,
) -> str:
    """Add MIDI notes to a clip (one undo step for the whole batch). Returns the note count and
    warns about notes that start past the clip end."""
    payload = [n.model_dump(exclude_defaults=True) for n in notes]
    return await call(
        "add_notes",
        {"track_index": track_index, "clip_slot_index": clip_slot_index, "notes": payload, "replace": replace},
        mutating=True,
    )


@mcp.tool(annotations=READ, structured_output=False)
async def get_clip_notes(
    track_index: int,
    clip_slot_index: SlotIndex,
    from_time: Annotated[float, Field(ge=0)] = 0.0,
    time_span: Annotated[float | None, Field(gt=0, description="Beats to read; default the whole clip.")] = None,
    from_pitch: Annotated[int, Field(ge=0, le=127)] = 0,
    pitch_span: Annotated[int, Field(ge=1, le=128)] = 128,
    limit: Annotated[int, Field(ge=1, le=10000)] = 2000,
) -> str:
    """Read a MIDI clip's notes sorted by time. Default fields (velocity 100, probability 1, not
    muted) are left out."""
    return await call(
        "get_clip_notes",
        {
            "track_index": track_index,
            "clip_slot_index": clip_slot_index,
            "from_time": from_time,
            "time_span": time_span,
            "from_pitch": from_pitch,
            "pitch_span": pitch_span,
            "limit": limit,
        },
        mutating=False,
    )


@mcp.tool(annotations=WRITE, structured_output=False)
async def fire_clip(track_index: int, clip_slot_index: SlotIndex) -> str:
    """Launch a Session View clip (quantized like a click in Live). Starts the transport if stopped."""
    return await call("fire_clip", {"track_index": track_index, "clip_slot_index": clip_slot_index}, mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def stop_clip(
    track_index: int,
    clip_slot_index: Annotated[int | None, Field(description="Omit to stop every clip on the track.")] = None,
) -> str:
    """Stop a clip, or all clips on a track."""
    return await call("stop_clip", {"track_index": track_index, "clip_slot_index": clip_slot_index}, mutating=True)


ArrangementIndex = Annotated[
    int | None, Field(ge=0, description="A timeline clip's index from get_track_info's arrangement list.")
]


@mcp.tool(annotations=WRITE, structured_output=False)
async def set_clip(
    track_index: int,
    name: str,
    clip_slot_index: Annotated[int | None, Field(ge=0, description="Session View slot (= scene index).")] = None,
    arrangement_index: ArrangementIndex = None,
) -> str:
    """Rename a clip: a Session clip by clip_slot_index, or a timeline clip by arrangement_index."""
    args = {"track_index": track_index, "name": name}
    args.update(clip_slot_index=clip_slot_index, arrangement_index=arrangement_index)
    return await call("set_clip", args, mutating=True)


@mcp.tool(annotations=REPLACE, structured_output=False)
async def delete_clip(
    track_index: int,
    clip_slot_index: Annotated[int | None, Field(ge=0, description="Session View slot (= scene index).")] = None,
    arrangement_index: ArrangementIndex = None,
) -> str:
    """Delete a clip (undoable): a Session clip by clip_slot_index, or a timeline clip by
    arrangement_index. Later timeline clips on the track move down one index."""
    args = {"track_index": track_index, "clip_slot_index": clip_slot_index, "arrangement_index": arrangement_index}
    return await call("delete_clip", args, mutating=True)


# ---- devices ------------------------------------------------------------------------------------


DeviceIndexArg = Annotated[
    int | None,
    Field(ge=0, description="0-based index in the track's device chain. Default 0. Omit when using device_path."),
]
DevicePathArg = Annotated[
    list[int] | None,
    Field(
        description="A device inside a rack: alternating device and chain indices, e.g. [2, 0, 1] = device 2's chain 0's "
        "device 1. Drum Rack pads are chains. get_track_info lists each nested device with its path."
    ),
]


def _device(
    track_index: int | None, track_type: str, device_index: int | None, device_path: list[int] | None
) -> dict[str, Any]:
    if device_index is not None and device_path is not None:
        raise ToolError("Pass device_index or device_path, not both.")
    args = _track(track_index, track_type)
    args.update(device_index=device_index, device_path=device_path)
    return args


@mcp.tool(annotations=READ, structured_output=False)
async def get_device_parameters(
    track_index: TrackIndex = None,
    device_index: DeviceIndexArg = None,
    device_path: DevicePathArg = None,
    track_type: TrackTypeArg = "track",
    name_filter: Annotated[
        str | None, Field(description="Only parameters whose name contains this (case-insensitive).")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=2048)] = 256,
) -> str:
    """List a device's parameters: index, name, raw value, min, max, and Live's display string.
    Parameters with a fixed set of options (filter types, tempo-synced rates like '1/4') list them as
    items. A rack's own parameters are its macros; reach the devices inside it with device_path.
    Simpler also lists its properties (playback_mode: Classic, One-Shot or Slicing), which
    set_device_property changes. Only parameters Live exposes for automation appear: some device
    settings (Drift's Voice Mode, its mod matrix routing) have none and can't be changed."""
    args = _device(track_index, track_type, device_index, device_path)
    args.update(name_filter=name_filter, limit=limit)
    return await call("get_device_parameters", args, mutating=False)


@mcp.tool(annotations=WRITE, structured_output=False)
async def set_device_parameter(
    parameter: Annotated[int | str, Field(description="Parameter index, or its exact name (case-insensitive).")],
    track_index: TrackIndex = None,
    device_index: DeviceIndexArg = None,
    device_path: DevicePathArg = None,
    track_type: TrackTypeArg = "track",
    value: Annotated[
        float | str | None,
        Field(
            description="What Live displays: '120 Hz', '1.2 kHz', '350 ms', '-6 dB', '25 %', or an option like 'Lowpass' or '1/4'. Or a raw number between the parameter's min and max."
        ),
    ] = None,
    normalized: Annotated[float | None, Field(ge=0, le=1, description="0-1 across the parameter's range.")] = None,
) -> str:
    """Set one device parameter in one call and one undo step. Pass exactly one of value or
    normalized. Text values are matched against Live's own display, so curved controls like filter
    frequency land exactly. Returns the new value with Live's display string and the previous one."""
    if (value is None) == (normalized is None):
        raise ToolError("Pass exactly one of value or normalized.")
    args = _device(track_index, track_type, device_index, device_path)
    args.update(parameter=parameter, value=value, normalized=normalized)
    return await call("set_device_parameter", args, mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def set_device_property(
    property: Annotated[
        Literal["playback_mode", "slicing_playback_mode"],
        Field(
            description="Simpler: playback_mode (Classic, One-Shot, Slicing) or slicing_playback_mode (Mono, Poly, Thru)."
        ),
    ],
    value: Annotated[
        str | int, Field(description="An option name like 'Classic' or 'One-Shot', or its 0-based index.")
    ],
    track_index: TrackIndex = None,
    device_index: DeviceIndexArg = None,
    device_path: DevicePathArg = None,
    track_type: TrackTypeArg = "track",
) -> str:
    """Change a device setting that isn't an automatable parameter, in one undo step. Today that's
    Simpler's playback mode. One-Shot plays the sample with Fade In and Fade Out and ignores the
    volume envelope (Ve Attack, Ve Decay, Ve Sustain, Ve Release), so switch to Classic to shape
    a sound's decay with it."""
    args = _device(track_index, track_type, device_index, device_path)
    args.update(property=property, value=value)
    return await call("set_device_property", args, mutating=True)


# ---- meters -------------------------------------------------------------------------------------


@mcp.tool(annotations=READ, structured_output=False)
async def get_meters(
    duration: Annotated[float, Field(ge=2.5, le=10, description="Seconds to listen.")] = 3.0,
) -> str:
    """Measure every track, return and the master while the set plays, from the meters Live draws
    next to each fader (post-fader). Returns peak_db (the highest peak) and avg_db (the average
    level, a rough guide to how loud it sits) in dBFS. Tracks with no signal show silent; over
    means a peak above 0 dBFS. A muted track still shows the level it would play at, marked mute.
    Start playback first: with the transport stopped it returns an error. Waits out the meters' fall time, so a fader move or mute just before the call is
    measured correctly. These are peak meters, not RMS or LUFS."""
    return await call("get_meters", {"duration": duration}, mutating=False, timeout=duration + 10)


# ---- clip envelopes -----------------------------------------------------------------------------

Curve = Literal["linear", "hold", "ease_in", "ease_out", "s_curve"]
EnvelopeValue = Annotated[
    float | str,
    Field(description="What Live displays ('-12 dB', '800 Hz', '25L', 'C', '40 %') or a raw parameter value."),
]
EnvelopeParameter = Annotated[
    int | str,
    Field(
        description="Without device_index or device_path: a mixer control, 'volume', 'pan', 'send 0' or 'send A'. With one: the device parameter's name or index."
    ),
]
EnvelopeDevice = Annotated[
    int | None,
    Field(
        ge=0,
        description="Device index on the clip's track. Omit this and device_path for the mixer (volume, pan, sends).",
    ),
]


def _envelope_target(args: dict[str, Any], device_index: int | None, device_path: list[int] | None) -> dict[str, Any]:
    if device_index is not None and device_path is not None:
        raise ToolError("Pass device_index or device_path, not both.")
    args.update(device_index=device_index, device_path=device_path)
    return args


class EnvelopePoint(BaseModel):
    time: Annotated[float, Field(ge=0, description="Beats from the clip start.")]
    value: EnvelopeValue
    curve: Annotated[
        Curve,
        Field(description="Shape from this point to the next: linear, hold (step), ease_in, ease_out or s_curve."),
    ] = "linear"


class EnvelopeRamp(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    start: Annotated[float, Field(ge=0, description="Beat where the ramp starts.")]
    end: Annotated[float, Field(gt=0, description="Beat where it ends.")]
    from_: Annotated[EnvelopeValue, Field(alias="from")]
    to: EnvelopeValue
    curve: Annotated[
        Curve, Field(description="linear, ease_in (slow then fast, for build-ups), ease_out, s_curve or hold.")
    ] = "linear"


@mcp.tool(annotations=REPLACE, structured_output=False)
async def set_clip_envelope(
    track_index: int,
    clip_slot_index: SlotIndex,
    parameter: EnvelopeParameter,
    device_index: EnvelopeDevice = None,
    device_path: DevicePathArg = None,
    ramp: Annotated[
        EnvelopeRamp | None,
        Field(description="One move, e.g. {start: 0, end: 32, from: '200 Hz', to: '8 kHz', curve: 'ease_in'}."),
    ] = None,
    points: Annotated[
        list[EnvelopePoint] | None,
        Field(min_length=1, max_length=500, description="Breakpoints in time order, for shapes a ramp can't draw."),
    ] = None,
    replace: Annotated[
        bool,
        Field(
            description="True: the ramp or points are the whole envelope (the first and last values hold outside them). False: only the range they cover changes."
        ),
    ] = True,
) -> str:
    """Draw automation inside a Session clip: filter sweeps, volume rides, rising sends, pan moves.
    It plays every time the clip plays (looping with it), so a build-up clip carries its own sweep.
    Pass a ramp or points. Times are beats from the clip start; values are what Live displays, so
    endpoints like '200 Hz' land exactly. Live joins breakpoints with straight lines along the
    control, as if drawn in Live; ease_in/ease_out/s_curve are drawn as 16 short lines. Switches
    like filter type step at each point. One undo step. Live's API only gives Session clips
    envelopes, so draw them before arrange_scenes, which carries them onto the timeline."""
    if (ramp is None) == (points is None):
        raise ToolError("Pass exactly one of ramp or points.")
    args: dict[str, Any] = {
        "track_index": track_index,
        "clip_slot_index": clip_slot_index,
        "parameter": parameter,
        "replace": replace,
    }
    _envelope_target(args, device_index, device_path)
    if ramp is not None:
        args["ramp"] = ramp.model_dump(by_alias=True)
    else:
        args["points"] = [p.model_dump() for p in points or []]
    return await call("set_clip_envelope", args, mutating=True)


@mcp.tool(annotations=READ, structured_output=False)
async def get_clip_envelope(
    track_index: int,
    clip_slot_index: SlotIndex,
    parameter: Annotated[
        int | str | None,
        Field(description="Omit to list every envelope in the clip. Otherwise as in set_clip_envelope."),
    ] = None,
    device_index: EnvelopeDevice = None,
    device_path: DevicePathArg = None,
    resolution: Annotated[
        float | None,
        Field(gt=0, description="Sample the envelope every this many beats instead of listing breakpoints."),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=2000)] = 256,
) -> str:
    """Read a Session clip's envelope as breakpoints (time, raw value, Live's display), or list
    which parameters have envelopes in the clip."""
    args = {"track_index": track_index, "clip_slot_index": clip_slot_index, "parameter": parameter}
    args.update(resolution=resolution, limit=limit)
    return await call("get_clip_envelope", _envelope_target(args, device_index, device_path), mutating=False)


@mcp.tool(annotations=REPLACE, structured_output=False)
async def clear_clip_envelope(
    track_index: int,
    clip_slot_index: SlotIndex,
    parameter: Annotated[
        int | str | None,
        Field(description="Omit to clear every envelope in the clip. Otherwise as in set_clip_envelope."),
    ] = None,
    device_index: EnvelopeDevice = None,
    device_path: DevicePathArg = None,
) -> str:
    """Remove one envelope from a Session clip, or all of them (undoable)."""
    args = {"track_index": track_index, "clip_slot_index": clip_slot_index, "parameter": parameter}
    return await call("clear_clip_envelope", _envelope_target(args, device_index, device_path), mutating=True)


# ---- browser ------------------------------------------------------------------------------------


@mcp.tool(annotations=READ, structured_output=False)
async def browse_browser(
    path: Annotated[
        str,
        Field(
            description="Empty for the categories, then e.g. 'instruments', 'plugins/VST3', 'user_library/Presets', 'user_folders/Samples'. A '/' inside a name is written '\\/'."
        ),
    ] = "",
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
) -> str:
    """List one level of Live's browser (Sounds, Drums, Instruments, Audio/MIDI Effects, Max for Live,
    Plugins, Clips, Samples, Packs, User Library, Current Project, User Folders). Each item has a path
    for load_browser_item."""
    return await call("browse_browser", {"path": path, "offset": offset, "limit": limit}, mutating=False, timeout=20)


@mcp.tool(annotations=READ, structured_output=False)
async def search_browser(
    query: Annotated[str, Field(min_length=1, description="Words that must all appear in the item name.")],
    categories: Annotated[
        list[str] | None,
        Field(
            description="Category keys to search, e.g. ['instruments', 'plugins']. Default: all. 'samples' and 'user_folders' are large."
        ),
    ] = None,
    max_results: Annotated[int, Field(ge=1, le=200)] = 30,
    max_depth: Annotated[int, Field(ge=1, le=20)] = 8,
) -> str:
    """Search Live's browser by name, shallowest matches first. Stops after max_results or ~15 s and
    says so (truncated). Live's browser is slow on big sample folders."""
    return await call(
        "search_browser",
        {"query": query, "categories": categories, "max_results": max_results, "max_depth": max_depth},
        mutating=False,
        timeout=45,
    )


@mcp.tool(annotations=WRITE, structured_output=False)
async def load_browser_item(
    path: Annotated[str, Field(description="Item path from browse_browser or search_browser.")],
    track_index: TrackIndex = None,
    track_type: TrackTypeArg = "track",
    clip_slot_index: Annotated[
        int | None, Field(description="For samples on audio tracks: the empty slot to put the clip in.")
    ] = None,
) -> str:
    """Load a device, plugin, preset, or sample onto a track, like double-clicking it in Live's
    browser. An instrument replaces the track's current instrument; effects are added to the
    device chain; a sample on a MIDI track loads into Simpler; an instrument aimed at an audio
    track lands on a new MIDI track (listed in new_tracks). Returns the track's device chain and
    arm state afterwards. Live arms the track it loads onto; the selection and every track's arm
    are put back the way they were (arm_restored lists what was undone), so recording isn't
    redirected.

    A sample from a user folder going into an audio clip slot never changes the tempo. A loop from
    Live's own library that is the first clip in the set makes Live set the tempo from it; the
    result then shows `tempo`, and removing the load takes two undos."""
    args = _track(track_index, track_type)
    args.update(path=path, clip_slot_index=clip_slot_index)
    return await call("load_browser_item", args, mutating=True, timeout=20)


# ---- scenes and the arrangement -----------------------------------------------------------------

SceneIndex = Annotated[int, Field(ge=0, description="0-based scene index (= clip slot index).")]


@mcp.tool(annotations=WRITE, structured_output=False)
async def create_scene(
    index: Annotated[int, Field(description="Insert position; -1 appends at the end.")] = -1,
    name: str | None = None,
) -> str:
    """Create an empty scene (a new row of clip slots on every track)."""
    return await call("create_scene", {"index": index, "name": name}, mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def set_scene(scene_index: SceneIndex, name: Annotated[str, Field(description="'' clears the name.")]) -> str:
    """Name a scene, e.g. after the song section it holds ('Intro', 'Drop')."""
    return await call("set_scene", {"scene_index": scene_index, "name": name}, mutating=True)


@mcp.tool(annotations=REPLACE, structured_output=False)
async def delete_scene(scene_index: SceneIndex) -> str:
    """Delete a scene and every clip in its row (undoable). Later scenes move up one index."""
    return await call("delete_scene", {"scene_index": scene_index}, mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def fire_scene(scene_index: SceneIndex) -> str:
    """Launch every clip in a scene, like its launch button (quantized). Starts the transport."""
    return await call("fire_scene", {"scene_index": scene_index}, mutating=True)


class Section(BaseModel):
    scene: Annotated[int, Field(ge=0, description="Scene index to play.")]
    bars: Annotated[float, Field(gt=0, le=1024, description="How long the section lasts, in bars.")]


@mcp.tool(annotations=REPLACE, structured_output=False)
async def arrange_scenes(
    sections: Annotated[
        list[Section],
        Field(
            min_length=1,
            max_length=64,
            description="Sections in order, e.g. [{scene: 0, bars: 32}, {scene: 1, bars: 16}].",
        ),
    ],
    start_bar: Annotated[float, Field(ge=1, description="Bar where the first section starts.")] = 1,
    replace: Annotated[bool, Field(description="Remove timeline clips already in that range first.")] = False,
    show: Annotated[bool, Field(description="Switch Live to Arrangement View afterwards.")] = True,
) -> str:
    """Put scenes on the Arrangement timeline back to back, like recording them into the Arrangement:
    each track's clip from a section's scene fills that section. A looping MIDI clip becomes one
    timeline clip as long as the section with its notes repeated; a non-looping clip plays once;
    audio clips are copied end to end. Clip envelopes come along, repeated with the notes (listed in
    envelopes); on tracks with devices Live stores them as the track's automation, which stays if
    those timeline clips are later deleted or replaced, so undo a layout rather than replacing it.
    Tracks with no clip in a scene stay empty there, and a MIDI clip with nothing inside a shorter
    section is skipped (listed in skipped_empty). The whole layout is one undo step. Fails if
    timeline clips are in the way unless replace is true."""
    payload = {
        "sections": [s.model_dump() for s in sections],
        "start_bar": start_bar,
        "replace": replace,
        "show": show,
    }
    return await call("arrange_scenes", payload, mutating=True, timeout=60)


# ---- plugins (the index abletonconnect-scan-plugins writes, plus the user's plugin_roles.yaml) ------

_index_cache: dict[str, Any] = {}


def _plugin_entries(required: bool = True) -> list[dict[str, Any]]:
    """The plugin index with the user's roles, re-read when either file changes."""
    paths = (plugin_index.index_path(), plugin_index.roles_path())
    stamp = tuple(p.stat().st_mtime if p.exists() else None for p in paths)
    if _index_cache.get("stamp") != stamp or _index_cache.get("paths") != paths:
        try:
            entries = plugin_index.read(*paths)["plugins"]
        except FileNotFoundError:
            entries = None
        except ValueError as e:
            raise ToolError(str(e)) from e
        _index_cache.update(stamp=stamp, paths=paths, entries=entries)
    entries = _index_cache["entries"]
    if entries is None and required:
        if WINDOWS:
            raise ToolError(SCAN_ON_WINDOWS)
        raise ToolError(
            "There's no plugin index yet. Run `uv run --project server abletonconnect-scan-plugins` "
            "in the AbletonConnect folder with Live open on an empty set."
        )
    return entries or []


def _plugin(name: str) -> dict[str, Any]:
    try:
        return plugin_index.find(_plugin_entries(), name)
    except LookupError as e:
        raise ToolError(str(e)) from e


PluginKind = Literal["instrument", "effect", "midi_effect"]
PluginFormat = Literal["VST3", "AU", "VST2"]


@mcp.tool(annotations=READ, structured_output=False)
async def list_plugins(
    kind: Annotated[PluginKind | None, Field(description="instrument, effect or midi_effect.")] = None,
    vendor: Annotated[str | None, Field(description="Part of the vendor name, e.g. 'UAD' or 'iZotope'.")] = None,
    role: Annotated[
        str | None, Field(description="A role from the user's plugin_roles.yaml: 'drums', 'eq', 'delay', 'synth'...")
    ] = None,
    text: Annotated[str | None, Field(description="Words matched against names, vendors, tags and notes.")] = None,
    loadable_only: Annotated[bool, Field(description="Only plugins Live can load right now.")] = False,
) -> str:
    """The user's installed plugins (VST3, AU, VST2) from the local plugin index, with the role,
    tags and notes they gave each one. Prefer these over Live's stock devices when they fit the
    job. role_draft means the role is a guess the user hasn't confirmed. needs_attention says why
    a plugin couldn't be checked in Live (for example it wants activating)."""
    entries = plugin_index.filter_entries(
        _plugin_entries(), kind=kind, vendor=vendor, role=role, text=text, loadable_only=loadable_only
    )
    return dumps({"count": len(entries), "plugins": [plugin_index.summary(e) for e in entries]})


@mcp.tool(annotations=READ, structured_output=False)
async def get_plugin_info(
    name: Annotated[str, Field(description="The plugin's name, or a unique part of it.")],
    parameter_filter: Annotated[str | None, Field(description="Only parameters whose name contains this.")] = None,
    limit: Annotated[int, Field(ge=1, le=2048, description="Most parameters to list.")] = 300,
) -> str:
    """Everything the index knows about one plugin: vendor, version, kind, formats and their paths,
    which format Live loads, the user's role and notes, and the parameters (name, range, default,
    what Live displays at each end) and programs captured by loading it in Live."""
    entry = dict(_plugin(name))
    params = entry.pop("parameters", None)
    if params is not None:
        if parameter_filter:
            params = [p for p in params if parameter_filter.lower() in p["name"].lower()]
        entry["parameters"] = params[:limit]
        if len(params) > limit:
            entry["parameters_truncated"] = len(params)
    return dumps(entry)


@mcp.tool(annotations=WRITE, structured_output=False)
async def load_plugin(
    name: Annotated[str, Field(description="The plugin's name, or a unique part of it, as list_plugins shows.")],
    track_index: TrackIndex = None,
    track_type: TrackTypeArg = "track",
    format: Annotated[
        PluginFormat | None, Field(description="Prefer this format. Default: VST3, then AU, then VST2.")
    ] = None,
) -> str:
    """Load one of the user's plugins onto a track by name, resolved to the right place in Live's
    browser. Like load_browser_item: an instrument replaces the track's instrument, an effect is
    added to the chain, and the selection and record arms are left as they were."""
    entry = _plugin(name)
    if not entry.get("loadable"):
        reasons = "; ".join(f"{f}: {why}" for f, why in (entry.get("missing") or {}).items())
        raise ToolError(f"Live can't load {entry['name']}. {reasons or 'Run abletonconnect-scan-plugins again.'}")
    path = entry["load_path"]
    if format is not None:
        listed = (entry.get("live") or {}).get(format)
        if not listed:
            why = (entry.get("missing") or {}).get(format) or f"it isn't installed as {format}"
            raise ToolError(f"Live can't load {entry['name']} as {format}: {why} It has {', '.join(entry['live'])}.")
        path = listed[0]
    args = _track(track_index, track_type)
    args["path"] = path
    result = await request("load_browser_item", args, mutating=True, timeout=30)
    result = plugin_index.annotate_devices(result, _plugin_entries(required=False))
    result["plugin"] = plugin_index.summary(entry)
    if entry.get("needs_attention"):
        result["note"] = f"The plugin scan flagged it: {entry['needs_attention']}"
    return dumps(result)


ProgramArg = Annotated[int | str, Field(description="Program index, or its name (or a unique part of it).")]


@mcp.tool(annotations=READ, structured_output=False)
async def get_plugin_programs(
    track_index: TrackIndex = None,
    device_index: DeviceIndexArg = None,
    device_path: DevicePathArg = None,
    track_type: TrackTypeArg = "track",
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=2000)] = 200,
) -> str:
    """A loaded plugin's programs: the factory presets it offers Live, and which one is selected.
    Many VST3 plugins keep their presets in their own browser and offer only 'Default' here."""
    args = _device(track_index, track_type, device_index, device_path)
    args.update(offset=offset, limit=limit)
    return dumps(_annotate_plugin(await request("get_plugin_programs", args, mutating=False)))


@mcp.tool(annotations=WRITE, structured_output=False)
async def set_plugin_program(
    program: ProgramArg,
    track_index: TrackIndex = None,
    device_index: DeviceIndexArg = None,
    device_path: DevicePathArg = None,
    track_type: TrackTypeArg = "track",
) -> str:
    """Switch a loaded plugin to one of its programs (one undo step). Live can't load a plugin's
    own preset files; for a saved sound, use a kit (a rack saved with the plugin in it)."""
    args = _device(track_index, track_type, device_index, device_path)
    args["program"] = program
    return dumps(_annotate_plugin(await request("set_plugin_program", args, mutating=True)))


def _annotate_plugin(result: dict[str, Any]) -> dict[str, Any]:
    """The exact format and vendor of the plugin a program result is about."""
    device = {"name": result.get("device", ""), "plugin": result.get("plugin")}
    plugin_index.annotate_devices({"devices": [device]}, _plugin_entries(required=False))
    result["plugin"] = device["plugin"]
    if "vendor" in device:
        result["vendor"] = device["vendor"]
    return result


# ---- Claude Kits (racks with the user's sounds, described by kits.yaml) ---------------------------


async def _browser_kit_files() -> list[str] | None:
    """Kit files Live's browser shows under User Library/Claude Kits, or None when Live can't say."""
    found: list[str] = []

    async def walk(path: str, parts: list[str], depth: int) -> None:
        out = await live.request("browse_browser", {"path": path, "limit": 1000}, mutating=False, timeout=20)
        for item in out["items"]:
            if item.get("folder") and depth < 4:
                await walk(item["path"], [*parts, item["name"]], depth + 1)
            elif item["name"].lower().endswith(kit_store.RACK_SUFFIXES):
                found.append("/".join([*parts, item["name"]]))

    try:
        await walk(kit_store.BROWSER_ROOT, [], 0)
    except LiveError:
        return None
    return found


def _kits(browser_files: list[str] | None) -> list[dict[str, Any]]:
    try:
        return kit_store.catalog(browser_files=browser_files)
    except ValueError as e:
        raise ToolError(str(e)) from e


@mcp.tool(annotations=READ, structured_output=False)
async def list_kits(
    role: Annotated[
        str | None, Field(description="A role or its folder: drums, bass, synths, pads, fx, vocal chains, mix bus.")
    ] = None,
    tags: Annotated[
        list[str] | None, Field(description="Character tags that must all match, e.g. ['dark', 'warm'].")
    ] = None,
    text: Annotated[
        str | None, Field(description="Words matched against names, roles, notes, tags and macros.")
    ] = None,
) -> str:
    """The user's Claude Kits: racks holding their own sounds and chains, from kits.yaml in the
    Claude Kits folder of their User Library, checked against what's on disk and in Live's browser.
    Reach for these before stock devices. Each kit has its role, character tags, key or range
    notes and what each macro does. starter: true marks a placeholder the user means to replace;
    unlisted: true is a rack file with no manifest entry yet; problem says why a kit can't load."""
    browser_files = await _browser_kit_files()
    found = [k for k in _kits(browser_files) if kit_store.matches(k, role, tags, text)]
    out: dict[str, Any] = {"folder": str(kit_store.kits_dir()), "count": len(found), "kits": found}
    if browser_files is None:
        out["note"] = "Live didn't answer, so whether its browser shows each kit wasn't checked."
    return dumps(out)


@mcp.tool(annotations=WRITE, structured_output=False)
async def load_kit(
    name: Annotated[str, Field(description="The kit's name, or a unique part of it, as list_kits shows.")],
    track_index: TrackIndex = None,
    track_type: TrackTypeArg = "track",
) -> str:
    """Load one of the user's kits onto a track (an instrument kit replaces the track's instrument,
    an effect or chain kit is added to the end of its devices). Returns the rack's device index and
    its macros, with their names, values and what the manifest says each does, ready for
    set_device_parameter."""
    try:
        kit = kit_store.find(_kits(await _browser_kit_files()), name)
    except LookupError as e:
        raise ToolError(str(e)) from e
    if "browser_path" not in kit:
        raise ToolError(f"Kit '{kit['name']}' can't load: {kit.get('problem', 'its file is missing')}")
    args = _track(track_index, track_type)
    args["path"] = kit["browser_path"]
    loaded = await request("load_browser_item", args, mutating=True, timeout=45)
    stem = Path(kit["file"]).stem
    racks = [d for d in loaded.get("devices", []) if d["name"] == stem]
    out: dict[str, Any] = {"kit": kit["name"], "track": loaded.get("track"), "starter": bool(kit.get("starter"))}
    if not racks:
        out["devices"] = loaded.get("devices")
        out["note"] = loaded.get("note") or "Live hasn't shown the rack yet; check get_track_info."
        return dumps(out)
    rack = racks[-1]
    params = await request(
        "get_device_parameters",
        {**_track(track_index, track_type), "device_index": rack["index"], "limit": 32},
        mutating=False,
    )
    out["device_index"] = rack["index"]
    out["macros"] = kit_store.macros(params["parameters"], kit.get("macros"))
    for key in ("role", "tags", "range"):
        if kit.get(key):
            out[key] = kit[key]
    out["arm"] = loaded.get("arm")
    return dumps(plugin_index.annotate_devices(out, _plugin_entries(required=False)))


# ---- taste profile (the user's ~/.config/abletonconnect/taste.md) -----------------------------------


@mcp.tool(annotations=READ, structured_output=False)
async def get_taste_profile(
    section: Annotated[
        str | None,
        Field(description="One section, e.g. 'Defaults', 'Sound palette', 'Hard rules', 'Feedback log'. Omit for all."),
    ] = None,
) -> str:
    """The user's taste profile: who they are and their references, default tempo, keys and meter,
    their sound palette per role (kits, plugins, what to avoid), harmony, rhythm, arrangement and
    mix habits, hard rules, and a dated feedback log of what they kept, changed or rejected. Read it
    before making anything. Entries marked draft are unconfirmed: follow them, but say so."""
    try:
        return dumps(taste.get(section))
    except taste.TasteError as e:
        raise ToolError(str(e)) from e


@mcp.tool(annotations=WRITE, structured_output=False)
async def add_taste_note(
    note: Annotated[
        str,
        Field(
            min_length=1,
            description="What they said and about what, e.g. 'Rejected the vocal bed: pads too bright, wants darker, sparser chords.'",
        ),
    ],
    section: Annotated[
        str, Field(description="The section to add to; default the Feedback log.")
    ] = taste.DEFAULT_SECTION,
) -> str:
    """Append a dated note to the user's taste profile, at the end of the Feedback log or another
    section. Nothing else in the file changes. Use it when they change or delete something you made
    (after asking why), or tell you a preference worth keeping."""
    try:
        return dumps(taste.add_note(note, section))
    except taste.TasteError as e:
        raise ToolError(str(e)) from e


# ---- sample library (local index, no Live round trip) ----------------------------------------------


@mcp.tool(annotations=READ, structured_output=False)
async def search_samples(
    query: Annotated[
        str | None,
        Field(
            description="Words matched against file names, folder names and drum tags, e.g. 'dark kick', 'conga', 'amen break'. Every word must match, as a prefix. 'loop' or 'one shot' in the text sets kind."
        ),
    ] = None,
    kind: Annotated[Literal["loop", "oneshot"] | None, Field(description="Loops or one-shots only.")] = None,
    bpm: Annotated[
        float | None, Field(gt=0, le=999, description="Loops near this tempo. One-shots have no BPM.")
    ] = None,
    bpm_tolerance: Annotated[float, Field(ge=0, le=50, description="BPM either side of bpm.")] = 3.0,
    half_double: Annotated[bool, Field(description="Also match loops at half or double the tempo.")] = True,
    key: Annotated[
        str | None,
        Field(
            description="'A minor', 'Am', 'F#', 'Eb major' or Camelot '8A'. Matches samples whose file name gives that key, and pitched one-shots whose root note is its tonic."
        ),
    ] = None,
    compatible_keys: Annotated[
        bool, Field(description="Also match the relative key and the keys a fifth up and down.")
    ] = False,
    brightness: Annotated[
        Literal["dark", "mid", "bright"] | None,
        Field(description="The darkest, middle or brightest third of the other matches, by spectral centroid."),
    ] = None,
    min_length: Annotated[float | None, Field(ge=0, description="Seconds.")] = None,
    max_length: Annotated[float | None, Field(gt=0, description="Seconds.")] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> str:
    """Search the sample library: a local index of the sample folders, built outside Live by
    tools/sampleindex. Instant even on large libraries, unlike search_browser. Each result has a
    browser_path for load_browser_item. bpm_estimated means the tempo came from the audio (the loop
    length) rather than the file name; keys come only from file names. Doesn't need Live running."""
    try:
        index = sample_config.index_path(sample_config.load())
        result = await asyncio.to_thread(
            sample_search.search,
            index,
            text=query,
            kind=kind,
            bpm=bpm,
            bpm_tolerance=bpm_tolerance,
            half_double=half_double,
            key=key,
            compatible_keys=compatible_keys,
            brightness=brightness,
            min_length=min_length,
            max_length=max_length,
            limit=limit,
            offset=offset,
        )
    except (sample_config.ConfigError, sample_search.SearchError) as e:
        raise ToolError(str(e)) from e
    return dumps(result)


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mcp.run("stdio")
