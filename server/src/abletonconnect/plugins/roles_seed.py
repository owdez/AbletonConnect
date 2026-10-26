"""A first plugin_roles.yaml: draft roles for the plugins on this machine, for the user to confirm.
Written once, when the file doesn't exist; the scanner never changes it after that."""

from __future__ import annotations

from pathlib import Path

from . import index

HEADER = """\
# Your notes on your plugins, merged into list_plugins and get_plugin_info.
# abletonconnect-scan-plugins never changes this file once it exists.
#
# Keys are plugin names as list_plugins shows them. Fields:
#   role   what you reach for it for: drums, bass, synth, pad, eq, compressor, delay, reverb, ...
#   tags   words to find it by
#   notes  anything else: what it's good at, settings you like, when not to use it
#   draft  true means Claude guessed. Confirm or fix the entry, then delete the draft line.
"""

# (match on the plugin name, role, tags, notes). The first match wins.
GUESSES = [
    ("RC909 Pro", "drums", ["909", "drum machine"], "TR-909 style drum machine."),
    ("Pultec EQP-1A", "eq", ["pultec", "low end", "air"], "Broad tone shaping: low-end weight and top-end air."),
    ("Pultec HLF-3C", "eq", ["pultec", "filter"], "High- and low-pass filtering with color."),
    ("Pultec MEQ-5", "eq", ["pultec", "mids"], "Midrange boosts and cuts."),
    ("Ozone 12 Equalizer", "eq", ["mastering", "linear phase"], "Clean and mastering EQ."),
    ("RX 10", "repair", ["cleanup", "restoration"], "Audio repair; not a creative effect."),
    ("SLVC Dub Echo", "delay", ["dub", "throws", "echo"], "Delay throws on vocals and percussion."),
    ("SLVC Delay", "delay", ["delay"], None),
    ("SLVC Reverb", "reverb", ["reverb"], None),
    ("SLVC Distortion", "saturation", ["distortion", "drive"], None),
    ("SLVC EQ", "eq", ["eq"], None),
    ("SLVC Max", "limiter", ["loudness", "master"], "Guess from the name: a maximizer for the master bus."),
    ("SLVC Control", "utility", [], "Guess: unclear from the name. Say what it does."),
    ("PolyMAX", "synth", ["poly", "analog"], "Polyphonic analog-style synth."),
    ("Massive X", "synth", ["wavetable", "bass", "lead"], None),
    ("SpectraForm", "synth", ["spectral"], "Your own synth."),
    ("Komplete Kontrol", "instrument host", ["nks", "kontakt"], "Hosts Kontakt and NKS instruments."),
    ("Guitar Rig", "amp", ["guitar", "multi-fx"], None),
    ("610-A", "preamp", ["tube", "color"], None),
    ("610-B", "preamp", ["tube", "color"], None),
    ("1176", "compressor", ["fet", "fast", "vocals", "drums"], "Fast FET compression: vocals, drums, bass."),
    ("LA-2A", "compressor", ["opto", "smooth", "vocals"], "Smooth optical leveling: vocals, bass."),
    ("Century Tube Channel Strip", "channel strip", ["tube", "vocals"], None),
    ("Galaxy Tape Echo", "delay", ["tape echo", "space echo"], None),
    ("Pure Plate", "reverb", ["plate", "vocals"], None),
    ("Showtime", "amp", ["guitar", "tube amp"], None),
    ("Hemisphere", "mic modeling", ["microphone"], None),
    ("Vibe Analog Machines", "saturation", ["tape", "analog"], None),
]


def _guess(name: str):
    for needle, role, tags, notes in GUESSES:
        if needle.lower() in name.lower():
            return role, tags, notes
    return None


def render(entries: list[dict]) -> str:
    lines = [HEADER]
    for entry in sorted(entries, key=lambda e: (e.get("vendor") or "", e["name"])):
        guess = _guess(entry["name"])
        if not guess:
            continue
        role, tags, notes = guess
        lines.append("{}:".format(_key(entry["name"])))
        lines.append(f"  role: {role}")
        if tags:
            lines.append("  tags: [{}]".format(", ".join(tags)))
        if notes:
            lines.append(f"  notes: {_quote(notes)}")
        lines.append("  draft: true")
        lines.append("")
    return "\n".join(lines)


def _key(name: str) -> str:
    return _quote(name) if any(c in name for c in ":#[]{},&*!|>'\"%@`") or name != name.strip() else name


def _quote(text: str) -> str:
    return '"{}"'.format(text.replace("\\", "\\\\").replace('"', '\\"'))


def seed_roles(entries: list[dict], path: Path | None = None) -> Path | None:
    path = path or index.roles_path()
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(entries), encoding="utf-8")
    return path
