"""The real analyzer on generated audio with a known tempo and pitch."""

import numpy as np
import pytest
import soundfile

from sampleindex import analyze

SR = 44100


def kick(seconds=0.5, freq=55.0, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return np.sin(2 * np.pi * freq * t) * np.exp(-t * 6)


def drum_loop(bpm=120.0, bars=2, sr=SR):
    """Kicks on beats 1 and 3, a snare-ish noise burst on 2 and 4, closed hats on eighths."""
    beat = 60.0 / bpm
    total = int(round(bars * 4 * beat * sr))
    y = np.zeros(total)
    rng = np.random.default_rng(1)
    for i in range(bars * 8):
        start = int(round(i * beat / 2 * sr))
        hat = rng.normal(0, 0.15, int(0.03 * sr)) * np.exp(-np.arange(int(0.03 * sr)) / (0.005 * sr))
        y[start : start + hat.size] += hat[: total - start]
        if i % 4 == 0:
            k = kick(0.3)
            y[start : start + k.size] += k[: total - start]
        if i % 4 == 2:
            n = rng.normal(0, 0.5, int(0.12 * sr)) * np.exp(-np.arange(int(0.12 * sr)) / (0.03 * sr))
            y[start : start + n.size] += n[: total - start]
    return y / np.max(np.abs(y)) * 0.9


def write(tmp_path, name, y, sr=SR):
    path = tmp_path / name
    soundfile.write(path, y, sr)
    return str(path)


def test_drum_loop_tempo_from_length(tmp_path):
    path = write(tmp_path, "Mystery 01.wav", drum_loop(120, bars=2))
    out = analyze.analyze_file(path, "Mystery 01.wav")
    assert out["kind"] == "loop"
    assert out["bpm"] == 120 and out["bpm_source"] == "audio"
    assert out["duration"] == pytest.approx(4.0) and out["sample_rate"] == SR and out["channels"] == 1


def test_fast_loop_is_reported_in_the_preferred_octave(tmp_path):
    path = write(tmp_path, "Thing.wav", drum_loop(170, bars=4))
    assert analyze.analyze_file(path, "Thing.wav")["bpm"] == 85  # 170 or 85; 70-140 is the convention here


def test_name_bpm_wins(tmp_path):
    path = write(tmp_path, "Loop 95BPM.wav", drum_loop(120, bars=2))
    out = analyze.analyze_file(path, "Loops/Loop 95BPM.wav")
    assert out["bpm"] == 95 and out["bpm_source"] == "name"


def test_bare_number_counts_when_the_length_agrees(tmp_path):
    path = write(tmp_path, "x.wav", drum_loop(81, bars=4))
    out = analyze.analyze_file(path, "Drum Loops/Dusty Drum Loop 81.wav")
    assert out["bpm"] == 81 and out["bpm_source"] == "name"
    out = analyze.analyze_file(path, "Drum Loops/Dusty Drum Loop 97.wav")
    assert out["bpm"] == 81 and out["bpm_source"] == "audio"
    hats = write(tmp_path, "h.wav", drum_loop(140, bars=2))
    assert analyze.analyze_file(hats, "Hihat Loops/DECAP 140 hihat loop.wav")["bpm"] == 140  # not folded to 70


def test_kick_is_a_pitched_one_shot(tmp_path):
    path = write(tmp_path, "k.wav", kick(0.6, freq=55.0))
    out = analyze.analyze_file(path, "Mystery.wav")
    assert out["kind"] == "oneshot" and "bpm" not in out
    assert out["note_midi"] == 33 and out["note_pc"] == 9  # 55 Hz = A0 in Live's naming


def test_name_note_wins_when_audio_disagrees(tmp_path):
    t = np.arange(SR) / SR
    path = write(tmp_path, "t.wav", 0.5 * np.sin(2 * np.pi * 220 * t) * np.exp(-t * 2))
    assert analyze.analyze_file(path, "One Shots/Tone.wav")["note_midi"] == 57  # A2
    out = analyze.analyze_file(path, "One Shots/Tone (C#).wav")
    assert out["note_pc"] == 1 and "note_midi" not in out


def test_noise_is_unpitched_and_brightness_orders_sounds(tmp_path):
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.3, SR // 2) * np.exp(-np.arange(SR // 2) / (0.1 * SR))
    bright = analyze.analyze_file(write(tmp_path, "n.wav", noise), "Hat.wav")
    dark = analyze.analyze_file(write(tmp_path, "k.wav", kick()), "Kick.wav")
    assert "note_midi" not in bright
    assert bright["brightness"] > 5 * dark["brightness"]


def test_key_comes_from_the_name(tmp_path):
    path = write(tmp_path, "x.wav", drum_loop(120, bars=2))
    out = analyze.analyze_file(path, "Melodic Loops/Keys Loop Am 120bpm.wav")
    assert (out["key_pc"], out["key_mode"]) == (9, "minor")
    assert "key_pc" not in analyze.analyze_file(path, "Melodic Loops/Keys Loop 120bpm.wav")


def test_silence_and_broken_files_never_raise(tmp_path):
    out = analyze.analyze_file(write(tmp_path, "s.wav", np.zeros(SR)), "Silence.wav")
    assert out["kind"] == "oneshot" and "error" not in out
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"RIFF....not audio")
    out = analyze.analyze_file(str(junk), "junk.wav")
    assert out["error"]
