"""The index: one SQLite file with a full-text table. Stdlib only.

The scanner writes it (scan.py). The MCP server only reads it (search.py), with a read-only
connection, so a scan can run while Claude searches.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .config import SampleFolder
from .names import NameInfo

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roots (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,      -- sample folder on disk
    browser TEXT NOT NULL,          -- the same folder in Live's browser, e.g. user_folders/Samples
    scanned_at REAL                 -- last scan that found the folder mounted
);
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    rel TEXT NOT NULL,              -- path under the root, '/'-separated
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    analyzer INTEGER NOT NULL,      -- analyze.ANALYZER_VERSION that produced the row
    error TEXT,                     -- why analysis failed; other fields are then mostly empty
    duration REAL,
    sample_rate INTEGER,
    channels INTEGER,
    kind TEXT,                      -- 'loop' | 'oneshot'
    bpm REAL,                       -- loops only
    bpm_source TEXT,                -- 'name' | 'audio'
    key_pc INTEGER,                 -- from the file name; pitch class 0-11
    key_mode TEXT,                  -- 'major' | 'minor' | NULL when the name gives only a tonic
    note_pc INTEGER,                -- pitched one-shots: root note pitch class
    note_midi INTEGER,              -- ...and its MIDI number when the audio gave one
    brightness REAL,                -- loudness-weighted spectral centroid, Hz
    tags TEXT NOT NULL DEFAULT '',
    UNIQUE (root_id, rel)
);
CREATE INDEX IF NOT EXISTS samples_kind_bpm ON samples (kind, bpm);
CREATE VIRTUAL TABLE IF NOT EXISTS samples_fts USING fts5(
    name, folders, tags, tokenize = 'unicode61 remove_diacritics 2'
);
"""

COLUMNS = (
    "duration",
    "sample_rate",
    "channels",
    "kind",
    "bpm",
    "bpm_source",
    "key_pc",
    "key_mode",
    "note_pc",
    "note_midi",
    "brightness",
)


class IndexMissing(Exception):
    """There's no index file yet."""


def connect_writer(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    version = _schema_version(conn)
    if version not in (None, SCHEMA_VERSION):
        # The index is a cache of the sample folders, so rebuild rather than migrate.
        conn.executescript(
            "DROP TABLE IF EXISTS samples_fts; DROP TABLE IF EXISTS samples; DROP TABLE IF EXISTS roots;"
        )
    conn.executescript(SCHEMA)
    set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()
    return conn


def connect_reader(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise IndexMissing(str(path))
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    if _schema_version(conn) != SCHEMA_VERSION:
        conn.close()
        raise IndexMissing(str(path))
    return conn


def _schema_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


# ---- roots --------------------------------------------------------------------------------------


def upsert_root(conn: sqlite3.Connection, folder: SampleFolder, mounted: bool) -> int:
    scanned = time.time() if mounted else None
    row = conn.execute(
        """INSERT INTO roots (path, browser, scanned_at) VALUES (?, ?, ?)
           ON CONFLICT (path) DO UPDATE SET browser = excluded.browser,
               scanned_at = COALESCE(excluded.scanned_at, roots.scanned_at)
           RETURNING id""",
        (folder.path, folder.browser, scanned),
    ).fetchone()
    return int(row[0])


def remove_roots_except(conn: sqlite3.Connection, keep: list[str]) -> int:
    """Forget folders that are no longer configured. Returns how many samples went with them."""
    marks = ",".join("?" * len(keep)) or "NULL"
    ids = [r[0] for r in conn.execute(f"SELECT id FROM roots WHERE path NOT IN ({marks})", keep)]
    removed = 0
    for root_id in ids:
        sample_ids = [r[0] for r in conn.execute("SELECT id FROM samples WHERE root_id = ?", (root_id,))]
        delete_samples(conn, sample_ids)
        removed += len(sample_ids)
        conn.execute("DELETE FROM roots WHERE id = ?", (root_id,))
    return removed


# ---- samples ------------------------------------------------------------------------------------


def existing(conn: sqlite3.Connection, root_id: int) -> dict[str, tuple[int, int, int, int, str | None]]:
    """rel -> (id, size, mtime_ns, analyzer, error) for one root."""
    rows = conn.execute("SELECT rel, id, size, mtime_ns, analyzer, error FROM samples WHERE root_id = ?", (root_id,))
    return {r[0]: (r[1], r[2], r[3], r[4], r[5]) for r in rows}


def write_sample(
    conn: sqlite3.Connection,
    root_id: int,
    rel: str,
    size: int,
    mtime_ns: int,
    analyzer: int,
    result: dict,
    info: NameInfo,
) -> int:
    values = [result.get(c) for c in COLUMNS]
    tags = " ".join(info.tags)
    row = conn.execute(
        f"""INSERT INTO samples (root_id, rel, size, mtime_ns, analyzer, error, tags, {", ".join(COLUMNS)})
            VALUES (?, ?, ?, ?, ?, ?, ?, {", ".join("?" * len(COLUMNS))})
            ON CONFLICT (root_id, rel) DO UPDATE SET
                size = excluded.size, mtime_ns = excluded.mtime_ns, analyzer = excluded.analyzer,
                error = excluded.error, tags = excluded.tags,
                {", ".join(f"{c} = excluded.{c}" for c in COLUMNS)}
            RETURNING id""",
        [root_id, rel, size, mtime_ns, analyzer, result.get("error"), tags, *values],
    ).fetchone()
    sample_id = int(row[0])
    filename = rel.rsplit("/", 1)[-1]
    stem = filename.rsplit(".", 1)[0]
    folders = rel.rsplit("/", 1)[0] if "/" in rel else ""
    conn.execute("DELETE FROM samples_fts WHERE rowid = ?", (sample_id,))
    conn.execute(
        "INSERT INTO samples_fts (rowid, name, folders, tags) VALUES (?, ?, ?, ?)",
        (sample_id, f"{stem} {' '.join(info.name_words)}", f"{folders} {' '.join(info.folder_words)}", tags),
    )
    return sample_id


def delete_samples(conn: sqlite3.Connection, ids: list[int]) -> None:
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM samples_fts WHERE rowid IN ({marks})", chunk)
        conn.execute(f"DELETE FROM samples WHERE id IN ({marks})", chunk)
