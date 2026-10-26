"""The user's taste profile: ~/.config/abletonconnect/taste.md, written by the user (docs/taste.example.md
is the template). Claude reads it by section and appends dated notes; it never rewrites the user's text."""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

from .config import config_file

DEFAULT_SECTION = "Feedback log"
_HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*#*\s*$")


class TasteError(Exception):
    """Something the user should fix; the message says what."""


def path() -> Path:
    return config_file("taste.md", "ABLETONCONNECT_TASTE")


@dataclass
class Section:
    title: str
    level: int  # 2 for "##", 3 for "###"
    start: int  # index of the heading line
    end: int  # index of the first line after the section (its subsections included)


def read(file: Path | None = None) -> str:
    file = file or path()
    try:
        return file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TasteError(
            f"There's no taste profile at {file}. Copy docs/taste.example.md from the AbletonConnect repo there and fill it in."
        ) from None


def sections(lines: list[str]) -> list[Section]:
    found: list[Section] = []
    in_code = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else _HEADING.match(line.rstrip("\n"))
        if m:
            found.append(Section(m.group(2), len(m.group(1)), i, len(lines)))
    for i, section in enumerate(found):
        later = [s for s in found[i + 1 :] if s.level <= section.level]
        if later:
            section.end = later[0].start
    return found


def find(lines: list[str], name: str) -> Section:
    """A section by title, case-insensitive: exact, else the only one starting with or containing it."""
    all_sections = sections(lines)
    key = " ".join(name.casefold().split())

    def title(s: Section) -> str:
        return " ".join(s.title.casefold().split())

    for test in (lambda s: title(s) == key, lambda s: title(s).startswith(key), lambda s: key in title(s)):
        hits = [s for s in all_sections if test(s)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise TasteError(f"'{name}' matches {', '.join(s.title for s in hits)}. Use the full title.")
    listed = ", ".join(s.title for s in all_sections) or "none"
    raise TasteError(f"No section '{name}' in the taste profile. Sections: {listed}.")


def get(name: str | None = None, file: Path | None = None) -> dict:
    text = read(file)
    lines = text.splitlines(keepends=True)
    if name is None:
        return {"path": str(file or path()), "sections": [s.title for s in sections(lines)], "text": text}
    section = find(lines, name)
    return {"section": section.title, "text": "".join(lines[section.start : section.end]).rstrip() + "\n"}


def add_note(
    note: str, section: str = DEFAULT_SECTION, file: Path | None = None, today: datetime.date | None = None
) -> dict:
    """Append "- YYYY-MM-DD: note" as the last line of a section. Nothing else in the file changes."""
    note = note.strip()
    if not note:
        raise TasteError("The note is empty.")
    file = file or path()
    text = read(file)
    lines = text.splitlines(keepends=True)
    target = find(lines, section)
    date = (today or datetime.date.today()).isoformat()
    body = note.splitlines()
    entry = [f"- {date}: {body[0]}\n"] + [f"  {line}\n" if line.strip() else "\n" for line in body[1:]]
    at = target.end
    while at > target.start + 1 and not lines[at - 1].strip():
        at -= 1  # after the section's last text, before the blank lines that end it
    if at > 0 and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    lines[at:at] = entry
    file.write_text("".join(lines), encoding="utf-8")
    return {"section": target.title, "added": "".join(entry).rstrip("\n")}
