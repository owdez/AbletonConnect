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
    assert "Can't read 'high'" in failure(d, "set_device_parameter", track_index=0, parameter="Filter Freq", value="high")["message"]
    assert failure(d, "get_device_parameters", track_index=0, device_index=4)["type"] == "not_found"


def test_set_parameter_by_display_value(operator, live):
    d = operator
    song, _ = live
    steps = song.undo_steps
    out = result(d, "set_device_parameter", track_index=0, parameter="Filter Freq", value="120 Hz")
    assert out["parameter"]["display"] == "120.0 Hz"
    assert result(d, "set_device_parameter", track_index=0, parameter="Filter Freq", value="1.2 kHz")["parameter"]["display"] == "1.20 kHz"
    assert result(d, "set_device_parameter", track_index=0, parameter="Filter Freq", value="2500")["parameter"]["display"] == "2.50 kHz"
    assert result(d, "set_device_parameter", track_index=0, parameter="Env Decay", value="350 ms")["parameter"]["display"] == "350.0 ms"
    assert result(d, "set_device_parameter", track_index=0, parameter="Env Decay", value="2.5 s")["parameter"]["display"] == "2.50 s"
    assert result(d, "set_device_parameter", track_index=0, parameter="F1 Freq", value="400")["parameter"]["display"] == "400"
    assert result(d, "set_device_parameter", track_index=0, parameter="F1 Freq", value="2.5k")["parameter"]["display"] == "2.5k"
    assert song.undo_steps == steps + 7  # one undo step per call, however many probes it took
    assert "outside" in failure(d, "set_device_parameter", track_index=0, parameter="Filter Freq", value="40 kHz")["message"]
    assert "different units" in failure(d, "set_device_parameter", track_index=0, parameter="Filter Freq", value="300 ms")["message"]


def test_stepped_parameters_list_and_take_their_options(operator):
    d = operator
    params = {p["name"]: p for p in result(d, "get_device_parameters", track_index=0)["parameters"]}
    assert params["LFO Rate"]["items"][12] == "1/4" and "quantized" not in params["LFO Rate"]
    assert "items" not in params["Filter Freq"]
    out = result(d, "set_device_parameter", track_index=0, parameter="LFO Rate", value="1/4")
    assert out["parameter"]["value"] == 12 and out["parameter"]["display"] == "1/4"
    assert "Options: 8, 6, 4" in failure(d, "set_device_parameter", track_index=0, parameter="LFO Rate", value="1/5")["message"]
    assert result(d, "set_device_parameter", track_index=0, parameter="Transpose", value="+7 st")["parameter"]["value"] == 7
    assert result(d, "set_device_parameter", track_index=0, parameter="Transpose", value="-12")["parameter"]["value"] == -12


def test_parse_display():
    from LiveBridge.lom import parse_display

    assert parse_display("1.20 kHz") == (1200.0, "hz")
    assert parse_display("350 ms") == (350.0, "ms") and parse_display("2.5 s") == (2500.0, "ms")
    assert parse_display("-inf dB") == (float("-inf"), "db")
    assert parse_display("25L") == (-25.0, "pan") and parse_display("C") == (0.0, "pan")
    assert parse_display("4.00 : 1") == (4.0, "")
    assert parse_display("22.0k") == (22000.0, "") and parse_display("400") == (400.0, "")
    assert parse_display("Lowpass") is None


# ---- tracks and scenes --------------------------------------------------------------------------


def test_rename_arm_and_delete_tracks(d, live):
    song, _ = live
    out = result(d, "set_track", track_index=0, name="Kick", arm=True)
    assert out["name"] == "Kick" and out["arm"] is True
    assert "can't be armed" in failure(d, "set_track", track_index=0, track_type="return", arm=True)["message"]
    assert "Nothing to change" in failure(d, "set_track", track_index=0)["message"]
    out = result(d, "delete_track", track_index=0)
    assert out["deleted"]["name"] == "Kick" and out["remaining"] == [{"index": 0, "name": "2-Audio"}]
    assert "at least one" in failure(d, "delete_track", track_index=0)["message"]
    assert "master" in failure(d, "delete_track", track_index=0, track_type="master")["message"]
    out = result(d, "delete_track", track_index=1, track_type="return")
    assert out["remaining"] == [{"index": 0, "name": "A-Reverb"}]
    assert len(song.tracks[0].mixer_device.sends) == 1


