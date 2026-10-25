"""Command handlers against the fake Live Object Model."""

import pytest
from helpers import call, failure, make_dispatcher, result

from LiveBridge.handlers.browser import join_path, split_path
from LiveBridge.lom import parse_db


@pytest.fixture
def d(live):
    song, app = live
    return make_dispatcher(song, app)


# ---- dispatcher ---------------------------------------------------------------------------------


def test_unknown_command_and_bad_args(d):
    assert failure(d, "nope")["type"] == "unknown_command"
    err = failure(d, "set_tempo", tempo=120)
    assert err["type"] == "bad_args" and "bpm" in err["message"]
    assert failure(d, "create_clip", track_index=0)["type"] == "bad_args"


def test_mutating_commands_are_one_undo_step(d, live):
    song, _ = live
    result(d, "set_tempo", bpm=100)
    assert song.undo_steps == 1 and song.undo_depth == 0
    failure(d, "set_tempo", bpm=5)
    assert song.undo_depth == 0  # step closed even on failure


def test_live_runtime_error_is_reported_as_live_error(d, live, monkeypatch):
    song, _ = live

    def explode(index):
        raise RuntimeError("Live said no")

    monkeypatch.setattr(song, "create_midi_track", explode)
    err = failure(d, "create_midi_track")
    assert err["type"] == "live_error" and "Live said no" in err["message"]


# ---- session ------------------------------------------------------------------------------------


def test_session_info(d):
    info = result(d, "get_session_info")
    assert info["tempo"] == 120.0 and info["signature"] == "4/4"
    assert info["scale"] == "C Major"
    assert [t["name"] for t in info["tracks"]] == ["1-MIDI", "2-Audio"]
    assert info["tracks"][0]["kind"] == "midi" and info["tracks"][1]["kind"] == "audio"
    assert info["returns"][0]["kind"] == "return"
    assert "mute" not in info["master"]
    assert info["selected_track"] == {"type": "track", "index": 0}


def test_track_info_and_index_errors(d):
    info = result(d, "get_track_info", track_index=1)
    assert info["name"] == "2-Audio" and info["mixer"]["volume"]["display"] == "0.0 dB"
    assert [s["return"] for s in info["mixer"]["sends"]] == ["A-Reverb", "B-Delay"]
    err = failure(d, "get_track_info", track_index=9)
    assert err["type"] == "not_found" and "2 tracks (0-1)" in err["message"]
    assert result(d, "get_track_info", track_type="master")["kind"] == "master"
    assert failure(d, "get_track_info", track_index=0, track_type="bus")["type"] == "bad_args"


def test_tempo_and_transport(d, live):
    song, _ = live
    assert result(d, "set_tempo", bpm=128.5) == {"tempo": 128.5, "previous": 120.0}
    assert failure(d, "set_tempo", bpm="fast")["type"] == "bad_args"
    assert result(d, "start_playback")["playing"] is True
    assert result(d, "stop_playback")["playing"] is False


def test_undo(d, live):
    song, _ = live
    assert failure(d, "undo")["type"] == "invalid_state"
    result(d, "set_tempo", bpm=90)
    assert result(d, "undo")["undone"] is True


# ---- tracks and mixer ---------------------------------------------------------------------------


def test_create_tracks(d, live):
    song, _ = live
    out = result(d, "create_midi_track", name="Bass")
    assert out["created"]["index"] == 2 and out["created"]["name"] == "Bass" and out["track_count"] == 3
    out = result(d, "create_audio_track", index=0, name="Vox")
    assert out["created"]["index"] == 0 and song.tracks[0].name == "Vox"
    assert failure(d, "create_midi_track", index=10)["type"] == "bad_args"


def test_set_mixer_db_and_raw(d, live):
    song, _ = live
    out = result(d, "set_mixer", track_index=0, volume_db=-6.0, pan=-0.5, sends={"1": 0.25}, mute=True)
    assert out["volume"]["display"] == "-6.0 dB"
    assert out["pan"]["display"] == "25L"
    assert out["sends"][1]["value"] == 0.25
    assert out["mute"] is True and song.tracks[0].mute is True
    out = result(d, "set_mixer", track_index=0, sends_db={"0": -12})
    assert out["sends"][0]["display"] == "-12.0 dB"


def test_set_mixer_validation(d):
    assert failure(d, "set_mixer", track_index=0)["type"] == "bad_args"
    assert failure(d, "set_mixer", track_index=0, volume=0.5, volume_db=-3)["type"] == "bad_args"
    assert "above the maximum" in failure(d, "set_mixer", track_index=0, volume_db=12)["message"]
    assert "out of range" in failure(d, "set_mixer", track_index=0, pan=2)["message"]
    assert failure(d, "set_mixer", track_index=0, sends={"5": 0.1})["type"] == "not_found"
    assert failure(d, "set_mixer", track_type="master", mute=True)["type"] == "bad_args"
    assert result(d, "set_mixer", track_type="master", volume_db=-1)["volume"]["display"] == "-1.0 dB"


