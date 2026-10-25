"""arrange_scenes against the fake Live."""

import pytest
from helpers import failure, make_dispatcher, result


@pytest.fixture
def d(live):
    song, app = live
    return make_dispatcher(song, app)


def notes(*starts, pitch=36):
    return [{"pitch": pitch, "start": s, "duration": 0.25} for s in starts]


@pytest.fixture
def scenes(d, live):
    """Scene 0: a 1-bar kick loop. Scene 1: a 2-bar riff and an audio loop. Scene 2: a 1-shot."""
    song, _ = live
    result(d, "create_clip", track_index=0, clip_slot_index=0, length=4, name="Intro kick")
    result(d, "add_notes", track_index=0, clip_slot_index=0, notes=notes(0, 1, 2, 3))
    result(d, "create_clip", track_index=0, clip_slot_index=1, length=8, name="Riff")
    result(d, "add_notes", track_index=0, clip_slot_index=1, notes=notes(0, 6.5, pitch=60))
    result(d, "load_browser_item", path="user_folders/Samples/Drums/Kick 01.wav", track_index=1, clip_slot_index=1)
    result(d, "create_clip", track_index=0, clip_slot_index=2, length=4, name="Hit")
    result(d, "add_notes", track_index=0, clip_slot_index=2, notes=notes(0, 3.9))
    song.tracks[0].clip_slots[2].clip.looping = False
    return song


def timeline(song, track=0):
    return [(c.name, c.start_time, c.end_time) for c in song.tracks[track].arrangement_clips]


def test_sections_land_back_to_back_in_one_undo_step(d, scenes, live):
    song, app = live
    steps = song.undo_steps
    out = result(d, "arrange_scenes", sections=[{"scene": 0, "bars": 4}, {"scene": 1, "bars": 3}, {"scene": 2, "bars": 2}])
    assert song.undo_steps == steps + 1
    assert [(s["start_bar"], s["bars"], s["clips"]) for s in out["sections"]] == [(1, 4, 1), (5, 3, 4), (8, 2, 1)]
    assert out["end_bar"] == 10 and app.view.shown == ["Arranger"]
    # MIDI: one clip per section, as long as the section; a one-shot plays once.
    assert timeline(song) == [("Intro kick", 0, 16), ("Riff", 16, 28), ("Hit", 28, 32)]
    kick = song.tracks[0].arrangement_clips[0]
    assert sorted(n.start_time for n in kick._notes) == list(range(16))
    riff = song.tracks[0].arrangement_clips[1]
    assert sorted(n.start_time for n in riff._notes) == [0, 6.5, 8]  # 3 bars of a 2-bar loop: the cut-off repeat drops its 2nd note
    # Audio: copied end to end; the 4-beat loop fills 3 bars exactly.
    assert timeline(song, 1) == [("Kick 01", 16, 20), ("Kick 01", 20, 24), ("Kick 01", 24, 28)]
    info = result(d, "get_track_info", track_index=0)
    assert info["arrangement"][1] == {"index": 1, "name": "Riff", "start_bar": 5, "end_bar": 8}
    assert result(d, "get_session_info")["tracks"][0]["arrangement_clips"] == 3


def test_start_bar_and_signature(d, scenes, live):
    song, _ = live
    song.signature_numerator, song.signature_denominator = 6, 8  # 3 beats per bar
    result(d, "arrange_scenes", sections=[{"scene": 0, "bars": 2}], start_bar=3, show=False)
    assert timeline(song) == [("Intro kick", 6, 12)]


def test_existing_timeline_clips_need_replace(d, scenes, live):
    song, _ = live
    result(d, "arrange_scenes", sections=[{"scene": 0, "bars": 4}])
    err = failure(d, "arrange_scenes", sections=[{"scene": 1, "bars": 2}], start_bar=3)
    assert err["type"] == "invalid_state" and "replace=true" in err["message"]
    out = result(d, "arrange_scenes", sections=[{"scene": 1, "bars": 2}], start_bar=3, replace=True)
    assert out["removed"] == 1 and [c[0] for c in timeline(song)] == ["Riff"]


