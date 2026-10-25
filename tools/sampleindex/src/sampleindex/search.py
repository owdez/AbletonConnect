"""Search the sample index. Stdlib only, so the MCP server can use it without librosa."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import db, music
from .config import browser_path
from .names import ALIASES, LOOP, ONESHOT, SYNONYMS, TAGS, singular

BRIGHTNESS = {"dark": 1, "mid": 2, "bright": 3}  # thirds of the matching samples, darkest first
STOPWORDS = frozenset({"a", "an", "the", "some", "sample", "samples", "sound", "sounds", "with", "and", "of", "for"})
_ONESHOT_RE = re.compile(r"\bone[\s_-]?shots?\b")
_LOOP_RE = re.compile(r"\bloops?\b")
SCAN_COMMAND = 'uv run --project "{repo}/tools/sampleindex" sampleindex scan'


class SearchError(Exception):
    """Bad search arguments or an unusable index. The message says what to do."""


@dataclass
class Query:
    text: str | None = None
    kind: str | None = None
    bpm: float | None = None
    bpm_tolerance: float = 3.0
    half_double: bool = True
    key: str | None = None
    compatible_keys: bool = False
    brightness: str | None = None
    min_length: float | None = None
    max_length: float | None = None
    limit: int = 20
    offset: int = 0


@dataclass
class ParsedText:
    fts: str | None  # FTS5 query; None when no words are left
    kind: str | None = None  # from "loop" / "one shot" in the text
    brightness: str | None = None  # from "dark" / "bright" in the text


def parse_text(text: str) -> ParsedText:
    """Free text -> a full-text query. Every word must match the file name, a folder name or a tag,
    as a prefix when it's three letters or more. 'loop' and 'one shot' set the kind instead, and 'dark' and 'bright' the brightness,
    since few file names say how a sample sounds."""
    raw = text.lower()
    parsed = ParsedText(fts=None)
    if _ONESHOT_RE.search(raw):
        parsed.kind, raw = ONESHOT, _ONESHOT_RE.sub(" ", raw)
    if _LOOP_RE.search(raw):
        parsed.kind = None if parsed.kind else LOOP
        raw = _LOOP_RE.sub(" ", raw)
    terms = []
    for word in re.findall(r"[^\W_]+", raw):
        if word in STOPWORDS:
            continue
        if word in ("dark", "bright") and parsed.brightness in (None, word):
            parsed.brightness = word
            continue
        word = singular(word)
        tag = word if word in TAGS else ALIASES[word] if word in SYNONYMS else None
        term = f'"{word}"*' if len(word) > 2 else f'"{word}"'  # "bd" shouldn't match "bde130"
        terms.append(f'({term} OR tags:"{tag}")' if tag else term)
    parsed.fts = " AND ".join(terms) or None
    return parsed


def _key_filter(key: str, compatible: bool) -> tuple[str, list]:
    try:
        pc, mode = music.parse_key(key)
    except ValueError as e:
        raise SearchError(str(e)) from None
    if compatible or mode is None:
        keys = music.compatible_keys(pc, mode)
    else:
        keys = {(pc, mode)}
    tonics = sorted({k[0] for k in keys})
    exact = " OR ".join("(s.key_pc = ? AND s.key_mode = ?)" for _ in keys)
    marks = ",".join("?" * len(tonics))
    sql = f"(({exact}) OR (s.key_mode IS NULL AND s.key_pc IN ({marks})) OR (s.key_pc IS NULL AND s.note_pc IN ({marks})))"
    params = [v for k in sorted(keys) for v in k] + tonics + tonics
    return sql, params


def search(index: Path, **query) -> dict:
    """Matching samples, best first, as compact dicts with a browser_path for load_browser_item.
    Keyword arguments are the fields of Query."""
    q = Query(**query)
    if q.kind not in (None, LOOP, ONESHOT):
        raise SearchError("kind must be 'loop' or 'oneshot'.")
    if q.brightness is not None and q.brightness not in BRIGHTNESS:
        raise SearchError("brightness must be 'dark', 'mid' or 'bright'.")
    try:
        conn = db.connect_reader(index)
    except db.IndexMissing:
        raise SearchError(missing_index_message(index)) from None
    try:
        return _search(conn, q)
    except sqlite3.OperationalError as e:
        raise SearchError(f"The sample index couldn't be searched ({e}). Run the scan again to rebuild it.") from e
    finally:
        conn.close()


def _search(conn: sqlite3.Connection, q: Query) -> dict:
    parsed = parse_text(q.text) if q.text else ParsedText(fts=None)
    kind = q.kind or parsed.kind
    brightness = q.brightness or parsed.brightness
    where, params, join, rank = ["s.error IS NULL"], [], "", "0.0"
    if parsed.fts:
        join = "JOIN samples_fts ON samples_fts.rowid = s.id"
        where.append("samples_fts MATCH ?")
        params.append(parsed.fts)
        rank = "bm25(samples_fts, 10.0, 2.0, 6.0)"  # file name counts most, then tags, then folders
    if kind:
        where.append("s.kind = ?")
        params.append(kind)
    if q.bpm is not None:
        if q.half_double:
            where.append("(ABS(s.bpm - ?) <= ? OR ABS(s.bpm * 2 - ?) <= ? OR ABS(s.bpm / 2 - ?) <= ?)")
            params += [q.bpm, q.bpm_tolerance] * 3
        else:
            where.append("ABS(s.bpm - ?) <= ?")
            params += [q.bpm, q.bpm_tolerance]
    if q.key:
        sql, key_params = _key_filter(q.key, q.compatible_keys)
        where.append(sql)
        params += key_params
    if q.min_length is not None:
        where.append("s.duration >= ?")
        params.append(q.min_length)
    if q.max_length is not None:
        where.append("s.duration <= ?")
        params.append(q.max_length)

    order, order_params = ["rank", "rel"], []
    if q.bpm is not None:  # straight tempo matches before half/double-time ones, then relevance
        order = ["ABS(bpm - ?) > ?", "rank", "MIN(ABS(bpm - ?), ABS(bpm * 2 - ?), ABS(bpm / 2 - ?))", "rel"]
        order_params = [q.bpm, q.bpm_tolerance, q.bpm, q.bpm, q.bpm]
    source = f"SELECT s.*, {rank} AS rank FROM samples s {join} WHERE {' AND '.join(where)}"
    if brightness:
        source = (
            "SELECT * FROM (SELECT m.*, NTILE(3) OVER (ORDER BY brightness) AS tile "
            f"FROM ({source}) m WHERE brightness IS NOT NULL) WHERE tile = ?"
        )
        params.append(BRIGHTNESS[brightness])
    sql = f"SELECT *, COUNT(*) OVER () AS total FROM ({source}) ORDER BY {', '.join(order)} LIMIT ? OFFSET ?"
    rows = conn.execute(sql, [*params, *order_params, q.limit, q.offset]).fetchall()

    roots = {r["id"]: (r["path"], r["browser"], os.path.isdir(r["path"])) for r in conn.execute("SELECT * FROM roots")}
    out: dict = {"total": rows[0]["total"] if rows else 0, "results": [_result(row, roots) for row in rows]}
    if parsed.kind and kind == parsed.kind and not q.kind:
        out["kind_from_text"] = parsed.kind
    if parsed.brightness and not q.brightness:
        out["brightness_from_text"] = parsed.brightness
    offline = sorted({roots[row["root_id"]][0] for row in rows if not roots[row["root_id"]][2]})
    if offline:
        out["offline"] = offline
        out["note"] = (
            "Some results are on folders that aren't mounted (marked available: false). Connect the drive to load them."
        )
    out["index"] = summary(conn, roots)
    return out


def _result(row: sqlite3.Row, roots: dict) -> dict:
    path, browser, mounted = roots[row["root_id"]]
    item: dict = {
        "name": row["rel"].rsplit("/", 1)[-1],
        "kind": row["kind"],
        "length": round(row["duration"] or 0.0, 2),
    }
    if row["bpm"] is not None:
        item["bpm"] = row["bpm"]
        if row["bpm_source"] == "audio":
            item["bpm_estimated"] = True
    if row["key_pc"] is not None:
        item["key"] = music.key_name(row["key_pc"], row["key_mode"])
    if row["note_midi"] is not None:
        item["note"] = music.note_name(row["note_midi"])
    elif row["note_pc"] is not None:
        item["note"] = music.pc_name(row["note_pc"])
    if row["brightness"] is not None:
        item["brightness_hz"] = int(round(row["brightness"]))
    item["browser_path"] = browser_path(browser, row["rel"])
    if not mounted:
        item["available"] = False
    return item


def summary(conn: sqlite3.Connection, roots: dict | None = None) -> dict:
    if roots is None:
        roots = {
            r["id"]: (r["path"], r["browser"], os.path.isdir(r["path"])) for r in conn.execute("SELECT * FROM roots")
        }
    files = conn.execute("SELECT COUNT(*) FROM samples WHERE error IS NULL").fetchone()[0]
    scanned = db.get_meta(conn, "scanned_at")
    out: dict = {"files": files}
    if scanned:
        out["updated"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(scanned)))
    return out


def overview(index: Path) -> dict:
    """What's indexed: folders, counts by kind, failures. For the CLI's stats command."""
    try:
        conn = db.connect_reader(index)
    except db.IndexMissing:
        raise SearchError(missing_index_message(index)) from None
    try:
        folders = []
        for r in conn.execute("SELECT * FROM roots ORDER BY path"):
            counts = dict(
                conn.execute(
                    "SELECT COALESCE(kind, 'failed'), COUNT(*) FROM samples WHERE root_id = ? GROUP BY 1", (r["id"],)
                ).fetchall()
            )
            failed = conn.execute(
                "SELECT COUNT(*) FROM samples WHERE root_id = ? AND error IS NOT NULL", (r["id"],)
            ).fetchone()[0]
            folders.append(
                {
                    "path": r["path"],
                    "browser": r["browser"],
                    "mounted": os.path.isdir(r["path"]),
                    "loops": counts.get(LOOP, 0),
                    "oneshots": counts.get(ONESHOT, 0),
                    "failed": failed,
                }
            )
        return {"index": str(index), **summary(conn), "folders": folders}
    finally:
        conn.close()


def missing_index_message(index: Path) -> str:
    repo = Path(__file__).resolve().parents[4]
    return (
        f"The sample index hasn't been built yet ({index} doesn't exist). Add your sample folders to "
        f'"sample_folders" in ~/.config/abletonconnect/config.json, then run: {SCAN_COMMAND.format(repo=repo)}'
    )