def test_volume_minus_inf(d):
    assert result(d, "set_mixer", track_index=0, volume_db=-200)["volume"]["display"] == "-inf dB"


def test_parse_db():
    assert parse_db("-6.0 dB") == -6.0
    assert parse_db("−6.0 dB") == -6.0
    assert parse_db("-inf dB") == float("-inf")
    assert parse_db("C") is None


# ---- clips and notes ----------------------------------------------------------------------------


def test_clip_and_notes_round_trip(d):
    out = result(d, "create_clip", track_index=0, clip_slot_index=0, length=8, name="Riff")
    assert out["clip"]["length"] == 8.0 and out["clip"]["name"] == "Riff" and out["clip"]["note_count"] == 0
    notes = [
        {"pitch": 60, "start": 0, "duration": 0.5, "velocity": 100},
        {"pitch": 64, "start": 1, "duration": 0.5, "probability": 0.5},
        {"pitch": 67, "start": 9, "duration": 1},
    ]
    out = result(d, "add_notes", track_index=0, clip_slot_index=0, notes=notes)
    assert out["added"] == 3 and out["clip"]["note_count"] == 3
    assert "1 note(s)" in out["warning"]
    got = result(d, "get_clip_notes", track_index=0, clip_slot_index=0)
    assert got["count"] == 3
    assert got["notes"][0] == {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 100}
    assert got["notes"][1]["probability"] == 0.5
    out = result(d, "add_notes", track_index=0, clip_slot_index=0, notes=[{"pitch": 48, "start": 0, "duration": 4}], replace=True)
    assert out["clip"]["note_count"] == 1


def test_clip_errors(d):
    assert failure(d, "create_clip", track_index=1, clip_slot_index=0)["type"] == "invalid_state"  # audio track
    result(d, "create_clip", track_index=0, clip_slot_index=0)
    assert "already has clip" in failure(d, "create_clip", track_index=0, clip_slot_index=0)["message"]
    assert "one per scene" in failure(d, "create_clip", track_index=0, clip_slot_index=99)["message"]
    assert failure(d, "add_notes", track_index=0, clip_slot_index=1, notes=[{"pitch": 1, "start": 0, "duration": 1}])["type"] == "not_found"
    bad = [{"pitch": 200, "start": 0, "duration": 1}]
    assert "pitch must be 0-127" in failure(d, "add_notes", track_index=0, clip_slot_index=0, notes=bad)["message"]
    assert "unknown field" in failure(d, "add_notes", track_index=0, clip_slot_index=0, notes=[{"pitch": 1, "start": 0, "duration": 1, "len": 2}])["message"]
    assert "missing duration" in failure(d, "add_notes", track_index=0, clip_slot_index=0, notes=[{"pitch": 1, "start": 0}])["message"]


def test_fire_and_stop(d, live):
    song, _ = live
    assert failure(d, "fire_clip", track_index=0, clip_slot_index=0)["type"] == "not_found"
    result(d, "create_clip", track_index=0, clip_slot_index=0)
    out = result(d, "fire_clip", track_index=0, clip_slot_index=0)
    assert out["clip"]["triggered"] is True
    assert result(d, "stop_clip", track_index=0)["stopped"] == "all"
    assert result(d, "stop_clip", track_index=0, clip_slot_index=0)["stopped"] == 0


# ---- devices ------------------------------------------------------------------------------------


@pytest.fixture
def operator(d):
    result(d, "load_browser_item", path="instruments/Operator", track_index=0)
    return d


def test_device_parameters(operator):
    d = operator
    out = result(d, "get_device_parameters", track_index=0, device_index=0)
    assert out["device"]["name"] == "Operator"
    by_name = {p["name"]: p for p in out["parameters"]}
    assert by_name["Filter Type"]["items"] == ["Lowpass", "Highpass", "Bandpass"]
    assert by_name["Locked"]["enabled"] is False
    filtered = result(d, "get_device_parameters", track_index=0, name_filter="filter")
    assert [p["name"] for p in filtered["parameters"]] == ["Filter Freq", "Filter Type"]


