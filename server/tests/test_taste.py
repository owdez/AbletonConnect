"""The taste profile: reading sections and appending dated notes without touching the user's text."""

import datetime
from pathlib import Path

import pytest

from abletonconnect import taste

TEMPLATE = Path(__file__).resolve().parents[2] / "docs" / "taste.example.md"
REQUIRED = [
    "Identity & references",
    "Defaults",
    "Sound palette",
    "Harmony & melody",
    "Rhythm & groove",
    "Arrangement",
    "Mix",
    "Hard rules",
    "Feedback log",
]

PROFILE = """\
# Taste profile

## Defaults

- Tempo: 124 BPM.
- Key: F# minor.

## Hard rules

### Always

- Ask for a seed.

### Never

- Stock presets.

```
## Not a heading inside code
```

## Feedback log

- 2026-10-01: Liked the darker pads.

"""
DAY = datetime.date(2026, 10, 26)


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "taste.md"
    path.write_text(PROFILE)
    return path


def test_the_template_has_every_section():
    titles = taste.sections(TEMPLATE.read_text().splitlines(keepends=True))
    assert [s.title for s in titles if s.level == 2] == REQUIRED


def test_sections_and_lookups(profile):
    lines = PROFILE.splitlines(keepends=True)
    assert [(s.title, s.level) for s in taste.sections(lines)] == [
        ("Defaults", 2),
        ("Hard rules", 2),
        ("Always", 3),
        ("Never", 3),
        ("Feedback log", 2),
    ]
    hard = taste.get("hard rules", profile)
    assert hard["section"] == "Hard rules" and "### Never" in hard["text"] and "Feedback" not in hard["text"]
    assert taste.get("feedback", profile)["text"] == "## Feedback log\n\n- 2026-10-01: Liked the darker pads.\n"
    whole = taste.get(None, profile)
    assert whole["text"] == PROFILE and whole["sections"][0] == "Defaults"
    with pytest.raises(taste.TasteError, match="No section 'Bass'.*Sections: Defaults, Hard rules"):
        taste.get("Bass", profile)


def test_notes_are_appended_and_nothing_else_changes(profile):
    out = taste.add_note("Rejected the vocal bed:\npads too bright.", file=profile, today=DAY)
    assert out == {"section": "Feedback log", "added": "- 2026-10-26: Rejected the vocal bed:\n  pads too bright."}
    after = profile.read_text()
    assert after == PROFILE.replace(
        "darker pads.\n", "darker pads.\n- 2026-10-26: Rejected the vocal bed:\n  pads too bright.\n"
    )
    taste.add_note("Never use risers.", section="never", file=profile, today=DAY)  # its last line is the fence
    assert (
        "## Not a heading inside code\n```\n- 2026-10-26: Never use risers.\n\n## Feedback log" in profile.read_text()
    )
    assert profile.read_text().replace("- 2026-10-26: Never use risers.\n", "") == after


def test_errors_say_what_to_do(tmp_path, profile):
    with pytest.raises(taste.TasteError, match="Copy docs/taste.example.md"):
        taste.read(tmp_path / "missing.md")
    with pytest.raises(taste.TasteError, match="empty"):
        taste.add_note("  ", file=profile)
    with pytest.raises(taste.TasteError, match="matches"):
        taste.add_note("x", section="e", file=profile)  # Defaults, Hard rules, Never, Feedback log...
    assert profile.read_text() == PROFILE


def test_a_file_without_a_final_newline(tmp_path):
    path = tmp_path / "taste.md"
    path.write_text("## Feedback log\n\n- 2026-01-01: First.")
    taste.add_note("Second.", file=path, today=DAY)
    assert path.read_text() == "## Feedback log\n\n- 2026-01-01: First.\n- 2026-10-26: Second.\n"