def test_returns_never_list_a_send_to_themselves(d):
    info = result(d, "get_track_info", track_index=0, track_type="return")
    assert [s["return"] for s in info["mixer"]["sends"]] == ["B-Delay"]
    assert "itself" in failure(d, "set_mixer", track_index=0, track_type="return", sends={0: 0.5})["message"]
    out = result(d, "set_mixer", track_index=0, track_type="return", sends={1: 0.5})
    assert out["sends"] == [{"index": 1, "return": "B-Delay", "value": 0.5, "display": out["sends"][0]["display"]}]


def test_scenes(d, live):
    song, _ = live
    result(d, "create_clip", track_index=0, clip_slot_index=1)
    out = result(d, "create_scene", index=1, name="Build")
    assert out["created"] == {"index": 1, "name": "Build", "clips": 0} and out["scene_count"] == 9
    assert result(d, "set_scene", scene_index=2, name="Drop") == {"index": 2, "name": "Drop", "clips": 1, "previous_name": ""}
    info = result(d, "get_session_info")
    assert info["scenes"] == 9 and info["scene_names"][:3] == ["", "Build", "Drop"]
    fired = result(d, "fire_scene", scene_index=2)
    assert fired["launching"] == 1 and fired["transport_playing"] is True
    assert result(d, "delete_scene", scene_index=1)["scene_count"] == 8
    assert failure(d, "set_scene", scene_index=40, name="x")["type"] == "not_found"


def test_set_scale(d, live):
    song, _ = live
    assert result(d, "set_scale", root="E", scale="phrygian") == {"scale": "E Phrygian", "previous": "C Major"}
    assert result(d, "set_scale", root="Bb")["scale"] == "A# Phrygian"
    assert "no scale called" in failure(d, "set_scale", scale="Lydian Augmented")["message"]
    assert "note name" in failure(d, "set_scale", root="H")["message"]


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
    assert out["clip"]["name"] == "Kick 03" and out["clip"]["type"] == "audio"
    assert "clip_slot_index" in failure(d, "load_browser_item", path="user_folders/Samples/Drums/Kick 04.wav", track_index=1)["message"]
    assert "can't be loaded" in failure(d, "load_browser_item", path="instruments", track_index=0)["message"]
    assert "already has clip" in failure(d, "load_browser_item", path="user_folders/Samples/Drums/Kick 05.wav", track_index=1, clip_slot_index=2)["message"]


def test_load_reports_no_change(d, live, monkeypatch):
    song, app = live
    monkeypatch.setattr(app.browser, "load_item", lambda item: None)
    out = result(d, "load_browser_item", path="instruments/Drift", track_index=0)
    assert out["changed"] is False and "note" in out


LOOP = "user_folders/Samples/Loops/Cymbal Loop.wav"


def test_user_folder_sample_into_clip_slot_is_one_undo_step(d, live, tmp_path):
    # Live's browser load would set the tempo of an empty set from this loop as a second undo step.
    song, app = live
    app.browser.mount_user_folder(str(tmp_path))
    out = result(d, "load_browser_item", path=LOOP, track_index=1, clip_slot_index=0)
    assert out["clip"]["name"] == "Cymbal Loop" and out["clip"]["file"] == str(tmp_path / "Samples" / "Loops" / "Cymbal Loop.wav")
    assert "tempo" not in out and song.tempo == 120.0
    assert song.undo_steps == 1 and song.undo_depth == 0 and app.browser.loaded == []
    assert song.view.selected_track is song.tracks[1]
    # A sample onto a MIDI track still goes through the browser (into Simpler).
    out = result(d, "load_browser_item", path="user_folders/Samples/Drums/Kick 01.wav", track_index=0)
    assert out["devices"][0]["name"] == "Simpler" and app.browser.loaded == ["Kick 01.wav"]
    assert song.undo_steps == 2


def test_loop_without_a_file_reports_lives_tempo_change(d, live):
    # Live's library samples have no file path, so they go through the browser. So does a user
    # folder on a drive that isn't mounted (the fake's default).
    song, app = live
    out = result(d, "load_browser_item", path="samples/80s Beat 90 bpm.wav", track_index=1, clip_slot_index=0)
    assert out["clip"]["name"] == "80s Beat 90 bpm" and app.browser.loaded == ["80s Beat 90 bpm.wav"]
    assert out["tempo"] == {"from": 120.0, "to": 90.0} and "undo twice" in out["note"]
    assert song.undo_steps == 2
    out = result(d, "load_browser_item", path=LOOP, track_index=1, clip_slot_index=1)
    assert "tempo" not in out and app.browser.loaded[-1] == "Cymbal Loop.wav"
