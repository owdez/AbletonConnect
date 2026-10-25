"""Clip envelopes against the fake Live."""

import fake_live
import pytest
from helpers import failure, make_dispatcher, result


@pytest.fixture
def d(live):
    song, app = live
    return make_dispatcher(song, app)


@pytest.fixture
def clip(d, live):
    """A 4-bar clip on track 0 (MIDI, two sends) with Operator loaded."""
    song, _ = live
    song.tracks[0].devices.append(fake_live.make_operator())
    result(d, "create_clip", track_index=0, clip_slot_index=0, length=16, name="Build")
    return song.tracks[0].clip_slots[0].clip


def env_of(clip, param):
    return clip.automation_envelope(param)


def at(env, *times):
    return [round(env.value_at_time(t), 4) for t in times]


def test_volume_ramp_in_db_is_one_undo_step(d, live, clip):
    song, _ = live
    steps = song.undo_steps
    out = result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume", ramp={"start": 0, "end": 16, "from": "-24 dB", "to": "0 dB"})
    assert song.undo_steps == steps + 1 and song.undo_depth == 0
    assert out["envelope"]["parameter"] == "Volume" and out["envelope"]["device"] == "Mixer"
    assert [(p["time"], p["display"]) for p in out["envelope"]["points"]] == [(0, "-24.0 dB"), (16, "-0.0 dB")]
    env = env_of(clip, song.tracks[0].mixer_device.volume)
    lo, mid, hi = at(env, 0, 8, 16)
    assert lo < mid < hi and abs(hi - 0.85) < 0.005 and abs(mid - (lo + hi) / 2) < 1e-3  # straight in raw fader values


def test_device_parameter_curve_with_display_text(d, live, clip):
    song, _ = live
    out = result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, device_index=0, parameter="Filter Freq", ramp={"start": 0, "end": 16, "from": "200 Hz", "to": "8 kHz", "curve": "ease_in"})
    points = out["envelope"]["points"]
    assert out["envelope"]["device"] == "Operator" and out["envelope"]["parameter_index"] == 1
    assert len(points) == 17 and points[0]["display"] == "200.0 Hz" and points[-1]["display"] == "8.00 kHz"
    values = [p["value"] for p in points]
    assert values == sorted(values)
    # Slow start: a quarter of the way in, well under a quarter of the rise.
    assert values[4] - values[0] < 0.1 * (values[-1] - values[0])


def test_hold_makes_a_step_and_pan_takes_display_text(d, live, clip):
    song, _ = live
    pts = [{"time": 0, "value": "C"}, {"time": 8, "value": "50L", "curve": "hold"}, {"time": 12, "value": "50R"}]
    result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="pan", points=pts)
    env = env_of(clip, song.tracks[0].mixer_device.panning)
    # At a jump Live reports the value reached at that time; the new value starts just after.
    assert at(env, 0, 4, 8, 11.9, 12, 12.01, 16) == [0.0, -0.5, -1.0, -1.0, -1.0, 1.0, 1.0]


def test_replace_false_only_changes_the_written_range(d, live, clip):
    song, _ = live
    vol = song.tracks[0].mixer_device.volume
    result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume", ramp={"start": 0, "end": 16, "from": 0.2, "to": 0.6})
    result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume", points=[{"time": 4, "value": 0.8}, {"time": 8, "value": 0.8}], replace=False)
    assert at(env_of(clip, vol), 2, 4.0001, 6, 8, 8.0001, 12) == [0.25, 0.8, 0.8, 0.8, 0.4, 0.5]  # jumps in at 4, back out at 8
    # replace=True (the default) starts over: the points are the whole envelope.
    result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume", points=[{"time": 4, "value": 0.1}, {"time": 8, "value": 0.3}])
    assert at(env_of(clip, vol), 0, 4, 6, 8, 16) == [0.1, 0.1, 0.2, 0.3, 0.3]


def test_sends_by_index_or_letter(d, live, clip):
    song, _ = live
    out = result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="send B", ramp={"start": 12, "end": 16, "from": "-inf dB", "to": "-6 dB"})
    assert out["envelope"]["parameter"] == "Send B" and out["envelope"]["send"] == 1
    env = env_of(clip, song.tracks[0].mixer_device.sends[1])
    assert at(env, 0, 12)[0] == 0.0 and song.tracks[0].mixer_device.sends[1].str_for_value(env.value_at_time(16)) == "-6.0 dB"
    same = result(d, "get_clip_envelope", track_index=0, clip_slot_index=0, parameter="send 1")
    assert same["envelope"]["points"][-1]["display"] == "-6.0 dB"


def test_read_list_and_clear(d, live, clip):
    song, _ = live
    result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume", ramp={"start": 0, "end": 16, "from": "-12 dB", "to": "0 dB"})
    result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, device_index=0, parameter=1, points=[{"time": 0, "value": 0.2}, {"time": 4, "value": 0.9}])
    listed = result(d, "get_clip_envelope", track_index=0, clip_slot_index=0)["envelopes"]
    assert listed == [{"device": "Mixer", "parameter": "Volume", "points": 2}, {"device": "Operator", "device_index": 0, "parameter": "Filter Freq", "parameter_index": 1, "points": 2}]
    missing = result(d, "get_clip_envelope", track_index=0, clip_slot_index=0, parameter="pan")["envelope"]
    assert missing["exists"] is False and missing["value"] == "C"
    sampled = result(d, "get_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume", resolution=8)["envelope"]
    assert [s["time"] for s in sampled["samples"]] == [0, 8, 16] and sampled["resolution"] == 8

    out = result(d, "clear_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume")
    assert out["cleared"] == [{"device": "Mixer", "parameter": "Volume"}] and len(out["envelopes_left"]) == 1
    assert failure(d, "clear_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume")["type"] == "not_found"
    out = result(d, "clear_clip_envelope", track_index=0, clip_slot_index=0)
    assert [c["parameter"] for c in out["cleared"]] == ["Filter Freq"] and out["envelopes_left"] == []
    assert not clip.has_envelopes


