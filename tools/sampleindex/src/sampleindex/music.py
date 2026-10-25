"""Pitch classes, keys and notes. Note names follow Live, where MIDI 60 is C3."""

from __future__ import annotations

import re

NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
MAJOR, MINOR = "major", "minor"

_LETTERS = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
_ACCIDENTALS = {"": 0, "#": 1, "♯": 1, "sharp": 1, "b": -1, "♭": -1, "flat": -1}
_QUALITIES = {"": None, "m": MINOR, "min": MINOR, "minor": MINOR, "maj": MAJOR, "major": MAJOR}
_KEY_RE = re.compile(r"^\s*([a-g])\s*(#|♯|♭|sharp|flat|b)?\s*(minor|min|major|maj|m)?\s*$", re.I)
_CAMELOT_RE = re.compile(r"^\s*(1[0-2]|[1-9])\s*([ab])\s*$", re.I)


def pitch_class(letter: str, accidental: str = "") -> int:
    return (_LETTERS[letter.lower()] + _ACCIDENTALS[accidental.lower()]) % 12


def pc_name(pc: int) -> str:
    return NAMES[pc % 12]


def key_name(pc: int, mode: str | None) -> str:
    return f"{pc_name(pc)} {mode}" if mode else pc_name(pc)


def note_name(midi: int) -> str:
    return f"{pc_name(midi)}{midi // 12 - 2}"


def parse_key(text: str) -> tuple[int, str | None]:
    """'A minor', 'Am', 'F#', 'Eb major', 'ebm' or Camelot '8A' -> (pitch class, mode or None)."""
    m = _CAMELOT_RE.match(text)
    if m:
        minor_pc = (9 + 7 * (int(m.group(1)) - 8)) % 12  # 8A is A minor; each step is a fifth
        return (minor_pc, MINOR) if m.group(2).lower() == "a" else ((minor_pc + 3) % 12, MAJOR)
    m = _KEY_RE.match(text)
    if not m:
        raise ValueError(f"Can't read {text!r} as a key. Use something like 'A minor', 'Am', 'F#' or 'Eb major'.")
    letter, accidental, quality = m.group(1), m.group(2) or "", m.group(3) or ""
    # "M" alone means major in chord symbols, "m" minor.
    mode = MAJOR if quality == "M" else _QUALITIES[quality.lower()]
    return pitch_class(letter, accidental), mode


def compatible_keys(pc: int, mode: str | None) -> set[tuple[int, str | None]]:
    """Keys that mix well: the key itself, its relative, and its neighbours a fifth up and down
    (adjacent on the Camelot wheel). A key with no mode matches either mode on the same tonic."""
    if mode is None:
        return {(pc, MAJOR), (pc, MINOR)}
    relative = ((pc + 3) % 12, MAJOR) if mode == MINOR else ((pc - 3) % 12, MINOR)
    return {(pc, mode), relative, ((pc + 7) % 12, mode), ((pc + 5) % 12, mode)}
