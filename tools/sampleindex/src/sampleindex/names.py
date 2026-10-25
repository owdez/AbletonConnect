"""What a sample's file and folder names say: words to search, drum tags, BPM, key, loop or one-shot.

Names beat audio analysis when they say something explicit ("90BPM", "Am"), because estimating
tempo and key from a short sample is unreliable. Loop vs one-shot is a vote: name words and folder
names give points here, and the audio adds its own in analyze.classify().
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import music

ONESHOT, LOOP = "oneshot", "loop"

# Canonical tag -> words that imply it. A file gets the tag when any word appears in its name or
# folders, so "BD0010.wav" is found by searching "kick".
TAGS: dict[str, frozenset[str]] = {
    "kick": frozenset({"kick", "kik", "kck", "bd", "bassdrum", "kickdrum"}),
    "snare": frozenset({"snare", "snr", "sd", "snaredrum"}),
    "clap": frozenset({"clap", "clp", "handclap"}),
    "hat": frozenset({"hat", "hihat", "hh", "hhat", "chh", "ohh", "hho", "hhc", "closedhat", "openhat"}),
    "cymbal": frozenset({"cymbal", "cym", "crash", "ride", "china", "splash"}),
    "tom": frozenset({"tom", "tomtom"}),
    "rim": frozenset({"rim", "rimshot", "rimclick", "sidestick"}),
    "perc": frozenset(
        {
            "perc",
            "percussion",
            "prc",
            "conga",
            "bongo",
            "clave",
            "cowbell",
            "shaker",
            "tamb",
            "tambourine",
            "guiro",
            "woodblock",
            "timbale",
            "cabasa",
            "triangle",
            "agogo",
            "djembe",
            "tabla",
            "maraca",
        }
    ),
    "bass": frozenset({"bass", "sub", "subbass", "bassline", "reese", "808"}),
    "vocal": frozenset({"vocal", "vox", "voice", "adlib", "shout", "chant", "acapella", "acappella"}),
    "fx": frozenset(
        {"fx", "sfx", "riser", "uplifter", "downlifter", "sweep", "impact", "whoosh", "transition", "noise", "foley"}
    ),
    "drums": frozenset({"drum", "drumloop", "drumkit"}),
    "break": frozenset({"break", "breakbeat", "amen"}),
    "fill": frozenset({"fill"}),
}
# Words that mean the same as their tag, so a search for "bd" also finds "kick" and vice versa.
# The other words in TAGS are kinds of the tag: "conga" finds congas, but "perc" finds congas too.
SYNONYMS = frozenset(
    {
        "kik", "kck", "bd", "bassdrum", "kickdrum",
        "snr", "sd", "snaredrum",
        "clp", "handclap",
        "hihat", "hh", "hhat",
        "cym",
        "tomtom",
        "rimshot", "sidestick",
        "prc", "percussion",
        "vox",
        "sfx",
        "drumloop", "drumkit",
        "breakbeat",
    }
)  # fmt: skip
DRUM_TAGS = frozenset({"kick", "snare", "clap", "hat", "cymbal", "tom", "rim", "perc"})
NON_TONAL_TAGS = DRUM_TAGS | {"drums", "break", "fill"}
ALIASES: dict[str, str] = {alias: tag for tag, aliases in TAGS.items() for alias in aliases}

# Loop vs one-shot evidence, strongest first.
LOOP_WORDS = frozenset({"loop", "drumloop"})
ONESHOT_WORDS = frozenset({"oneshot", "shot", "hit", "single", "stab"})
LOOPISH_WORDS = frozenset({"break", "breakbeat", "amen", "groove", "fill"})
ONESHOTISH_WORDS = frozenset(
    {a for tag in DRUM_TAGS for a in TAGS[tag]}
    | {"808", "riser", "fall", "impact", "uplifter", "downlifter", "sweep", "whoosh", "boom"}
)

_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_TOKEN_SPLIT_RE = re.compile(r"[\s_\-.,()\[\]{}+&@=~']+")
_BPM_RES = (
    re.compile(r"(?<![\d.])(\d{2,3})(?:[.,-](\d{1,2}))?[\s_-]*bpm", re.I),
    re.compile(r"bpm[\s_:-]*(\d{2,3})(?:[.,](\d{1,2}))?(?!\d)", re.I),
)
_KEY_TOKEN_RE = re.compile(r"^([A-G])([#b♯♭]?)((?i:minor|min|major|maj)|m)?$")
_NOTE_PAREN_RE = re.compile(r"\(([A-G])([#b♯♭]?)\)")
_KEY_WORDS_RE = re.compile(
    r"(?<![A-Za-z])([A-Ga-g])\s?(#|♯|♭|b|sharp|flat)?[\s_-]*(minor|major|min|maj)(?![a-z])", re.I
)
MIN_BPM, MAX_BPM = 40.0, 300.0


@dataclass
class NameInfo:
    name_words: list[str]  # words from the file name, for full-text search
    folder_words: list[str]  # words from the folders between the sample root and the file
    tags: list[str]
    loop_votes: float = 0.0
    oneshot_votes: float = 0.0
    bpm: float | None = None  # explicit "120 BPM" in the file name or a folder name
    bpm_hints: tuple[float, ...] = ()  # bare numbers that might be a BPM ("Drum Loop 81"); the loop length decides
    key: tuple[int, str | None] | None = None  # (pitch class, mode or None)
    note_letter: int | None = None  # pitch class of a bare trailing letter ("808 F"); could be a variant name
    drum: bool = False  # a drum sound or drum loop, so a key estimated from audio would be noise


def words(text: str) -> list[str]:
    """'KB6 Kit17 HiHat_open' -> ['kb', '6', 'kit', '17', 'hi', 'hat', 'open']."""
    return [w.lower() for w in _WORD_RE.findall(text)]


def singular(word: str) -> str:
    if len(word) > 4 and word.endswith(("ches", "shes", "xes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _vocabulary(ws: list[str]) -> set[str]:
    """Singular words plus joined neighbours, so 'hi hat', 'bass drum' and 'one shot' are seen as one word."""
    out = {singular(w) for w in ws}
    out.update(singular(a + b) for a, b in zip(ws, ws[1:], strict=False))
    return out


def tags_for(vocab: set[str]) -> list[str]:
    found = {ALIASES[w] for w in vocab if w in ALIASES}
    if "bassdrum" in vocab or "kickdrum" in vocab:
        found.discard("bass")
    return sorted(found)


def _folder_kind(vocab: set[str]) -> str | None:
    """'Loops', 'Fills', 'Drum Breaks' -> LOOP; 'One Shots' -> ONESHOT."""
    loop, oneshot = bool(vocab & (LOOP_WORDS | LOOPISH_WORDS)), bool(vocab & ONESHOT_WORDS)
    if loop == oneshot:
        return None
    return LOOP if loop else ONESHOT


def parse_bpm(text: str) -> float | None:
    for regex in _BPM_RES:
        for m in regex.finditer(text):
            value = float(m.group(1) + ("." + m.group(2) if m.group(2) else ""))
            if MIN_BPM <= value <= MAX_BPM:
                return value
    return None


def parse_key(text: str) -> tuple[int, str | None] | None:
    """A key written in a name: 'Am', 'F#m', 'Eb', 'C#maj', 'A minor'. A bare letter ('Kick A') doesn't count."""
    m = _KEY_WORDS_RE.search(text)
    if m:
        mode = music.MINOR if m.group(3).lower().startswith("min") else music.MAJOR
        return music.pitch_class(m.group(1), (m.group(2) or "").lower()), mode
    m = _NOTE_PAREN_RE.search(text)
    if m:
        return music.pitch_class(m.group(1), m.group(2)), None
    for token in _TOKEN_SPLIT_RE.split(text):
        m = _KEY_TOKEN_RE.match(token)
        if m and (m.group(2) or m.group(3)):
            quality = (m.group(3) or "").lower()
            mode = music.MINOR if quality in ("m", "min", "minor") else music.MAJOR if quality else None
            return music.pitch_class(m.group(1), m.group(2)), mode
    return None


