"""sampleindex: build and query the sample library index.

uv run --project tools/sampleindex sampleindex scan          # add new and changed files
uv run --project tools/sampleindex sampleindex stats         # what's indexed
uv run --project tools/sampleindex sampleindex search "dark kick" --kind oneshot
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import config, search


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sampleindex", description="Index sample folders for AbletonConnect.")
    parser.add_argument(
        "--index",
        type=Path,
        help="Index file (default: from config, or ~/Library/Application Support/AbletonConnect/samples.db).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="Analyze new and changed files in the sample folders.")
    p.add_argument("--folder", action="append", help="Scan this folder instead of the configured ones (repeatable).")
    p.add_argument("--jobs", type=int, help=f"Worker processes (default {_default_jobs()}).")
    p.add_argument("--force", action="store_true", help="Re-analyze every file.")
    p.add_argument("--retry-failed", action="store_true", help="Try files that failed last time again.")

    sub.add_parser("stats", help="Show the indexed folders and counts.")

    p = sub.add_parser("search", help="Search the index, like the search_samples MCP tool.")
    p.add_argument("text", nargs="?", help='Words to match, e.g. "dark kick".')
    p.add_argument("--kind", choices=["loop", "oneshot"])
    p.add_argument("--bpm", type=float)
    p.add_argument("--key")
    p.add_argument("--compatible", action="store_true", help="Also match keys that mix well with --key.")
    p.add_argument("--brightness", choices=["dark", "mid", "bright"])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true", help="Print the raw result.")

    args = parser.parse_args(argv)
    try:
        cfg = config.load()
        index = args.index or config.index_path(cfg)
        if args.command == "scan":
            return _scan(args, cfg, index)
        if args.command == "stats":
            return _stats(index)
        return _search(args, index)
    except (config.ConfigError, search.SearchError) as e:
        print(e, file=sys.stderr)
        return 1


def _default_jobs() -> int:
    from .scan import default_jobs

    return default_jobs()


def _scan(args: argparse.Namespace, cfg: dict, index: Path) -> int:
    try:
        from . import scan
    except ImportError as e:  # librosa is missing
        print(
            f"Analysis needs librosa ({e}). Run this through uv: uv run --project tools/sampleindex sampleindex scan",
            file=sys.stderr,
        )
        return 1
    if args.folder:
        folders = [config.make_folder(f) for f in args.folder]
    else:
        folders = config.sample_folders(cfg)
        if not folders:
            print(
                f"No sample folders configured. Add them to {config.CONFIG_PATH}, for example:\n"
                '  {"sample_folders": ["/Volumes/Samples Drive/Samples"]}\n'
                "or pass --folder PATH.",
                file=sys.stderr,
            )
            return 1
    jobs = args.jobs or scan.default_jobs()
    progress = _Progress(jobs)
    try:
        report = scan.scan(
            folders,
            index,
            jobs=jobs,
            force=args.force,
            retry_failed=args.retry_failed,
            forget_others=not args.folder,
            progress=progress,
            log=print,
        )
    except ImportError as e:
        print(
            f"Analysis needs librosa ({e}). Run this through uv: uv run --project tools/sampleindex sampleindex scan",
            file=sys.stderr,
        )
        return 1
    progress.finish()
    analyzed = sum(f.analyzed for f in report.folders)
    failed = sum(f.failed for f in report.folders)
    if report.forgotten:
        print(f"Removed {report.forgotten} files from folders that are no longer configured.")
    print(f"Analyzed {analyzed} files ({failed} failed) in {_duration(report.seconds)}.")
    if failed:
        print('  Failed files are skipped until they change. Use "sampleindex scan --retry-failed" to try them again.')
    if report.interrupted:
        print("Stopped early. What finished is saved; run the scan again to continue.")
    if report.crashed:
        print(
            "An analysis process crashed, probably on a damaged file. What finished is saved; run the scan again. "
            "If it keeps happening, run with --jobs 1 to see which file it is."
        )
    with_counts = search.overview(index)
    print(f"Index: {index} ({with_counts['files']} files)")
    return 130 if report.interrupted else 1 if report.crashed else 0


class _Progress:
    """One updating status line on a terminal, an occasional line otherwise."""

    def __init__(self, jobs: int) -> None:
        self.jobs, self.started, self.last, self.shown = jobs, None, 0.0, False
        self.tty = sys.stdout.isatty()

    def __call__(self, done: int, total: int) -> None:
        now = time.monotonic()
        if self.started is None:
            self.started = self.last = now
        if done == 0 or (done != total and now - self.last < (0.5 if self.tty else 10)):
            return
        self.last = now
        rate = done / max(now - self.started, 1e-6)
        left = (total - done) / rate if rate > 0 else 0
        line = f"  Analyzing with {self.jobs} processes: {done}/{total} ({100 * done // total}%), {rate:.0f} files/s"
        if done < total:
            line += f", about {_duration(left)} left"
        print(("\r" + line.ljust(90)) if self.tty else line, end="" if self.tty else "\n", flush=True)
        self.shown = True

    def finish(self) -> None:
        if self.tty and self.shown:
            print()


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"


def _stats(index: Path) -> int:
    info = search.overview(index)
    print(f"Index: {info['index']}")
    print(f"{info['files']} files, last updated {info.get('updated', 'never')}")
    for f in info["folders"]:
        state = "" if f["mounted"] else "  (not mounted)"
        print(f"  {f['path']}{state}")
        print(f"    {f['loops']} loops, {f['oneshots']} one-shots, {f['failed']} failed; in Live: {f['browser']}")
    return 0


def _search(args: argparse.Namespace, index: Path) -> int:
    result = search.search(
        index,
        text=args.text,
        kind=args.kind,
        bpm=args.bpm,
        key=args.key,
        compatible_keys=args.compatible,
        brightness=args.brightness,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"{result['total']} matches")
    for r in result["results"]:
        facts = [r["kind"], f"{r['length']}s"]
        if "bpm" in r:
            facts.append(f"{r['bpm']:g} BPM" + ("?" if r.get("bpm_estimated") else ""))
        for field in ("key", "note"):
            if field in r:
                facts.append(r[field])
        if "brightness_hz" in r:
            facts.append(f"{r['brightness_hz']} Hz")
        print(f"  {r['name']}  [{', '.join(facts)}]")
        print(f"      {r['browser_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