def test_points_past_the_clip_end_warn(d, clip):
    out = result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume", ramp={"start": 8, "end": 32, "from": 0.1, "to": 0.9})
    assert "clip ends at 16" in out["warning"]


def test_bad_input(d, live, clip):
    def err(**args):
        base = {"track_index": 0, "clip_slot_index": 0, "parameter": "volume"}
        base.update(args)
        return failure(d, "set_clip_envelope", **base)

    ramp = {"start": 0, "end": 4, "from": 0.1, "to": 0.5}
    assert "exactly one" in err()["message"]
    assert "exactly one" in err(ramp=ramp, points=[{"time": 0, "value": 0.1}])["message"]
    assert "must increase" in err(points=[{"time": 4, "value": 0.1}, {"time": 2, "value": 0.2}])["message"]
    assert "curve must be" in err(points=[{"time": 0, "value": 0.1, "curve": "wobble"}, {"time": 4, "value": 0.2}])["message"]
    assert "outside 'Track Volume'" in err(ramp=dict(ramp, to=3))["message"]
    assert "above the maximum" in err(ramp=dict(ramp, to="+12 dB"))["message"]
    assert "ramp must be" in err(ramp={"start": 0, "to": 1})["message"]
    assert "mixer control" in err(parameter="cutoff")["message"]
    assert err(parameter="send C")["type"] == "not_found"
    assert "Close matches: Filter Freq" in err(parameter="Fliter Freq", device_index=0, ramp=ramp)["message"]
    assert "point 1" in err(device_index=0, parameter="Filter Freq", points=[{"time": 0, "value": "200 Hz"}, {"time": 4, "value": "loud"}])["message"]
    assert failure(d, "set_clip_envelope", track_index=0, clip_slot_index=3, parameter="volume", ramp=ramp)["type"] == "not_found"
    assert live[0].tracks[0].clip_slots[0].clip.has_envelopes is False  # nothing half-written


def test_older_live_draws_steps(d, live, clip, monkeypatch):
    song, _ = live
    monkeypatch.setattr(fake_live.FakeClip, "envelope_class", fake_live.OldFakeEnvelope)
    out = result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume", ramp={"start": 4, "end": 12, "from": 0.2, "to": 0.6})
    assert "small steps" in out["note"] and "samples" in out["envelope"]
    env = env_of(clip, song.tracks[0].mixer_device.volume)
    assert at(env, 0.01, 15) == [0.2, 0.6] and abs(env.value_at_time(8) - 0.4) < 0.01


def test_quantized_parameters_step_and_read_back_both_sides(d, live, clip):
    pts = [{"time": 0, "value": "Lowpass"}, {"time": 8, "value": "Highpass"}]
    out = result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, device_index=0, parameter="Filter Type", points=pts)
    # Live holds a quantized value until the next event; at the event it still reports the old one.
    assert [(p["time"], p["display"]) for p in out["envelope"]["points"]] == [(0, "Lowpass"), (8, "Lowpass"), (8, "Highpass")]


def test_reading_uses_raw_values_not_event_values(d, live, clip):
    """Live's event values for faders are linear gain; points come from value_at_time instead."""
    song, _ = live
    result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume", ramp={"start": 0, "end": 8, "from": 0.5, "to": 0.7})
    env = env_of(clip, song.tracks[0].mixer_device.volume)
    assert [round(e.value, 3) for e in env.events_in_range(0, 8)] != [0.5, 0.7]
    got = result(d, "get_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume")["envelope"]["points"]
    assert [(p["time"], p["value"]) for p in got] == [(0, 0.5), (8, 0.7)]


def test_times_stay_inside_lives_range(d, clip):
    err = failure(d, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume", points=[{"time": 0, "value": 0.1}, {"time": 2e6, "value": 0.2}])
    assert "time must be 0-1576800" in err["message"]


def test_devices_inside_racks_by_device_path(d, live, clip):
    song, _ = live
    song.tracks[0].devices = [fake_live.make_instrument_rack()]  # Arp rack > chain 0 'Lead' > Operator at [0, 0, 1]
    out = result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, device_path=[0, 0, 1], parameter="Filter Freq", ramp={"start": 0, "end": 16, "from": "200 Hz", "to": "2 kHz"})
    assert out["envelope"]["device_path"] == [0, 0, 1] and "device_index" not in out["envelope"]
    result(d, "set_clip_envelope", track_index=0, clip_slot_index=0, device_index=0, parameter="Macro 1", ramp={"start": 0, "end": 16, "from": 0, "to": 127})
    listed = result(d, "get_clip_envelope", track_index=0, clip_slot_index=0)["envelopes"]
    assert [(e.get("device_index"), e.get("device_path"), e["parameter"]) for e in listed] == [(None, [0, 0, 1], "Filter Freq"), (0, None, "Macro 1")]
    assert "not both" in failure(d, "get_clip_envelope", track_index=0, clip_slot_index=0, device_index=0, device_path=[0], parameter=0)["message"]
    out = result(d, "clear_clip_envelope", track_index=0, clip_slot_index=0, device_path=[0, 0, 1], parameter="Filter Freq")
    assert [e["parameter"] for e in out["envelopes_left"]] == ["Macro 1"]