def test_set_device_parameter_variants(operator):
    d = operator
    out = result(d, "set_device_parameter", track_index=0, parameter="Filter Freq", value=0.25)
    assert out["parameter"]["value"] == 0.25 and out["previous"]["value"] == 0.5
    assert out["parameter"]["display"].endswith("Hz")
    assert result(d, "set_device_parameter", track_index=0, parameter="filter type", value="Bandpass")["parameter"]["value"] == 2.0
    assert result(d, "set_device_parameter", track_index=0, parameter=3, normalized=0.5)["parameter"]["value"] == 0.5
    assert result(d, "set_device_parameter", track_index=0, parameter="Filter Type", normalized=0.5)["parameter"]["value"] == 1.0


def test_set_device_parameter_errors(operator):
    d = operator
    assert "Close matches" in failure(d, "set_device_parameter", track_index=0, parameter="Filter Frq", value=0.1)["message"]
    assert "out of range" in failure(d, "set_device_parameter", track_index=0, parameter="Filter Freq", value=3)["message"]
    assert "Options" in failure(d, "set_device_parameter", track_index=0, parameter="Filter Type", value="Notch")["message"]
    assert "whole steps" in failure(d, "set_device_parameter", track_index=0, parameter="Filter Type", value=0.5)["message"]
    assert "disabled" in failure(d, "set_device_parameter", track_index=0, parameter="Locked", value=1)["message"]
    assert "exactly one" in failure(d, "set_device_parameter", track_index=0, parameter=1)["message"]
    assert "continuous" in failure(d, "set_device_parameter", track_index=0, parameter="Filter Freq", value="high")["message"]
    assert failure(d, "get_device_parameters", track_index=0, device_index=4)["type"] == "not_found"


# ---- browser ------------------------------------------------------------------------------------


def test_path_escaping_round_trips():
    parts = ["user_library", "Presets", "Lo/Fi Keys.adg", "back\\slash"]
    assert split_path(join_path(parts)) == parts


def test_browse_root_and_folders(d):
    root = result(d, "browse_browser")
    assert {"instruments", "user_folders", "plugins"} <= {i["path"] for i in root["items"]}
    inst = result(d, "browse_browser", path="instruments")
    assert inst["items"][0] == {"name": "Operator", "path": "instruments/Operator", "folder": False, "loadable": True, "device": True}
    lib = result(d, "browse_browser", path="user_library/Presets")
    assert lib["items"][0]["path"] == "user_library/Presets/Lo\\/Fi Keys.adg"
    page = result(d, "browse_browser", path="user_folders/Samples/Drums", offset=200, limit=20)
    assert page["total"] == 250 and len(page["items"]) == 20 and page["more"] is True
    assert page["items"][0]["name"] == "Kick 200.wav"
    assert "not found" in failure(d, "browse_browser", path="instruments/Nope")["message"]
    assert failure(d, "browse_browser", path="bogus")["type"] == "not_found"


def test_search(d):
    out = result(d, "search_browser", query="operator")
    assert out["results"][0]["path"] == "instruments/Operator"
    assert out.get("complete") is True
    kicks = result(d, "search_browser", query="kick 1", categories=["user_folders"], max_results=5)
    assert len(kicks["results"]) == 5 and kicks["truncated"] == "max_results"
    assert failure(d, "search_browser", query="  ")["type"] == "bad_args"
    assert failure(d, "search_browser", query="x", categories=["nope"])["type"] == "not_found"


def test_search_spans_ticks_on_big_trees(d):
    reply = call(d, "search_browser", query="zzz-no-match", categories=["user_folders"])
    assert reply["ok"] and reply["result"]["visited"] >= 250
    assert reply["_ticks"] >= 1


def test_load_items(d, live):
    song, app = live
    out = result(d, "load_browser_item", path="audio_effects/Reverb", track_index=1)
    assert out["changed"] is True and out["devices"][-1]["name"] == "Reverb"
    assert song.view.selected_track is song.tracks[1]
    out = result(d, "load_browser_item", path="user_folders/Samples/Drums/Kick 03.wav", track_index=1, clip_slot_index=2)
    assert out["clip"]["name"] == "Kick 03.wav" and out["clip"]["type"] == "audio"
    assert "clip_slot_index" in failure(d, "load_browser_item", path="user_folders/Samples/Drums/Kick 04.wav", track_index=1)["message"]
    assert "can't be loaded" in failure(d, "load_browser_item", path="instruments", track_index=0)["message"]
    assert "already has clip" in failure(d, "load_browser_item", path="user_folders/Samples/Drums/Kick 05.wav", track_index=1, clip_slot_index=2)["message"]


def test_load_reports_no_change(d, live, monkeypatch):
    song, app = live
    monkeypatch.setattr(app.browser, "load_item", lambda item: None)
    out = result(d, "load_browser_item", path="instruments/Drift", track_index=0)
    assert out["changed"] is False and "note" in out
