"""Devices inside racks, Simpler's playback mode, and output meters against the fake Live."""

import time

import fake_live
import pytest
from helpers import failure, make_dispatcher, result


@pytest.fixture
def d(live):
    song, app = live
    return make_dispatcher(song, app)


@pytest.fixture
def racks(d, live):
    """Track 0: Arp rack (chain 'Lead': Arpeggiator, Operator, an effect rack) and a Drum Rack."""
    song, _ = live
    song.tracks[0].devices = [fake_live.make_instrument_rack(), fake_live.make_drum_rack()]
    return song


# ---- racks --------------------------------------------------------------------------------------


def test_track_info_lists_chains_with_device_paths(d, racks):
    arp, kit = result(d, "get_track_info", track_index=0)["devices"]
    lead, sub = arp["chains"]
    assert lead["name"] == "Lead" and sub == {"index": 1, "name": "Sub", "devices": []}
    assert [(x["path"], x["name"]) for x in lead["devices"]] == [([0, 0, 0], "Arpeggiator"), ([0, 0, 1], "Operator"), ([0, 0, 2], "Space")]
    assert lead["devices"][2]["chains"][0]["devices"][0] == {"path": [0, 0, 2, 0, 0], "name": "Reverb", "class": "Reverb"}
    assert [(c["name"], c["note"]) for c in kit["chains"]] == [("Kick", "C1"), ("Tom", "A1")]
    assert kit["chains"][1]["devices"][0]["playback_mode"] == "Classic"


def test_chain_listing_is_capped(d, racks, monkeypatch):
    monkeypatch.setattr("LiveBridge.lom.CHAIN_LIMIT", 1)
    monkeypatch.setattr("LiveBridge.lom.RACK_DEPTH", 1)
    arp = result(d, "get_track_info", track_index=0)["devices"][0]
    assert len(arp["chains"]) == 1 and arp["chain_count"] == 2
    assert arp["chains"][0]["devices"][2]["chain_count"] == 1  # the nested rack is past RACK_DEPTH


def test_parameters_of_a_nested_device(d, racks):
    out = result(d, "get_device_parameters", track_index=0, device_path=[0, 0, 1], name_filter="filter type")
    assert out["device"]["path"] == [0, 0, 1] and out["device"]["name"] == "Operator"
    assert out["parameters"][0]["items"] == ["Lowpass", "Highpass", "Bandpass"]
    out = result(d, "set_device_parameter", track_index=0, device_path=[0, 0, 1], parameter="Filter Freq", value="1.2 kHz")
    assert out["path"] == [0, 0, 1] and out["parameter"]["display"] == "1.20 kHz"
    reverb = racks.tracks[0].devices[0].chains[0].devices[2].chains[0].devices[0]
    result(d, "set_device_parameter", track_index=0, device_path=[0, 0, 2, 0, 0], parameter="Device On", value="Off")
    assert reverb.parameters[0].value == 0.0
    # A top-level path is the same as device_index; the rack itself shows its macros.
    assert result(d, "get_device_parameters", track_index=0, device_path=[0])["parameters"][1]["name"] == "Macro 1"


def test_device_path_errors(d, racks):
    assert "not both" in failure(d, "get_device_parameters", track_index=0, device_index=0, device_path=[0])["message"]
    assert "odd length" in failure(d, "get_device_parameters", track_index=0, device_path=[0, 0])["message"]
    assert "odd length" in failure(d, "get_device_parameters", track_index=0, device_path=[])["message"]
    assert "integer" in failure(d, "get_device_parameters", track_index=0, device_path=[0, "a", 1])["message"]
    err = failure(d, "get_device_parameters", track_index=0, device_path=[0, 5, 0])
    assert err["type"] == "not_found" and "Chains: 0 Lead, 1 Sub" in err["message"]
    assert "0 Arpeggiator, 1 Operator, 2 Space" in failure(d, "get_device_parameters", track_index=0, device_path=[0, 0, 9])["message"]
    assert "isn't a rack" in failure(d, "get_device_parameters", track_index=0, device_path=[0, 0, 1, 0, 0])["message"]
    assert "has no device 0" in failure(d, "set_device_parameter", track_index=0, device_path=[0, 1, 0], parameter=0, value=0)["message"]


# ---- Simpler playback mode ----------------------------------------------------------------------


def test_simpler_playback_mode_in_a_drum_pad(d, racks):
    song = racks
    tom = song.tracks[0].devices[1].chains[1].devices[0]
    out = result(d, "get_device_parameters", track_index=0, device_path=[1, 1, 0])
    assert out["device"]["playback_mode"] == "Classic"
    assert out["properties"][0] == {"name": "playback_mode", "value": "Classic", "options": ["Classic", "One-Shot", "Slicing"]}

    steps = song.undo_steps
    out = result(d, "set_device_property", track_index=0, device_path=[1, 1, 0], property="playback_mode", value="one shot")
    assert out["property"]["value"] == "One-Shot" and out["previous"] == "Classic" and out["path"] == [1, 1, 0]
    assert tom.playback_mode == 1 and song.undo_steps == steps + 1 and song.undo_depth == 0
    assert result(d, "set_device_property", track_index=0, device_path=[1, 1, 0], property="playback_mode", value=2)["property"]["value"] == "Slicing"
    assert result(d, "set_device_property", track_index=0, device_path=[1, 1, 0], property="slicing_playback_mode", value="Poly")["property"]["value"] == "Poly"


