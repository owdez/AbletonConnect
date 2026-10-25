"""Scenes: create, name, delete and launch."""

from ..dispatch import WAIT, command
from ..lom import require_int, safe
from ..protocol import BridgeError


def get_scene(song, scene_index):
    require_int(scene_index, "scene_index")
    scenes = list(song.scenes)
    if not 0 <= scene_index < len(scenes):
        raise BridgeError("not_found", "No scene %d. The set has %d scenes (0-%d)." % (scene_index, len(scenes), len(scenes) - 1))
    return scenes[scene_index]


def scene_brief(song, scene, index):
    clips = 0
    for track in song.tracks:
        slots = list(track.clip_slots)
        if index < len(slots) and slots[index].has_clip:
            clips += 1
    return {"index": index, "name": scene.name, "clips": clips}


@command(undo=True)
def create_scene(ctx, index=-1, name=None):
    song = ctx.song
    count = len(song.scenes)
    require_int(index, "index")
    if index != -1 and not 0 <= index <= count:
        raise BridgeError("bad_args", "index must be -1 (end) or 0-%d, got %d." % (count, index))
    song.create_scene(index)
    new_index = count if index == -1 else index
    scene = list(song.scenes)[new_index]
    if name:
        scene.name = name
    return {"created": scene_brief(song, scene, new_index), "scene_count": len(song.scenes)}


@command(undo=True)
def set_scene(ctx, scene_index, name):
    if not isinstance(name, str):
        raise BridgeError("bad_args", "name must be text (an empty string clears it).")
    song = ctx.song
    scene = get_scene(song, scene_index)
    previous = scene.name
    scene.name = name
    out = scene_brief(song, scene, scene_index)
    out["previous_name"] = previous
    return out


@command(undo=True)
def delete_scene(ctx, scene_index):
    song = ctx.song
    scene = get_scene(song, scene_index)
    if len(song.scenes) == 1:
        raise BridgeError("invalid_state", "A set needs at least one scene.")
    deleted = scene_brief(song, scene, scene_index)
    song.delete_scene(scene_index)
    return {"deleted": deleted, "scene_count": len(song.scenes)}


@command()
def fire_scene(ctx, scene_index):
    """Launch every clip in the scene, like clicking its launch button (quantized). Starts the transport."""
    song = ctx.song
    scene = get_scene(song, scene_index)
    scene.fire()
    yield WAIT
    launching = 0
    for track in song.tracks:
        slots = list(track.clip_slots)
        if scene_index < len(slots) and slots[scene_index].has_clip:
            slot = slots[scene_index]
            if safe(lambda s=slot: s.is_triggered or s.is_playing, False):
                launching += 1
    out = scene_brief(song, scene, scene_index)
    out.update({"launching": launching, "transport_playing": bool(song.is_playing)})
    return out