def _trailing_letter(stem: str) -> int | None:
    tokens = [t for t in _TOKEN_SPLIT_RE.split(stem) if t]
    if len(tokens) > 1 and len(tokens[-1]) == 1 and tokens[-1] in "ABCDEFG":
        return music.pitch_class(tokens[-1])
    return None


def _bare_numbers(stem: str) -> tuple[float, ...]:
    """Standalone numbers that could be a tempo, last first: packs write "Drum Loop 81" or "DECAP 109 drum loop"."""
    tokens = [t for t in _TOKEN_SPLIT_RE.split(stem) if t.isdigit() and not t.startswith("0")]
    return tuple(float(t) for t in reversed(tokens) if 60 <= int(t) <= 200)


def parse(rel: str) -> NameInfo:
    """Read everything the path relative to the sample root says. `rel` uses '/' separators."""
    parts = rel.split("/")
    filename, folders = parts[-1], parts[:-1]
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename

    name_ws = words(stem)
    name_vocab = _vocabulary(name_ws)
    folder_ws = [w for folder in folders for w in words(folder)]
    folder_vocabs = [_vocabulary(words(folder)) for folder in reversed(folders)]  # nearest folder first
    all_vocab = name_vocab.union(*folder_vocabs)
    name_tags = tags_for(name_vocab)
    info = NameInfo(name_words=name_ws, folder_words=folder_ws, tags=tags_for(all_vocab))
    info.drum = bool(NON_TONAL_TAGS.intersection(name_tags or info.tags))

    info.bpm = parse_bpm(stem)
    bpm_in_folder = False
    if info.bpm is None:
        for folder in reversed(folders):
            info.bpm = parse_bpm(folder)
            if info.bpm:
                bpm_in_folder = True
                break

    if name_vocab & LOOP_WORDS:
        info.loop_votes += 3
    if name_vocab & ONESHOT_WORDS:
        info.oneshot_votes += 3
    if name_vocab & LOOPISH_WORDS:
        info.loop_votes += 1.5
    if name_vocab & ONESHOTISH_WORDS:
        info.oneshot_votes += 1
    if info.bpm is not None:
        info.loop_votes += 1 if bpm_in_folder else 1.5
    for depth, vocab in enumerate(folder_vocabs):
        kind = _folder_kind(vocab)
        if kind == LOOP:
            info.loop_votes += 2.5 if depth == 0 else 1
        elif kind == ONESHOT:
            info.oneshot_votes += 2.5 if depth == 0 else 1
        if kind:
            break
    if folder_vocabs and folder_vocabs[0] & ONESHOTISH_WORDS:  # "Kicks", "808s + Bass", "Risers"
        info.oneshot_votes += 1.5

    if info.bpm is None:
        info.bpm_hints = _bare_numbers(stem)
    info.key = parse_key(stem)
    if info.key is None:
        info.note_letter = _trailing_letter(stem)
    return info