def test_top_level_simpler_and_property_errors(d, live):
    song, _ = live
    song.tracks[0].devices = [fake_live.FakeSimpler(), fake_live.make_operator()]
    assert result(d, "get_track_info", track_index=0)["devices"][0]["playback_mode"] == "Classic"
    assert result(d, "set_device_property", track_index=0, device_index=0, property="playback_mode", value="One-Shot")["property"]["value"] == "One-Shot"
    assert "Options: Classic, One-Shot, Slicing" in failure(d, "set_device_property", track_index=0, property="playback_mode", value="Loop")["message"]
    assert "0-2" in failure(d, "set_device_property", track_index=0, property="playback_mode", value=3)["message"]
    assert "has: playback_mode" in failure(d, "set_device_property", track_index=0, property="voices", value=1)["message"]
    err = failure(d, "set_device_property", track_index=0, device_index=1, property="playback_mode", value=0)
    assert "no settable properties" in err["message"] and "OriginalSimpler" in err["message"]
    assert "properties" not in result(d, "get_device_parameters", track_index=0, device_index=1)


# ---- meters -------------------------------------------------------------------------------------


def reading(db):
    """What Live's meters report for a level in dBFS: linear in dB, -70 dB -> 0, +6 dB -> 1."""
    return (db + 70.0) / 76.0


@pytest.fixture
def quick(monkeypatch):
    """Shrink the measurement windows so a test takes ~0.3 s instead of 3 s."""
    monkeypatch.setattr("LiveBridge.handlers.meters.SETTLE_S", 0.05)
    monkeypatch.setattr("LiveBridge.handlers.meters.HOLD_FROM_S", 0.1)
    monkeypatch.setattr("LiveBridge.handlers.meters.MIN_SECONDS", 0.2)


def run_meters(d, feed, duration=0.3):
    """Run get_meters tick by tick, calling feed(seconds since start) before each tick."""
    replies = []
    d.handle({"id": 1, "cmd": "get_meters", "args": {"duration": duration}}, replies.append)
    start = time.monotonic()
    while not replies and time.monotonic() - start < 5:
        feed(time.monotonic() - start)
        d.run_jobs(time.monotonic() + 0.04)
        time.sleep(0.01)
    assert replies and replies[0]["ok"], replies
    return replies[0]["result"]


def set_meter(track, db=None, held=None):
    track.output_meter_left = track.output_meter_right = 0.0 if db is None else reading(db)
    track.output_meter_level = 0.0 if held is None else reading(held)


def test_meters_report_peak_and_average_in_dbfs(d, live, quick):
    song, _ = live
    song.is_playing = True
    song.tracks[0].devices = [fake_live.make_operator()]
    track, audio = song.tracks
    audio.mute = True
    set_meter(song.return_tracks[0], -12.0)
    set_meter(song.master_track, 3.0)

    def feed(t):
        # Stale 0 dB from before the call, then -20 dB with one -6 dB hit.
        set_meter(track, 0.0 if t < 0.04 else -6.0 if 0.15 < t < 0.17 else -20.0)

    out = run_meters(d, feed)
    assert 0.3 <= out["seconds"] < 1.0
    assert out["tracks"][0]["peak_db"] == -6.0 and -20.0 < out["tracks"][0]["avg_db"] < -12.0
    assert out["tracks"][1] == {"index": 1, "name": "2-Audio", "silent": True, "mute": True}
    assert out["returns"][0] == {"index": 0, "name": "A-Reverb", "peak_db": -12.0, "avg_db": -12.0}
    assert out["returns"][1]["silent"]
    assert out["master"] == {"name": "Master", "peak_db": 3.0, "avg_db": 3.0, "over": True}


def test_held_peaks_count_only_once_they_are_from_the_window(d, live, quick):
    song, _ = live
    song.is_playing = True
    a, b = song.tracks
    a.devices = [fake_live.make_operator()]

    def feed(t):
        # a: a -3 dB hit the momentary readings missed, held flat by Live.
        set_meter(a, -10.0, held=-3.0)
        # b: a 0 dB peak from before the call, fading out of the held meter.
        set_meter(b, -20.0, held=max(-20.0, -10.0 - 100 * t))

    out = run_meters(d, feed)
    assert out["tracks"][0]["peak_db"] == -3.0 and out["tracks"][0]["avg_db"] == -10.0
    assert out["tracks"][1]["peak_db"] == -20.0


def test_meters_need_the_transport_running(d, live, quick):
    song, _ = live
    err = failure(d, "get_meters")
    assert err["type"] == "invalid_state" and "start_playback" in err["message"]
    assert "0.2-10.0" in failure(d, "get_meters", duration=11)["message"]
    song.is_playing = True
    out = run_meters(d, lambda t: setattr(song, "is_playing", t < 0.1), duration=2.0)
    assert "stopped after" in out["note"] and out["seconds"] < 1.0
    assert out["tracks"][0] == {"index": 0, "name": "1-MIDI", "audio": False}  # no instrument yet
