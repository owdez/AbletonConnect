"""What Live makes of each plugin: whether its browser lists it, and what a loaded instance exposes
(parameters with their ranges, and programs). Loads happen on a scratch track that's deleted after."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

from ..client import LiveClient, LiveError, LiveTimeout, is_live_running

SCRATCH = "LB plugin scan"
LIVE_KINDS = {"instrument": "instrument", "audio_effect": "effect", "midi_effect": "midi_effect"}
# Windows that say a plugin wants a sign-in, activation or license: never clicked, only reported.
AUTH_WORDS = ("authoriz", "activat", "sign in", "sign-in", "log in", "login", "licen", "trial", "subscri", "register")
CONSENT_WORDS = ("opt-in", "opt in", "analytics", "consent", "privacy")
IGNORED_OWNERS = {
    "Window Server",
    "Dock",
    "Control Center",
    "Notification Center",
    "SystemUIServer",
    "Claude",
    "screencapture",
    "Spotlight",
    "loginwindow",
    "TextInputMenuAgent",
}


class ScanAborted(Exception):
    """Live crashed or stopped answering; the scan saves what it has and stops."""


# ---- Live's browser -----------------------------------------------------------------------------


async def browser_plugins(live: LiveClient) -> list[dict]:
    """Every loadable item under Live's Plug-Ins category, with its uri."""
    items: list[dict] = []

    async def walk(path: str, depth: int) -> None:
        out = await live.request("browse_browser", {"path": path, "limit": 1000}, mutating=False, timeout=30)
        for item in out["items"]:
            if item.get("loadable"):
                items.append(item)
            elif item.get("folder") and depth < 4:
                await walk(item["path"], depth + 1)

    await walk("plugins", 0)
    return items


# ---- windows ------------------------------------------------------------------------------------

_WINDOWS_JXA = """
ObjC.import('CoreGraphics');
function run() {
  const list = ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly, 0));
  const out = [];
  for (let i = 0; i < list.count; i++) {
    const w = list.objectAtIndex(i);
    const b = w.objectForKey('kCGWindowBounds');
    out.push({id: ObjC.unwrap(w.objectForKey('kCGWindowNumber')), owner: ObjC.unwrap(w.objectForKey('kCGWindowOwnerName')) || '',
      title: ObjC.unwrap(w.objectForKey('kCGWindowName')) || '', layer: ObjC.unwrap(w.objectForKey('kCGWindowLayer')),
      w: ObjC.unwrap(b.objectForKey('Width')), h: ObjC.unwrap(b.objectForKey('Height'))});
  }
  return JSON.stringify(out);
}
"""


def windows() -> list[dict]:
    """On-screen windows (macOS). Empty if they can't be listed."""
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _WINDOWS_JXA], capture_output=True, text=True, timeout=10
        )
        return [w for w in json.loads(proc.stdout or "[]") if w["w"] > 40 and w["h"] > 40]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def attention(new_windows: list[dict], parameter_count: int) -> str | None:
    """Why a plugin needs the user, from the windows its load opened and what Live could see.
    Nothing in those windows is ever clicked."""
    reasons = []
    for w in new_windows:
        title = w["title"].lower()
        if any(word in title for word in AUTH_WORDS):
            reasons.append(f"It opened '{w['title']}' ({w['owner']}), which asks for authorization.")
        elif any(word in title for word in CONSENT_WORDS):
            reasons.append(f"It opened '{w['title']}' on every load; answer it once in the plugin window.")
        elif w["owner"] not in ("Live", *IGNORED_OWNERS):
            reasons.append(f"It opened a {w['owner']} window ('{w['title']}'), probably to sign in or activate.")
    if parameter_count == 0:
        reasons.append(
            "Live sees no parameters on it, so Claude can't adjust it. Open it in Live to see why: a dialog, "
            "missing content, or a plugin that doesn't publish its parameters."
        )
    return " ".join(reasons) or None


