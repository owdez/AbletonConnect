"""Find audio files in the sample folders and keep the index in step with them.

Only new or changed files (by size and modification time) are analyzed, so a re-scan takes
seconds. A folder that isn't there, such as one on an unmounted drive, is skipped and its entries
are kept. Analysis runs in worker processes; results are committed in batches, so a scan that's
interrupted keeps what it finished.
"""

from __future__ import annotations

import os
import signal
import time
import warnings
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

from . import db, names
from .config import SampleFolder

AUDIO_EXTENSIONS = frozenset({".wav", ".wave", ".aif", ".aiff", ".aifc", ".flac", ".mp3", ".ogg", ".m4a"})
COMMIT_EVERY = 50
THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS")

Analyzer = Callable[[str, str], dict]
Progress = Callable[[int, int], None]


class WorkerCrashed(Exception):
    """An analysis process died, most likely on a damaged file that crashed the audio decoder."""


@dataclass
class FolderReport:
    path: str
    mounted: bool
    found: int = 0  # audio files on disk
    analyzed: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: int = 0
    kept_offline: int = 0  # entries kept because the folder isn't mounted


@dataclass
class ScanReport:
    folders: list[FolderReport]
    forgotten: int = 0  # entries removed because their folder left the config
    seconds: float = 0.0
    interrupted: bool = False
    crashed: bool = False  # a worker process died; what finished before that is saved


def walk(root: str) -> Iterator[tuple[str, int, int]]:
    """(rel, size, mtime_ns) for each audio file under root. Hidden files and folders are skipped,
    and symlinked folders aren't followed."""
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for filename in sorted(filenames):
            if filename.startswith(".") or os.path.splitext(filename)[1].lower() not in AUDIO_EXTENSIONS:
                continue
            full = os.path.join(dirpath, filename)
            try:
                st = os.stat(full)
            except OSError:
                continue
            yield os.path.relpath(full, root).replace(os.sep, "/"), st.st_size, st.st_mtime_ns


def default_jobs() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def _init_worker() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C is the parent's to handle
    warnings.simplefilter("ignore")
    for var in THREAD_VARS:
        os.environ[var] = "1"  # one process per core already; nested BLAS threads only thrash


def scan(
    folders: list[SampleFolder],
    index: Path,
    *,
    jobs: int | None = None,
    force: bool = False,
    retry_failed: bool = False,
    forget_others: bool = True,
    analyzer: Analyzer | None = None,
    analyzer_version: int | None = None,
    progress: Progress | None = None,
    log: Callable[[str], None] = lambda s: None,
) -> ScanReport:
    """Bring the index up to date. `forget_others` drops folders that aren't in `folders`."""
    if analyzer is None:
        from . import analyze  # imports librosa; only the scanner needs it

        analyzer, analyzer_version = analyze.analyze_file, analyze.ANALYZER_VERSION
    version = analyzer_version or 0
    started = time.monotonic()
    conn = db.connect_writer(index)
    report = ScanReport(folders=[])
    try:
        if forget_others:
            report.forgotten = db.remove_roots_except(conn, [f.path for f in folders])
            conn.commit()

        todo: list[tuple[int, str, str, int, int]] = []  # (root_id, root path, rel, size, mtime_ns)
        for folder in folders:
            mounted = os.path.isdir(folder.path)
            root_id = db.upsert_root(conn, folder, mounted)
            known = db.existing(conn, root_id)
            fr = FolderReport(path=folder.path, mounted=mounted)
            report.folders.append(fr)
            if not mounted:
                fr.kept_offline = len(known)
                log(f"{folder.path} isn't there (drive not mounted?). Keeping its {len(known)} indexed files.")
                continue
            log(f"Scanning {folder.path}")
            seen = set()
            for rel, size, mtime_ns in walk(folder.path):
                fr.found += 1
                seen.add(rel)
                old = known.get(rel)
                fresh = old is not None and old[1] == size and old[2] == mtime_ns and old[3] == version
                if fresh and not force and not (retry_failed and old[4]):
                    fr.unchanged += 1
                else:
                    todo.append((root_id, folder.path, rel, size, mtime_ns))
            gone = [old[0] for rel, old in known.items() if rel not in seen]
            if gone and fr.found == 0:
                log(f"  Found no audio files, so keeping its {len(gone)} indexed files. Is the drive fully mounted?")
            elif gone:
                db.delete_samples(conn, gone)
                fr.removed = len(gone)
            conn.commit()
            log(
                f"  {fr.found} audio files: {fr.found - fr.unchanged} to analyze, {fr.unchanged} unchanged, {fr.removed} removed"
            )

        by_root = {fr.path: fr for fr in report.folders}
        try:
            for (root_id, root, rel, size, mtime_ns), result in _run(analyzer, todo, jobs, progress):
                db.write_sample(conn, root_id, rel, size, mtime_ns, version, result, names.parse(rel))
                fr = by_root[root]
                fr.analyzed += 1
                if result.get("error"):
                    fr.failed += 1
                if fr.analyzed % COMMIT_EVERY == 0:
                    conn.commit()
        except KeyboardInterrupt:
            report.interrupted = True
        except WorkerCrashed:
            report.crashed = True
        db.set_meta(conn, "scanned_at", str(time.time()))
        conn.commit()
    finally:
        conn.close()
    report.seconds = time.monotonic() - started
    return report


def _run(analyzer: Analyzer, todo: list, jobs: int | None, progress: Progress | None) -> Iterator[tuple[tuple, dict]]:
    """Analyze each item, yielding (item, result) as results arrive."""
    total, done = len(todo), 0
    jobs = jobs or default_jobs()
    if total == 0:
        return
    if progress:
        progress(0, total)
    if jobs == 1 or total == 1:
        for item in todo:
            yield item, analyzer(os.path.join(item[1], item[2]), item[2])
            done += 1
            if progress:
                progress(done, total)
        return
    pending: dict[Future, tuple] = {}
    items = iter(todo)
    with ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker) as pool:
        try:
            while True:
                while len(pending) < jobs * 4:  # keep the workers busy without queueing everything
                    item = next(items, None)
                    if item is None:
                        break
                    pending[pool.submit(analyzer, os.path.join(item[1], item[2]), item[2])] = item
                if not pending:
                    break
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    item = pending.pop(future)
                    try:
                        result = future.result()
                    except BrokenProcessPool as e:
                        raise WorkerCrashed() from e
                    except Exception as e:  # e.g. a result that didn't pickle
                        result = {"error": f"{type(e).__name__}: {e}"[:300]}
                    yield item, result
                    done += 1
                    if progress:
                        progress(done, total)
        except BrokenProcessPool as e:  # from submit() once a worker has died
            pool.shutdown(wait=True, cancel_futures=True)
            raise WorkerCrashed() from e
        except BaseException:
            pool.shutdown(wait=True, cancel_futures=True)
            raise