def test_audio_overhang_is_reported(d, scenes, live):
    out = result(d, "arrange_scenes", sections=[{"scene": 1, "bars": 1.5}])
    assert "runs 2 beats past" in out["warnings"][0]


def test_create_midi_clip_returning_nothing(d, scenes, live, monkeypatch):
    song, _ = live
    track = song.tracks[0]
    real = track.create_midi_clip
    monkeypatch.setattr(track, "create_midi_clip", lambda start, length: real(start, length) and None)
    result(d, "arrange_scenes", sections=[{"scene": 0, "bars": 1}])
    assert len(track.arrangement_clips[0]._notes) == 4


def test_bad_sections(d, scenes):
    assert "non-empty list" in failure(d, "arrange_scenes", sections=[])["message"]
    assert "must be" in failure(d, "arrange_scenes", sections=[{"scene": 0}])["message"]
    assert failure(d, "arrange_scenes", sections=[{"scene": 99, "bars": 4}])["type"] == "not_found"
    assert "bars must be > 0" in failure(d, "arrange_scenes", sections=[{"scene": 0, "bars": 0}])["message"]
    assert "limit" in failure(d, "arrange_scenes", sections=[{"scene": 1, "bars": 5000}])["message"]


def test_playhead_stays_put(d, scenes, live, monkeypatch):
    """Live moves the playhead to each clip create_midi_clip makes; arrange_scenes puts it back."""
    song, _ = live
    track = song.tracks[0]
    real = track.create_midi_clip

    def create_and_move(start, length):
        song.current_song_time = start
        return real(start, length)

    monkeypatch.setattr(track, "create_midi_clip", create_and_move)
    result(d, "arrange_scenes", sections=[{"scene": 0, "bars": 4}, {"scene": 1, "bars": 4}])
    assert song.current_song_time == 0.0


def test_clips_with_no_notes_in_their_section_are_skipped(d, scenes, live):
    """A riser at the end of a long clip doesn't reach a shorter section: place nothing, say so."""
    song, _ = live
    result(d, "create_clip", track_index=0, clip_slot_index=3, length=32, name="Riser")
    result(d, "add_notes", track_index=0, clip_slot_index=3, notes=notes(24, pitch=60))
    out = result(d, "arrange_scenes", sections=[{"scene": 3, "bars": 4}, {"scene": 3, "bars": 8}])
    assert [s["clips"] for s in out["sections"]] == [0, 1]
    assert out["skipped_empty"] == ["1-MIDI: Riser"]
    assert timeline(song) == [("Riser", 16, 48)]


def test_rename_and_delete_clips(d, scenes, live):
    song, _ = live
    result(d, "arrange_scenes", sections=[{"scene": 0, "bars": 4}, {"scene": 1, "bars": 4}])
    out = result(d, "set_clip", track_index=0, arrangement_index=1, name="Verse riff")
    assert out["clip"] == {"index": 1, "name": "Verse riff", "start_bar": 5, "end_bar": 9} and out["previous_name"] == "Riff"
    assert result(d, "set_clip", track_index=0, clip_slot_index=0, name="Kick")["clip"]["name"] == "Kick"
    out = result(d, "delete_clip", track_index=0, arrangement_index=0)
    assert out["deleted"]["name"] == "Intro kick" and out["arrangement_clips"] == 1
    assert result(d, "delete_clip", track_index=0, clip_slot_index=2)["deleted"]["name"] == "Hit"
    assert not song.tracks[0].clip_slots[2].has_clip
    assert "exactly one" in failure(d, "delete_clip", track_index=0)["message"]
    assert "exactly one" in failure(d, "set_clip", track_index=0, name="x", clip_slot_index=0, arrangement_index=0)["message"]
    assert failure(d, "delete_clip", track_index=0, arrangement_index=5)["type"] == "not_found"
    assert failure(d, "delete_clip", track_index=0, clip_slot_index=2)["type"] == "not_found"
