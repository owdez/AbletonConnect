"""Live's browser: list, search and load items.

Items are addressed by path: a category key, then item names joined by "/"
("instruments/Operator", "user_folders/Samples/Kicks/kick 01.wav"). A "/" in
a name is escaped as "\\/". Walking large folders runs as a multi-tick job so
Live's UI never freezes.

Undo (verified in Live 12.4.7): browser.load_item records its own undo steps
and ignores begin/end_undo_step around it. When a looping sample is the first
clip in the set, Live also sets the song tempo from it as a second undo step.
So a sample from a user folder goes into an audio clip slot through
ClipSlot.create_audio_clip instead. That gives the same clip in one step and
leaves the tempo alone.
"""

import collections
import os
import time
import urllib.parse

from ..dispatch import WAIT, command
from ..lom import clip_summary, device_brief, get_clip_slot, get_track, require_int, require_number, rnd
from ..protocol import BridgeError

CATEGORIES = (
    ("sounds", "Sounds"),
    ("drums", "Drums"),
    ("instruments", "Instruments"),
    ("audio_effects", "Audio Effects"),
    ("midi_effects", "MIDI Effects"),
    ("max_for_live", "Max for Live"),
    ("plugins", "Plugins"),
    ("clips", "Clips"),
    ("samples", "Samples"),
    ("packs", "Packs"),
    ("user_library", "User Library"),
    ("current_project", "Current Project"),
    ("user_folders", "User Folders"),
)
AUDIO_EXTENSIONS = (".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg", ".m4a", ".aac")
YIELD_EVERY = 100
LOAD_WAIT_TICKS = 30


class _Folder:
    """Stands in for browser.user_folders, which is a tuple rather than a BrowserItem."""

    is_folder = True
    is_loadable = False
    is_device = False

    def __init__(self, name, children):
        self.name = name
        self.children = list(children)


# ---- paths --------------------------------------------------------------------------------------


def escape(name):
    return name.replace("\\", "\\\\").replace("/", "\\/")


def join_path(parts):
    return "/".join(escape(p) for p in parts)


def split_path(path):
    parts, cur, i = [], [], 0
    while i < len(path):
        c = path[i]
        if c == "\\" and i + 1 < len(path):
            cur.append(path[i + 1])
            i += 2
            continue
        if c == "/":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return [p for p in parts if p != ""]


# ---- tree access --------------------------------------------------------------------------------


def _category(browser, key):
    for k, label in CATEGORIES:
        if k == key:
            value = getattr(browser, k, None)
            if value is None:
                break
            if k == "user_folders":
                return _Folder(label, value)
            return value
    available = ", ".join(k for k, _ in CATEGORIES if getattr(browser, k, None) is not None)
    raise BridgeError("not_found", "Unknown browser category %r. Categories: %s." % (key, available))


def _children(item):
    if isinstance(item, _Folder):
        return item.children
    it = getattr(item, "iter_children", None)
    if it is not None:
        return it() if callable(it) else it
    return item.children


def _flag(item, name):
    try:
        return bool(getattr(item, name))
    except Exception:
        return False


def _entry(item, parts):
    out = {"name": item.name, "path": join_path(parts), "folder": _flag(item, "is_folder"), "loadable": _flag(item, "is_loadable")}
    if _flag(item, "is_device"):
        out["device"] = True
    return out


def _resolve(browser, path):
    """Generator: walk `path` and return (item, parts)."""
    parts = split_path(path) if isinstance(path, str) else None
    if not parts:
        raise BridgeError("bad_args", "path must start with a category, e.g. 'instruments/Operator'.")
    item = _category(browser, parts[0])
    walked = [parts[0]]
    for name in parts[1:]:
        exact, folded = None, []
        seen = 0
        for child in _children(item):
            seen += 1
            child_name = child.name
            if child_name == name:
                exact = child
                break
            if child_name.lower() == name.lower():
                folded.append(child)
            if seen % YIELD_EVERY == 0:
                yield
        if exact is None and len(folded) == 1:
            exact = folded[0]
        if exact is None:
            raise BridgeError("not_found", "'%s' not found in %s. Use browse_browser on that path to see what's there." % (name, join_path(walked)))
        item = exact
        walked.append(exact.name)
    return item, walked


# ---- commands -----------------------------------------------------------------------------------


@command()
def browse_browser(ctx, path="", offset=0, limit=100):
    browser = ctx.browser
    offset = require_int(offset, "offset")
    limit = require_int(limit, "limit")
    if not path:
        roots = []
        for key, label in CATEGORIES:
            if getattr(browser, key, None) is not None:
                roots.append({"name": label, "path": key, "folder": True, "loadable": False})
        return {"path": "", "total": len(roots), "items": roots}
    item, parts = yield from _resolve(browser, path)
    items, total = [], 0
    for child in _children(item):
        if offset <= total < offset + limit:
            items.append(_entry(child, parts + [child.name]))
        total += 1
        if total % YIELD_EVERY == 0:
            yield
    out = {"path": join_path(parts), "total": total, "offset": offset, "items": items}
    if offset + limit < total:
        out["more"] = True
    return out


