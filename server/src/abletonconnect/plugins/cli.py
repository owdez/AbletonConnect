"""abletonconnect-scan-plugins: build ~/.config/abletonconnect/plugins.json.

    uv run --project server abletonconnect-scan-plugins            disk + Live (asks first)
    uv run --project server abletonconnect-scan-plugins --no-live  disk only
    ... --only "SLVC"     capture only plugins whose name contains this (again, even if captured)
    ... --flag "Name=why" record what a look at the plugin's window showed (no license, missing content)
    ... --force           capture every plugin again
    ... --screenshots DIR save a picture of every window a load opens, for review

With Live, run it on a new, empty set: each plugin loads on a scratch track that's deleted after.
Plugins already captured at the same version are skipped. Your notes in plugin_roles.yaml are
never touched (a draft is written once if the file doesn't exist).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ..client import LiveClient, LiveError
from ..config import WINDOWS
from . import SCAN_ON_WINDOWS, disk, index, live
from .roles_seed import seed_roles


def _print(msg: str = "") -> None:
    print(msg, flush=True)


async def scan(args: argparse.Namespace) -> int:
    _print("Reading plugin bundles and auval...")
    installs = disk.scan_disk(auval_text=disk.run_auval())
    entries = index.merge(installs)
    _print(f"  {len(installs)} installs, {len(entries)} plugins")
    out_path = Path(args.index).expanduser() if args.index else index.index_path()
    previous = index.load(out_path) if out_path.exists() else None
    result = {"version": index.INDEX_VERSION, "scanned_at": index.now(), "plugins": entries}

    if args.no_live:
        if previous:  # keep what Live said last time
            old = {index.norm(e["name"]): e for e in previous.get("plugins", [])}
            for entry in entries:
                for key in ("live", "loadable", "load_path", "missing"):
                    if key in old.get(index.norm(entry["name"]), {}):
                        entry[key] = old[index.norm(entry["name"])][key]
            result["live"] = previous.get("live")
        index.carry_over(entries, previous)
    else:
        client = LiveClient(default_timeout=20)
        try:
            items = await live.browser_plugins(client)
            summary = index.match_live(entries, items)
            hello = client.info or {}
            result["live"] = {"version": hello.get("live_version"), **summary}
            formats = ", ".join(summary["formats"]) or "none"
            _print(f"Live {hello.get('live_version')} lists {summary['items']} plugins ({formats})")
            index.carry_over(entries, previous)
            only = [o.lower() for o in args.only or []]
            todo = [e for e in entries if e.get("loadable") and (args.force or only or "captured" not in e)]
            if only:
                todo = [e for e in todo if any(o in e["name"].lower() for o in only)]
            if todo and not args.yes:
                answer = input(f"Load {len(todo)} plugin(s) one by one on a scratch track in the open Live set? [y/N] ")
                if answer.strip().lower() not in ("y", "yes"):
                    todo = []
            if todo:
                await live.remove_scratch_tracks(client)
            shots = Path(args.screenshots).expanduser() if args.screenshots else None
            for n, entry in enumerate(todo, 1):
                _print(f"  [{n}/{len(todo)}] {entry['load_path']}")
                for key in index.CAPTURE_KEYS:
                    entry.pop(key, None)
                try:
                    entry.update(
                        await live.capture(client, entry, timeout=args.timeout, settle=args.settle, shots=shots)
                    )
                except live.ScanAborted as e:
                    entry["needs_attention"] = str(e)
                    _print(f"    {e} Saving what's done; restart Live and run this again to continue.")
                    break
                note = entry.get("needs_attention")
                if note:
                    _print(f"    needs attention: {note}")
                else:
                    _print(f"    {entry['parameter_count']} parameters, {len(entry.get('programs', []))} programs")
                index.save(result, out_path)  # progress survives a crash
        except LiveError as e:
            _print(f"Live: {e}")
            _print("Run with --no-live for a disk-only index, or open Live and try again.")
            index.carry_over(entries, previous)
        finally:
            await client.close()

    for flag in args.flag or []:
        name, _, reason = flag.partition("=")
        try:
            entry = index.find(entries, name.strip())
        except LookupError as e:
            _print(str(e))
            continue
        if reason.strip() not in (entry.get("needs_attention") or ""):
            entry["needs_attention"] = " ".join(filter(None, [entry.get("needs_attention"), reason.strip()]))
    saved = index.save(result, out_path)
    roles = seed_roles(entries)
    _print()
    _print(f"Wrote {saved}")
    if roles:
        _print(f"Wrote draft roles to {roles}: confirm or edit them.")
    loadable = [e for e in entries if e.get("loadable")]
    attention = [e for e in entries if e.get("needs_attention")]
    _print(f"{len(entries)} plugins, {len(loadable)} loadable in Live, {len(attention)} need attention")
    for e in attention:
        _print("  {}: {}".format(e["name"], e["needs_attention"]))
    return 0


def main() -> None:
    if WINDOWS:
        sys.exit(SCAN_ON_WINDOWS)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-live", action="store_true", help="read the disk only")
    parser.add_argument("--only", action="append", help="capture only plugins whose name contains this (repeatable)")
    parser.add_argument("--force", action="store_true", help="capture every plugin again")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for each plugin (default 30)")
    parser.add_argument("--settle", type=float, default=1.5, help="seconds to let a plugin start before reading it")
    parser.add_argument("--screenshots", help="folder for pictures of the windows each load opens")
    parser.add_argument(
        "--flag",
        action="append",
        metavar="NAME=REASON",
        help="mark a plugin as needing attention, e.g. after looking at its window (kept until it's captured again)",
    )
    parser.add_argument("--index", help="write here instead of ~/.config/abletonconnect/plugins.json")
    parser.add_argument("--yes", action="store_true", help="don't ask before loading plugins")
    sys.exit(asyncio.run(scan(parser.parse_args())))
