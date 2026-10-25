import pytest

from sampleindex import config, db, names, search

LOOP, ONESHOT = "loop", "oneshot"

# rel, analysis result
SAMPLES = [
    ("Drums/Kicks/BD0010.wav", {"duration": 0.4, "kind": ONESHOT, "note_pc": 9, "note_midi": 33, "brightness": 300.0}),
    (
        "Drums/Kicks/Deep Kick 02.wav",
        {"duration": 0.6, "kind": ONESHOT, "note_pc": 5, "note_midi": 29, "brightness": 150.0},
    ),
    ("Drums/Kicks/Clicky Kick.wav", {"duration": 0.3, "kind": ONESHOT, "brightness": 2500.0}),
    ("Drums/Hats/Hat Closed.wav", {"duration": 0.2, "kind": ONESHOT, "brightness": 8000.0}),
    (
        "Loops/Break 90BPM.wav",
        {"duration": 2.667, "kind": LOOP, "bpm": 90.0, "bpm_source": "name", "brightness": 2000.0},
    ),
    ("Loops/Break 45.wav", {"duration": 5.333, "kind": LOOP, "bpm": 45.0, "bpm_source": "audio", "brightness": 1800.0}),
    (
        "Loops/Keys Loop Am 120bpm.wav",
        {"duration": 8.0, "kind": LOOP, "bpm": 120.0, "bpm_source": "name", "key_pc": 9, "key_mode": "minor"},
    ),
    (
        "Loops/Pad Loop C 120bpm.wav",
        {"duration": 8.0, "kind": LOOP, "bpm": 120.0, "bpm_source": "name", "key_pc": 0, "key_mode": "major"},
    ),
    ("Loops/Broken.wav", {"error": "LibsndfileError: bad file"}),
]


@pytest.fixture
def index(tmp_path):
    path = tmp_path / "samples.db"
    conn = db.connect_writer(path)
    root = db.upsert_root(conn, config.SampleFolder(str(tmp_path), "user_folders/Samples"), mounted=True)
    for rel, result in SAMPLES:
        db.write_sample(conn, root, rel, 1, 1, 1, result, names.parse(rel))
    db.set_meta(conn, "scanned_at", "1700000000")
    conn.commit()
    conn.close()
    return path


def names_of(result):
    return [r["name"] for r in result["results"]]


def test_text_matches_names_folders_and_tags(index):
    out = search.search(index, text="kick")
    assert set(names_of(out)) == {"BD0010.wav", "Deep Kick 02.wav", "Clicky Kick.wav"}  # BD -> kick
    assert names_of(search.search(index, text="kicks deep")) == ["Deep Kick 02.wav"]
    assert names_of(search.search(index, text="hats")) == ["Hat Closed.wav"]
    assert search.search(index, text="nothing like this")["total"] == 0


def test_failed_files_are_never_returned(index):
    assert "Broken.wav" not in names_of(search.search(index, limit=100))


def test_loop_and_one_shot_words_set_the_kind(index):
    out = search.search(index, text="break loop")
    assert out["kind_from_text"] == LOOP and set(names_of(out)) == {"Break 90BPM.wav", "Break 45.wav"}
    assert search.search(index, text="one-shot")["total"] == 4


def test_bpm_with_half_and_double_time(index):
    out = search.search(index, bpm=90)
    assert names_of(out) == ["Break 90BPM.wav", "Break 45.wav"]  # the straight match first
    assert names_of(search.search(index, bpm=91, bpm_tolerance=2)) == ["Break 90BPM.wav", "Break 45.wav"]
    assert out["results"][1]["bpm_estimated"] is True
    assert names_of(search.search(index, bpm=90, half_double=False)) == ["Break 90BPM.wav"]
    assert search.search(index, bpm=100)["total"] == 0


def test_key_matches_loops_and_root_notes(index):
    assert set(names_of(search.search(index, key="Am"))) == {"Keys Loop Am 120bpm.wav", "BD0010.wav"}
    assert names_of(search.search(index, key="A minor", kind=LOOP)) == ["Keys Loop Am 120bpm.wav"]
    compatible = names_of(search.search(index, key="Am", compatible_keys=True, kind=LOOP))
    assert set(compatible) == {"Keys Loop Am 120bpm.wav", "Pad Loop C 120bpm.wav"}  # relative major
    with pytest.raises(search.SearchError, match="Can't read"):
        search.search(index, key="purple")


def test_brightness_is_relative_to_the_other_matches(index):
    assert names_of(search.search(index, text="kick", brightness="dark")) == ["Deep Kick 02.wav"]
    assert names_of(search.search(index, text="kick", brightness="bright")) == ["Clicky Kick.wav"]


def test_result_shape(index):
    out = search.search(index, text="deep kick")
    assert out["index"] == {"files": 8, "updated": out["index"]["updated"]}
    assert out["results"] == [
        {
            "name": "Deep Kick 02.wav",
            "kind": ONESHOT,
            "length": 0.6,
            "note": "F0",
            "brightness_hz": 150,
            "browser_path": "user_folders/Samples/Drums/Kicks/Deep Kick 02.wav",
        }
    ]
    loop = search.search(index, text="keys")["results"][0]
    assert loop["key"] == "A minor" and loop["bpm"] == 120 and "bpm_estimated" not in loop


def test_length_limit_and_offset(index):
    assert search.search(index, max_length=0.3)["total"] == 2
    assert search.search(index, min_length=5)["total"] == 3
    page = search.search(index, kind=ONESHOT, limit=2, offset=2)
    assert page["total"] == 4 and len(page["results"]) == 2


def test_offline_folders_are_flagged(tmp_path):
    path = tmp_path / "samples.db"
    conn = db.connect_writer(path)
    root = db.upsert_root(
        conn, config.SampleFolder("/Volumes/Not Mounted/Samples", "user_folders/Samples"), mounted=False
    )
    db.write_sample(conn, root, "Kick.wav", 1, 1, 1, {"duration": 0.5, "kind": ONESHOT}, names.parse("Kick.wav"))
    conn.commit()
    out = search.search(path, text="kick")
    assert out["offline"] == ["/Volumes/Not Mounted/Samples"] and out["results"][0]["available"] is False


def test_missing_index_explains_how_to_build_it(tmp_path):
    with pytest.raises(search.SearchError, match="sampleindex scan"):
        search.search(tmp_path / "none.db", text="kick")


def test_fts_query_is_safe_with_punctuation(index):
    assert search.search(index, text='kick" OR "hat')["total"] == 0  # quotes are dropped, words must all match
    assert search.parse_text("808 (C)").fts == '"808"* AND "c"'


def test_only_synonyms_widen_a_search():
    assert search.parse_text("bd").fts == '("bd" OR tags:"kick")'
    assert search.parse_text("percs").fts == '("perc"* OR tags:"perc")'  # the tag itself: congas, shakers...
    assert search.parse_text("conga").fts == '"conga"*'  # a kind of perc, not every perc
    assert search.parse_text("amen").fts == '"amen"*'


def test_dark_and_bright_in_the_text_set_brightness(index):
    out = search.search(index, text="dark kick")
    assert out["brightness_from_text"] == "dark" and names_of(out) == ["Deep Kick 02.wav"]
    assert search.parse_text("dark bright").brightness == "dark"  # contradictory: the second is searched as a word