@command()
def search_browser(ctx, query, categories=None, max_results=30, max_depth=8, time_limit=15.0):
    """Breadth-first name search; every word in `query` must appear in the name."""
    browser = ctx.browser
    terms = [t for t in str(query).lower().split() if t]
    if not terms:
        raise BridgeError("bad_args", "query is empty.")
    max_results = require_int(max_results, "max_results")
    max_depth = require_int(max_depth, "max_depth")
    time_limit = min(require_number(time_limit, "time_limit"), 45.0)
    if categories is None:
        keys = [k for k, _ in CATEGORIES if getattr(browser, k, None) is not None]
    elif isinstance(categories, list) and categories:
        keys = [str(c) for c in categories]
    else:
        raise BridgeError("bad_args", "categories must be a non-empty list of category keys.")

    pending = collections.deque((_category(browser, k), [k], 0) for k in keys)
    results, visited, started = [], 0, time.monotonic()
    stopped = None
    while pending and stopped is None:
        item, parts, depth = pending.popleft()
        for child in _children(item):
            visited += 1
            name = child.name
            child_parts = parts + [name]
            lowered = name.lower()
            if all(t in lowered for t in terms):
                results.append(_entry(child, child_parts))
                if len(results) >= max_results:
                    stopped = "max_results"
                    break
            if depth + 1 < max_depth and (_flag(child, "is_folder") or _flag(child, "is_device")):
                pending.append((child, child_parts, depth + 1))
            if visited % YIELD_EVERY == 0:
                if time.monotonic() - started > time_limit:
                    stopped = "time_limit"
                    break
                yield

    q = " ".join(terms)
    results.sort(key=lambda e: (_rank(e["name"], q), not e["loadable"]))
    out = {"query": query, "results": results, "visited": visited, "seconds": round(time.monotonic() - started, 2)}
    if stopped:
        out["truncated"] = stopped
    elif not pending:
        out["complete"] = True
    return out


@command()
def load_browser_item(ctx, path, track_index=None, track_type="track", clip_slot_index=None):
    """Select the track (and clip slot), load the item, and wait for Live to show the result.

    A sample from a user folder going into an audio clip slot is created from its file, in one
    undo step (see the module docstring).
    """
    song = ctx.song
    browser = ctx.browser
    track = get_track(song, track_index, track_type)
    item, parts = yield from _resolve(browser, path)
    if not _flag(item, "is_loadable"):
        raise BridgeError("invalid_state", "'%s' can't be loaded (it's a folder or a category). Browse into it." % join_path(parts))
    is_sample = item.name.lower().endswith(AUDIO_EXTENSIONS)
    slot = None
    if clip_slot_index is not None:
        if track_type != "track":
            raise BridgeError("bad_args", "clip_slot_index only applies to regular tracks.")
        slot = get_clip_slot(track, clip_slot_index)
        if slot.has_clip:
            raise BridgeError("invalid_state", "Slot %d on '%s' already has clip '%s'." % (clip_slot_index, track.name, slot.clip.name))
    elif is_sample and track_type == "track" and not track.has_midi_input:
        raise BridgeError("bad_args", "Pass clip_slot_index to load a sample onto audio track '%s' as a clip." % track.name)

    before = _snapshot(track, slot)
    tempo = song.tempo
    song.view.selected_track = track
    if slot is not None:
        song.view.highlighted_clip_slot = slot
    file_path = _sample_file(browser, parts) if is_sample and slot is not None and not track.has_midi_input else None
    if file_path:
        with ctx.undo_step():
            slot.create_audio_clip(file_path)
    else:
        browser.load_item(item)  # records its own undo step

    changed = False
    for _ in range(LOAD_WAIT_TICKS):
        if _snapshot(track, slot) != before:
            changed = True
            break
        yield WAIT

    out = {
        "loaded": item.name,
        "path": join_path(parts),
        "track": track.name,
        "changed": changed,
        "devices": [device_brief(d, i) for i, d in enumerate(track.devices)],
    }
    if slot is not None and slot.has_clip:
        out["clip"] = clip_summary(slot.clip, clip_slot_index, notes=False)
    if not changed:
        out["note"] = "Live hasn't shown a change yet. Plugins can take a few seconds; check get_track_info."
    if song.tempo != tempo:
        out["tempo"] = {"from": rnd(tempo, 3), "to": rnd(song.tempo, 3)}
        out["note"] = "Live set the tempo from this loop because it's the first clip in the set. That's a second undo step: undo twice to remove the clip."
    return out


def _sample_file(browser, parts):
    """The file behind a sample in a user folder, or None.

    The folder's uri holds its location ("userfolder:/Volumes/HD/Samples"). Samples in Live's
    library have no file path (their uri is "query:Samples#FileId_507").
    """
    if parts[0] != "user_folders" or len(parts) < 3:
        return None
    folder = next((f for f in _category(browser, "user_folders").children if f.name == parts[1]), None)
    uri = str(getattr(folder, "uri", ""))
    if not uri.startswith("userfolder:"):
        return None
    path = os.path.join(urllib.parse.unquote(uri[len("userfolder:") :].split("#", 1)[0]), *parts[2:])
    return path if os.path.isfile(path) else None


def _snapshot(track, slot):
    names = tuple(d.name for d in track.devices)
    if slot is None:
        return names
    return names, slot.has_clip


def _rank(name, query):
    base = os.path.splitext(name)[0].lower()
    if base == query:
        return 0
    if base.startswith(query):
        return 1
    return 2
