"""MCP server exposing Ableton Live through the LiveBridge Remote Script.

Runs over stdio (Claude Code starts it as a subprocess). The only network
socket is the client connection to LiveBridge on 127.0.0.1.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .client import LiveClient, LiveError

INSTRUCTIONS = """\
Controls the Ableton Live set that's open on this Mac.
- Start with get_session_info. All indices are 0-based: tracks, clip slots (one per scene), devices, parameters, sends.
- Every tool returns the state after the change. Each change is one Live undo step; `undo` reverts the last one.
- Clips are Session View clips. Times and lengths are in beats (quarter notes); 4.0 is one bar of 4/4.
- Volume: prefer volume_db. Raw fader value 0.85 is 0 dB.
- Find sounds with search_browser or browse_browser, then load_browser_item with the returned path.
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


async def call(cmd: str, args: dict[str, Any] | None = None, *, mutating: bool, timeout: float | None = None) -> str:
    clean = {k: v for k, v in (args or {}).items() if v is not None}
    try:
        result = await live.request(cmd, clean, mutating=mutating, timeout=timeout)
    except LiveError as e:
        raise ToolError(str(e)) from e
    return dumps(result)


def _track(track_index: int | None, track_type: str) -> dict[str, Any]:
    if track_type != "master" and track_index is None:
        raise ToolError(f"track_index is required when track_type is '{track_type}'.")
    return {"track_index": track_index, "track_type": track_type}


# ---- session ------------------------------------------------------------------------------------


@mcp.tool(annotations=READ, structured_output=False)
async def get_session_info() -> str:
    """Overview of the open set: tempo, signature, transport, scale, scene count, and every track,
    return and master with mute/solo/arm, volume, device names and clip count."""
    return await call("get_session_info", mutating=False)


@mcp.tool(annotations=READ, structured_output=False)
async def get_track_info(track_index: TrackIndex = None, track_type: TrackTypeArg = "track") -> str:
    """One track in detail: routing, mixer (volume, pan, sends with return names), devices with
    parameter counts, and every Session View clip with its slot index."""
    return await call("get_track_info", _track(track_index, track_type), mutating=False)


@mcp.tool(annotations=WRITE, structured_output=False)
async def set_tempo(bpm: Annotated[float, Field(ge=20, le=999)]) -> str:
    """Set the song tempo in BPM. Returns the new and previous tempo."""
    return await call("set_tempo", {"bpm": bpm}, mutating=True)


@mcp.tool(annotations=WRITE, structured_output=False)
async def start_playback(
    from_start: Annotated[bool, Field(description="Jump to the start of the arrangement first.")] = False,
) -> str:
    """Start the transport."""
    return await call("start_playback", {"from_start": from_start}, mutating=True)


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


# ---- devices ------------------------------------------------------------------------------------


@mcp.tool(annotations=READ, structured_output=False)
async def get_device_parameters(
    track_index: TrackIndex = None,
    device_index: Annotated[int, Field(ge=0, description="0-based index in the track's device chain.")] = 0,
    track_type: TrackTypeArg = "track",
    name_filter: Annotated[
        str | None, Field(description="Only parameters whose name contains this (case-insensitive).")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=2048)] = 256,
) -> str:
    """List a device's parameters: index, name, raw value, min, max, and Live's display string.
    Quantized parameters also list their options (items). Rack macros are the rack's parameters."""
    args = _track(track_index, track_type)
    args.update(device_index=device_index, name_filter=name_filter, limit=limit)
    return await call("get_device_parameters", args, mutating=False)


@mcp.tool(annotations=WRITE, structured_output=False)
async def set_device_parameter(
    parameter: Annotated[int | str, Field(description="Parameter index, or its exact name (case-insensitive).")],
    track_index: TrackIndex = None,
    device_index: Annotated[int, Field(ge=0)] = 0,
    track_type: TrackTypeArg = "track",
    value: Annotated[
        float | str | None,
        Field(
            description="Raw value between the parameter's min and max, or an option name for quantized parameters (e.g. 'Lowpass')."
        ),
    ] = None,
    normalized: Annotated[float | None, Field(ge=0, le=1, description="0-1 across the parameter's range.")] = None,
) -> str:
    """Set one device parameter. Pass exactly one of value or normalized. Returns the new value with
    Live's display string (e.g. '1.20 kHz') and the previous one."""
    if (value is None) == (normalized is None):
        raise ToolError("Pass exactly one of value or normalized.")
    args = _track(track_index, track_type)
    args.update(parameter=parameter, device_index=device_index, value=value, normalized=normalized)
    return await call("set_device_parameter", args, mutating=True)


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
    browser. This selects the track in Live. Instruments and effects go on the device chain; a sample
    on a MIDI track loads into Simpler. Returns the track's device chain afterwards."""
    args = _track(track_index, track_type)
    args.update(path=path, clip_slot_index=clip_slot_index)
    return await call("load_browser_item", args, mutating=True, timeout=20)


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mcp.run("stdio")