def loaded_binary(entry: dict, fmt: str) -> str | None:
    """Which of a plugin's installs Live actually mapped, by its bundle path in `lsof` for Live.
    Matters when one format is installed twice: copies sharing a class ID load as one of them."""
    paths = [i["path"] for i in entry["formats"].get(fmt, []) if i.get("path")]
    try:
        pid = subprocess.run(["pgrep", "-x", "Live"], capture_output=True, text=True, timeout=5).stdout.split()[0]
        mapped = subprocess.run(["lsof", "-p", pid, "-Fn"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError, IndexError):
        return None
    files = [line[1:] for line in mapped.splitlines() if line.startswith("n")]
    hits = [p for p in paths if any(f.startswith(p.rstrip("/") + "/Contents/") for f in files)]
    return hits[0] if len(hits) == 1 else None


def screenshot(window: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["screencapture", "-x", "-o", "-l", str(window["id"]), str(path)], capture_output=True, timeout=15)


# ---- capturing one plugin -----------------------------------------------------------------------


def _parameter(p: dict) -> dict:
    out = {
        "index": p["index"],
        "name": p["name"],
        "min": p["min"],
        "max": p["max"],
        "default": p["value"],
        "default_display": p["display"],
    }
    for key in ("min_display", "max_display", "items", "quantized"):
        if key in p:
            out[key] = p[key]
    return out


async def _responsive(live: LiveClient, wait: float) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if is_live_running() is False:
            return False
        try:
            await live.request("ping", mutating=False, timeout=5)
            return True
        except LiveError:
            await asyncio.sleep(2)
    return False


async def remove_scratch_tracks(live: LiveClient) -> None:
    info = await live.request("get_session_info", mutating=False)
    for track in reversed(info["tracks"]):
        if track["name"] == SCRATCH and len(info["tracks"]) > 1:
            await live.request("delete_track", {"track_index": track["index"]}, mutating=True)


async def capture(
    live: LiveClient, entry: dict, timeout: float = 30.0, settle: float = 1.5, shots: Path | None = None
) -> dict:
    """Load the entry's load_path on a new scratch track and read what the plugin exposes.
    Returns fields for the index: parameters, programs, live_kind and captured, or needs_attention."""
    path = entry["load_path"]
    before = {w["id"] for w in windows()}
    started = time.monotonic()
    created = await live.request("create_midi_track", {"name": SCRATCH}, mutating=True)
    track = created["created"]["index"]
    out: dict = {}
    try:
        try:
            loaded = await live.request(
                "load_browser_item", {"path": path, "track_index": track}, mutating=True, timeout=timeout + 15
            )
        except LiveTimeout:
            if not await _responsive(live, 90):
                raise ScanAborted(f"Live stopped responding while loading {entry['name']}.") from None
            return {"needs_attention": f"Live hung for over {timeout + 15:.0f} s loading it."}
        devices = loaded["devices"]
        while not devices and time.monotonic() - started < timeout:
            await asyncio.sleep(0.5)
            devices = (await live.request("get_track_info", {"track_index": track}, mutating=False))["devices"]
        if not devices:
            return {"needs_attention": f"Live showed no device within {timeout:.0f} s of loading it."}
        await asyncio.sleep(settle)  # plugins fill in their parameter list after they load
        params = await live.request(
            "get_device_parameters",
            {"track_index": track, "device_index": 0, "limit": 2048, "ranges": True},
            mutating=False,
            timeout=60,
        )
        out["live_kind"] = LIVE_KINDS.get(params["device"].get("type", ""), params["device"].get("type"))
        out["parameters"] = [_parameter(p) for p in params["parameters"] if p["name"] != "Device On"]
        out["parameter_count"] = len(out["parameters"])
        try:
            programs = await live.request(
                "get_plugin_programs", {"track_index": track, "device_index": 0, "limit": 2000}, mutating=False
            )
            out["programs"] = [p["name"] for p in programs["programs"]]
        except LiveError:
            out["programs"] = []
        fmt = next((f for f, insts in entry["formats"].items() if any(i.get("live_path") == path for i in insts)), None)
        if fmt and len(entry["formats"][fmt]) > 1:
            out["loaded_install"] = loaded_binary(entry, fmt)
        new = [w for w in windows() if w["id"] not in before]
        reason = attention(new, out["parameter_count"])
        if reason:
            out["needs_attention"] = reason
        if shots is not None:
            for i, w in enumerate(new):
                screenshot(w, shots / f"{entry['name'].replace('/', '-')}-{i}.png")
        out["captured"] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "path": path,
            "seconds": round(time.monotonic() - started, 1),
            "windows": [{"owner": w["owner"], "title": w["title"]} for w in new],
        }
        return out
    finally:
        try:
            info = await live.request("get_session_info", mutating=False, timeout=15)
            for t in reversed(info["tracks"]):
                if t["name"] == SCRATCH and t["index"] >= track:
                    await live.request("delete_track", {"track_index": t["index"]}, mutating=True, timeout=30)
        except LiveError:
            pass
