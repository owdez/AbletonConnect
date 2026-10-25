import pytest

from sampleindex import music, names
from sampleindex.names import LOOP, ONESHOT


@pytest.mark.parametrize(
    "text, bpm",
    [
        ("Pitched Clave Percussion Loop (112BPM)", 112),
        ("KB6 Kit17 Break08 90BPM 01", 90),
        ("Groove 5 - 108 BPM - Fill 1", 108),
        ("21chh129_CT_jack hat_2-mod@130bpm", 130),
        ("116043-DrumPlayPc-4-79-5bpm-Kalantzis", 79.5),
        ("loop_bpm120_dry", 120),
        ("Kit 17 (90 BPM)", 90),
        ("Snare E008", None),
        ("Wavy Drum Loop 81", None),  # a bare number isn't a BPM; it's only a hint
        ("Riser 900bpm", None),
    ],
)
def test_parse_bpm(text, bpm):
    assert names.parse_bpm(text) == bpm


@pytest.mark.parametrize(
    "text, key",
    [
        ("Birds and Bees Strings Rhodes Loop CMIN (150BPM)", (0, "minor")),
        ("Bass F#m 120bpm", (6, "minor")),
        ("pad_a_minor", (9, "minor")),
        ("Lead Ebmaj", (3, "major")),
        ("Vocal Shout Adlib One Shot Bb", (10, None)),
        ("Tunisian Flute Held (C#)", (1, None)),
        ("Kick A", None),  # a variant letter, not a key
        ("Kick A002", None),
        ("SD BD CH OH", None),
        ("FM Bass", None),
        ("Eminem style", None),
    ],
)
def test_parse_key(text, key):
    assert names.parse_key(text) == key


def test_words_split_camel_case_and_numbers():
    assert names.words("KB6 Kit17 HiHat_open") == ["kb", "6", "kit", "17", "hi", "hat", "open"]


def test_tags_from_abbreviations_folders_and_word_pairs():
    assert names.parse("Analogue/BD/BD0010.wav").tags == ["kick"]
    assert "hat" in names.parse("Kit/Hi Hat Closed 3.wav").tags
    assert "bass" not in names.parse("Kit/Bass Drum 01.wav").tags and "kick" in names.parse("Kit/Bass Drum 01.wav").tags
    assert names.parse("DECAP/808s + Bass/Wide (C) 808.wav").tags == ["bass"]


def _kind(rel):
    info = names.parse(rel)
    return LOOP if info.loop_votes > info.oneshot_votes else ONESHOT if info.oneshot_votes > info.loop_votes else None


def test_name_votes_for_loop_or_one_shot():
    assert _kind("DECAP/Drum Loops/Wavy LoFi Dirty Tape Noise Drum Loop 81.wav") == LOOP
    assert _kind("DECAP/Kicks/Superpower Kick.wav") == ONESHOT
    assert _kind("Drum Breaks/Drums from Another Planet/One_Shot/Kick A002.wav") == ONESHOT
    assert _kind("Drum Breaks/Drums from Another Planet/Loop/Thing.wav") == LOOP
    assert _kind("Soundsnap/266772-rain_car_interior.wav") is None  # the audio decides
    # A numbered "Break14" with "Hit" in a breaks pack: close, so the audio settles it.
    info = names.parse("Drum Breaks/Monster Breaks Vol.2/Kit 10 (100 BPM)/MB2 Kit10 Break14 Hit 100BPM 01.wav")
    assert info.oneshot_votes >= 3 and info.loop_votes >= 3


def test_bare_numbers_are_bpm_hints():
    assert names.parse("DECAP/Drum Loops/Wavy Drum Loop 81.wav").bpm_hints == (81,)
    assert names.parse("DECAP/Percussion Loops/Conga Loop 1 116.wav").bpm_hints == (116,)
    assert names.parse("DECAP/X/Hihat Loops/DECAP 140 hihat loop simple bright.wav").bpm_hints == (140,)
    assert names.parse("Soundsnap/170055-NatureCalls-95-Drums01.wav").bpm_hints == (95,)
    assert names.parse("KB6 Kit17 Break08 01.wav").bpm_hints == ()
    assert names.parse("Drum Breaks/Kit 06 (100 BPM)/Break05 01.wav").bpm == 100  # from the folder


def test_trailing_note_letter_is_kept_separately():
    info = names.parse("Matt Lange Drop Sample /One Shots/Raw 808 43 G.wav")
    assert info.key is None and info.note_letter == music.pitch_class("G")
    assert names.parse("X/Snares/Snare B.wav").drum
