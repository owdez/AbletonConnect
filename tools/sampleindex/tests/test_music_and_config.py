import json

import pytest

from sampleindex import config, music


@pytest.mark.parametrize(
    "text, key",
    [
        ("A minor", (9, "minor")),
        ("Am", (9, "minor")),
        ("am", (9, "minor")),
        ("F#", (6, None)),
        ("Eb major", (3, "major")),
        ("ebm", (3, "minor")),
        ("F sharp minor", (6, "minor")),
        ("8A", (9, "minor")),
        ("8B", (0, "major")),
        ("1A", (8, "minor")),
        ("12B", (4, "major")),
    ],
)
def test_parse_key(text, key):
    assert music.parse_key(text) == key


def test_parse_key_rejects_nonsense():
    with pytest.raises(ValueError, match="A minor"):
        music.parse_key("H minor")


def test_compatible_keys_are_the_camelot_neighbours():
    assert music.compatible_keys(9, "minor") == {(9, "minor"), (0, "major"), (4, "minor"), (2, "minor")}
    assert music.compatible_keys(0, "major") == {(0, "major"), (9, "minor"), (7, "major"), (5, "major")}
    assert music.compatible_keys(9, None) == {(9, "major"), (9, "minor")}


def test_note_names_follow_live():
    assert music.note_name(60) == "C3"
    assert music.note_name(33) == "A0"
    assert music.key_name(10, "minor") == "Bb minor"


def test_sample_folders_default_to_live_user_folders(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "sample_folders": [
                    "/Volumes/Samples Drive/Samples/",
                    {"path": "~/Music/Breaks", "browser": "user_folders/Music/Breaks/"},
                    "/Volumes/Samples Drive/Samples",
                ]
            }
        )
    )
    folders = config.sample_folders(config.load(cfg_file), cfg_file)
    assert [f.browser for f in folders] == ["user_folders/Samples", "user_folders/Music/Breaks"]
    assert folders[0].path == "/Volumes/Samples Drive/Samples"
    assert not folders[1].path.startswith("~")


def test_nested_sample_folders_are_rejected():
    with pytest.raises(config.ConfigError, match="inside"):
        config.sample_folders({"sample_folders": ["/a/Samples", "/a/Samples/Kicks"]})


def test_bad_config_says_what_to_fix(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{nope")
    with pytest.raises(config.ConfigError, match="isn't valid JSON"):
        config.load(cfg_file)
    assert config.load(tmp_path / "missing.json") == {}
    with pytest.raises(config.ConfigError, match="list"):
        config.sample_folders({"sample_folders": "/a"})


def test_index_path_env_overrides_config(monkeypatch, tmp_path):
    monkeypatch.delenv(config.INDEX_ENV, raising=False)
    assert config.index_path({}) == config.DEFAULT_INDEX
    assert config.index_path({"sample_index": "~/x.db"}).name == "x.db"
    monkeypatch.setenv(config.INDEX_ENV, str(tmp_path / "env.db"))
    assert config.index_path({"sample_index": "~/x.db"}) == tmp_path / "env.db"


def test_browser_path_escapes_like_livebridge():
    assert config.browser_path("user_folders/Samples", "Kicks/a\\b.wav") == "user_folders/Samples/Kicks/a\\\\b.wav"
