"""Smoke test against a running Live with LiveBridge loaded.

Run it on a new, empty Live set; it adds two tracks named "LB smoke ...":

    uv run --project server python scripts/smoke_test.py [--yes] [--port 9878]

Every tool's bridge command is exercised. The MIDI track is muted before its
clip is launched, so nothing is audible. Close the set without saving when done.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import time
from typing import Any

from abletonconnect.client import LiveClient, LiveCommandError, LiveError

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[0m",
)


class Smoke:
    def __init__(self, client: LiveClient):
        self.c = client
        self.failures: list[str] = []
        self.skipped: list[str] = []

    async def run(
        self, cmd: str, mutating: bool = True, timeout: float | None = None, **args: Any
    ) -> Any:
        return await self.c.request(cmd, args, mutating=mutating, timeout=timeout)

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
        if not ok:
            self.failures.append(label)
        return ok

    def skip(self, label: str, why: str) -> None:
        print(f"  {YELLOW}SKIP{RESET}  {label}  {DIM}{why}{RESET}")
        self.skipped.append(label)

    async def step(self, label: str, coro) -> Any:
        try:
            return await coro
        except LiveError as e:
            self.check(label, False, f"{type(e).__name__}: {e}")
            return None


def rejects_http(port: int) -> bool:
    """A browser-style request must be dropped without a reply."""
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.sendall(
            b'POST / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n{"id":1,"cmd":"set_tempo","args":{"bpm":61}}\n'
        )
        s.settimeout(3)
        try:
            return s.recv(1) == b""
        except ConnectionResetError:
            return True


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--yes", action="store_true", help="don't ask before changing the open set"
    )
    parser.add_argument("--port", type=int, default=None)
    opts = parser.parse_args()

    client = LiveClient(port=opts.port)
    t = Smoke(client)
    print(f"LiveBridge smoke test on 127.0.0.1:{client.port}\n")

    # ---- connection ----
    try:
        info = await t.run("get_session_info", mutating=False)
    except LiveError as e:
        print(f"{RED}Can't reach LiveBridge:{RESET} {e}")
        return 2
    print(
        f"Connected: Live {client.info['live_version']}, LiveBridge {client.info['script_version']}, protocol {client.info['protocol']}"
    )
    print(
        f"Open set: {len(info['tracks'])} tracks, {len(info['returns'])} returns, {info['scenes']} scenes, {info['tempo']} BPM\n"
    )
    if not opts.yes:
        answer = input(
            "This adds two tracks and changes tempo/mixer on the open set. Continue? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            return 1

    print("Protocol")
    t.check("browser-style HTTP request is dropped", rejects_http(client.port))
    samples = []
    for _ in range(10):
        started = time.perf_counter()
        await t.run("ping", mutating=False)
        samples.append((time.perf_counter() - started) * 1000)
    t.check(
        "ping round trip",
        max(samples) < 500,
        f"avg {sum(samples) / len(samples):.0f} ms, max {max(samples):.0f} ms",
    )
    try:
        await t.run("get_track_info", mutating=False, track_index=9999)
        t.check("bad index returns a clear error", False)
    except LiveCommandError as e:
        t.check("bad index returns a clear error", e.type == "not_found", str(e))

    print("\nTempo and undo")
    original_tempo = info["tempo"]
    out = await t.step("set_tempo", t.run("set_tempo", bpm=original_tempo + 1))
    if out:
        t.check(
            "set_tempo",
            abs(out["tempo"] - (original_tempo + 1)) < 1e-3,
            f"{out['previous']} -> {out['tempo']}",
        )
    await t.step("set_tempo again", t.run("set_tempo", bpm=original_tempo + 2))
    out = await t.step("undo", t.run("undo"))
    if out:
        now = (await t.run("get_session_info", mutating=False))["tempo"]
        t.check(
            "undo reverts one step",
            abs(now - (original_tempo + 1)) < 1e-3,
            f"tempo back to {now}",
        )
    await t.step("restore tempo", t.run("set_tempo", bpm=original_tempo))

    print("\nMIDI track, clip, notes")
    out = await t.step(
        "create_midi_track", t.run("create_midi_track", name="LB smoke MIDI")
    )
    if not out:
        return report(t)
    midi = out["created"]["index"]
    t.check("create_midi_track", out["created"]["kind"] == "midi", f"index {midi}")
    out = await t.step(
        "create_clip",
        t.run(
            "create_clip",
            track_index=midi,
            clip_slot_index=0,
            length=4.0,
            name="LB smoke clip",
        ),
    )
    if out:
        t.check("create_clip", out["clip"]["length"] == 4.0, f"{out['clip']}")
    notes = [
        {"pitch": p, "start": i * 0.5, "duration": 0.45, "velocity": 90 + i}
        for i, p in enumerate([60, 64, 67, 72, 67, 64, 60, 55])
    ]
    notes[1]["probability"] = 0.5
    out = await t.step(
        "add_notes",
        t.run("add_notes", track_index=midi, clip_slot_index=0, notes=notes),
    )
    if out:
        t.check(
            "add_notes",
            out["added"] == 8 and out["clip"]["note_count"] == 8,
            f"note_count {out['clip']['note_count']}",
        )
    out = await t.step(
        "get_clip_notes",
        t.run("get_clip_notes", mutating=False, track_index=midi, clip_slot_index=0),
    )
    if out:
        first = out["notes"][0]
        t.check(
            "get_clip_notes",
            out["count"] == 8
            and first["pitch"] == 60
            and out["notes"][1].get("probability") == 0.5,
            f"first {first}",
        )

    print("\nBrowser")
    out = await t.step("browse_browser root", t.run("browse_browser", mutating=False))
    if out:
        t.check(
            "browse_browser root",
            out["total"] > 5,
            ", ".join(i["path"] for i in out["items"]),
        )
    for path in ("plugins", "user_library", "user_folders"):
        out = await t.step(
            f"browse_browser {path}",
            t.run("browse_browser", mutating=False, path=path, limit=8),
        )
        if out:
            t.check(
                f"browse_browser {path}",
                True,
                f"{out['total']} items: "
                + ", ".join(i["name"] for i in out["items"][:8]),
            )
    out = await t.step(
        "search_browser",
        t.run(
            "search_browser",
            mutating=False,
            timeout=45,
            query="operator",
            categories=["instruments"],
        ),
    )
    instrument = None
    if out:
        hits = [r for r in out["results"] if r["loadable"]]
        t.check(
            "search_browser",
            bool(hits),
            f"{len(out['results'])} results in {out['seconds']} s, first {hits[0]['path'] if hits else None}",
        )
        instrument = hits[0]["path"] if hits else None
    if instrument:
        out = await t.step(
            "load_browser_item instrument",
            t.run("load_browser_item", timeout=20, path=instrument, track_index=midi),
        )
        if out:
            t.check(
                "load_browser_item instrument",
                out["changed"] and bool(out["devices"]),
                ", ".join(d["name"] for d in out["devices"]),
            )

    print("\nDevice parameters")
    out = await t.step(
        "get_device_parameters",
        t.run(
            "get_device_parameters", mutating=False, track_index=midi, device_index=0
        ),
    )
    if out and out["parameters"]:
        params = out["parameters"]
        t.check(
            "get_device_parameters",
            True,
            f"{out['device']['name']}: {len(params)} parameters",
        )
        continuous = next(
            (
                p
                for p in params
                if not p.get("quantized")
                and p.get("enabled", True)
                and p["max"] > p["min"]
            ),
            None,
        )
        if continuous:
            out = await t.step(
                "set_device_parameter normalized",
                t.run(
                    "set_device_parameter",
                    track_index=midi,
                    parameter=continuous["index"],
                    normalized=0.3,
                ),
            )
            if out:
                t.check(
                    "set_device_parameter normalized",
                    out["parameter"]["value"] != out["previous"]["value"]
                    or continuous["value"] == out["parameter"]["value"],
                    f"{out['parameter']['name']}: {out['previous']['display']} -> {out['parameter']['display']}",
                )
        quantized = next(
            (
                p
                for p in params
                if len(p.get("items", [])) > 1
                and p["name"] != "Device On"
                and p.get("enabled", True)
            ),
            None,
        )
        if quantized:
            target = quantized["items"][1]
            out = await t.step(
                "set_device_parameter by option name",
                t.run(
                    "set_device_parameter",
                    track_index=midi,
                    parameter=quantized["name"],
                    value=target,
                ),
            )
            if out:
                t.check(
                    "set_device_parameter by option name",
                    out["parameter"]["display"] == target,
                    f"{quantized['name']} = {out['parameter']['display']}",
                )
        else:
            t.skip("set_device_parameter by option name", "no quantized parameter")
    elif out is not None:
        t.skip("device parameters", "no device on the track")

    print("\nMixer")
    args: dict[str, Any] = {"track_index": midi, "volume_db": -6.0, "pan": -0.3}
    if info["returns"]:
        args["sends_db"] = {"0": -12.0}
    out = await t.step("set_mixer", t.run("set_mixer", **args))
    if out:
        detail = f"volume {out['volume']['display']}, pan {out['pan']['display']}"
        if out.get("sends"):
            detail += f", send A {out['sends'][0]['display']}"
        t.check(
            "set_mixer dB/pan/sends",
            out["volume"]["display"].startswith("-6.0"),
            detail,
        )
    out = await t.step(
        "set_mixer solo", t.run("set_mixer", track_index=midi, solo=True)
    )
    if out:
        t.check("set_mixer solo", out["solo"] is True)
    await t.step("set_mixer unsolo", t.run("set_mixer", track_index=midi, solo=False))
    out = await t.step(
        "set_mixer mute", t.run("set_mixer", track_index=midi, mute=True)
    )
    if out:
        t.check(
            "set_mixer mute (track stays muted for the launch test)",
            out["mute"] is True,
        )

    print("\nLaunching (muted)")
    out = await t.step(
        "fire_clip", t.run("fire_clip", track_index=midi, clip_slot_index=0)
    )
    if out:
        t.check(
            "fire_clip",
            out["clip"].get("triggered") or out["clip"]["playing"],
            f"transport playing: {out['transport_playing']}",
        )
    await asyncio.sleep(1.0)
    info_now = await t.step(
        "get_track_info", t.run("get_track_info", mutating=False, track_index=midi)
    )
    if info_now:
        t.check(
            "clip is playing",
            info_now["playing_slot"] == 0,
            f"playing_slot {info_now['playing_slot']}",
        )
    out = await t.step("stop_clip", t.run("stop_clip", track_index=midi))
    if out:
        t.check("stop_clip", True, f"playing_slot {out['playing_slot']}")
    out = await t.step("stop_playback", t.run("stop_playback"))
    if out:
        t.check("stop_playback", out["playing"] is False)
    out = await t.step("start_playback", t.run("start_playback"))
    if out:
        t.check(
            "start_playback", out["playing"] is True, f"song_time {out['song_time']}"
        )
    out = await t.step("stop_playback", t.run("stop_playback"))
    if out:
        t.check("stop_playback", out["playing"] is False)

    print("\nAudio track and samples")
    out = await t.step(
        "create_audio_track", t.run("create_audio_track", name="LB smoke audio")
    )
    if out:
        audio = out["created"]["index"]
        t.check(
            "create_audio_track", out["created"]["kind"] == "audio", f"index {audio}"
        )
        out = await t.step(
            "search_browser samples",
            t.run(
                "search_browser",
                mutating=False,
                timeout=45,
                query="kick",
                categories=["samples", "user_library"],
                max_results=10,
                max_depth=6,
            ),
        )
        sample = None
        if out:
            sample = next(
                (
                    r["path"]
                    for r in out["results"]
                    if r["loadable"]
                    and r["name"].lower().endswith((".wav", ".aif", ".aiff", ".flac"))
                ),
                None,
            )
            t.check(
                "search_browser samples",
                True,
                f"{len(out['results'])} results in {out['seconds']} s ({out.get('truncated') or 'complete'})",
            )
        if sample:
            out = await t.step(
                "load sample as clip",
                t.run(
                    "load_browser_item",
                    timeout=20,
                    path=sample,
                    track_index=audio,
                    clip_slot_index=0,
                ),
            )
            if out:
                t.check(
                    "load sample as clip",
                    "clip" in out,
                    out.get("clip", {}).get("name", "no clip"),
                )
        else:
            t.skip(
                "load sample as clip", "no kick sample found in Samples or User Library"
            )
        out = await t.step(
            "get_track_info audio",
            t.run("get_track_info", mutating=False, track_index=audio),
        )
        if out:
            t.check(
                "get_track_info audio",
                out["kind"] == "audio",
                f"{len(out['clips'])} clip(s)",
            )

    return report(t)


def report(t: Smoke) -> int:
    print()
    if t.failures:
        print(f"{RED}{len(t.failures)} failed:{RESET} " + ", ".join(t.failures))
    else:
        print(
            f"{GREEN}All checks passed{RESET}"
            + (f" ({len(t.skipped)} skipped)" if t.skipped else "")
        )
    print(
        "The open set now has 'LB smoke' tracks. Delete them or close the set without saving."
    )
    return 1 if t.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
