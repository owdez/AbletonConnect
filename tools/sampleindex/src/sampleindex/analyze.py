"""Audio analysis with librosa: length, loop or one-shot, BPM, root note and brightness.

Runs in worker processes during a scan. analyze_file() never raises: a file that can't be read
comes back with an "error" so the scan can record it and move on. What the file name says
(names.py) wins over estimates from the audio.

Keys come only from file names. Krumhansl-style key estimation (four profiles, three chroma
variants) matched the key in the name on 35-45% of a test library's melodic loops, which is worse
than no answer.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass

import librosa
import numpy as np
import soundfile

from . import names
from .names import LOOP, ONESHOT

ANALYZER_VERSION = 1  # bump to re-analyze every file on the next scan
SR = 22050
HOP = 512
MAX_SECONDS = 30.0  # enough for tempo, notes and brightness; long files aren't decoded whole
NOTE_SECONDS = 1.5
MAX_FLATNESS = 0.01  # above this a one-shot is too noisy to have a root note

LENGTH_FIT_TOLERANCE = 0.015  # a bare number in the name counts as the BPM if the loop length agrees this closely
PREFERRED_BPM = (70.0, 140.0)  # the octave to report; 94% right on loops with a BPM in their name
LONGEST_CUT_LOOP = 32.0  # seconds; longer files are recordings or stems, not loops cut to whole bars


@dataclass
class Features:
    duration: float  # whole file, seconds
    strong_onsets: int
    onset_spread: float  # first to last strong onset, as a fraction of the analyzed audio
    tail_energy: float  # energy in the second half relative to the first half


def analyze_file(path: str, rel: str) -> dict:
    """Analyze one file. `rel` is its path under the sample root, for name hints."""
    out: dict = {}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out.update(read_info(path))
            y, sr = librosa.load(path, sr=SR, mono=True, duration=MAX_SECONDS)
            out.update(analyze_signal(y, sr, out["duration"], names.parse(rel)))
    except Exception as e:  # a broken file must not stop the scan
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    return out


def read_info(path: str) -> dict:
    try:
        info = soundfile.info(path)
        return {
            "duration": info.frames / info.samplerate,
            "sample_rate": int(info.samplerate),
            "channels": int(info.channels),
        }
    except Exception:
        return {"duration": float(librosa.get_duration(path=path))}


def analyze_signal(y: np.ndarray, sr: int, duration: float, hints: names.NameInfo) -> dict:
    out: dict = {}
    if y.size < sr // 50 or float(np.max(np.abs(y))) < 1e-4:  # empty or silent
        out["kind"] = LOOP if hints.loop_votes > hints.oneshot_votes else ONESHOT
        return out

    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=HOP)[0]
    oenv = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    out["brightness"] = brightness(y, sr, rms)

    kind = out["kind"] = classify(features(oenv, rms, sr, duration), hints)

    if kind == LOOP:
        bpm, source = loop_bpm(oenv, sr, duration, hints)
        if bpm is not None:
            out["bpm"], out["bpm_source"] = bpm, source

    if hints.key is not None and (kind == LOOP or hints.key[1] is not None):
        out["key_pc"], out["key_mode"] = hints.key

    if kind == ONESHOT:
        named = hints.key[0] if hints.key is not None and hints.key[1] is None else None
        midi = root_note(y, sr)
        if midi is not None and (named is None or midi % 12 == named):
            out["note_pc"], out["note_midi"] = midi % 12, midi
        elif named is not None:
            out["note_pc"] = named
        elif hints.note_letter is not None and not hints.drum:  # "Raw 808 43 G", but not "Snare B"
            out["note_pc"] = hints.note_letter
    return out


# ---- features ---------------------------------------------------------------------------------


def brightness(y: np.ndarray, sr: int, rms: np.ndarray) -> float | None:
    """Spectral centroid in Hz, weighted by loudness and ignoring near-silent frames."""
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, n_fft=2048, hop_length=HOP)[0]
    n = min(len(centroid), len(rms))
    centroid, weight = centroid[:n], rms[:n].copy()
    weight[weight < 0.05 * weight.max()] = 0.0
    total = float(weight.sum())
    if total <= 0:
        return None
    return round(float((centroid * weight).sum() / total), 1)


def features(oenv: np.ndarray, rms: np.ndarray, sr: int, duration: float) -> Features:
    frames = librosa.onset.onset_detect(onset_envelope=oenv, sr=sr, hop_length=HOP, units="frames")
    peak = float(oenv.max()) if oenv.size else 0.0
    strong = [f for f in frames if peak > 0 and oenv[f] >= 0.3 * peak]
    analyzed = max(len(oenv) - 1, 1)
    spread = (strong[-1] - strong[0]) / analyzed if len(strong) > 1 else 0.0
    energy = rms.astype(np.float64) ** 2
    half = len(energy) // 2
    head = float(energy[:half].sum())
    tail = float(energy[half:].sum()) / head if head > 0 else 0.0
    return Features(duration=duration, strong_onsets=len(strong), onset_spread=spread, tail_energy=tail)


def classify(f: Features, hints: names.NameInfo) -> str:
    """Loop or one-shot: the name's votes plus the audio's. A tie goes to what the audio suggests."""
    loop, oneshot = hints.loop_votes, hints.oneshot_votes
    rhythmic = f.strong_onsets >= 3 and f.onset_spread >= 0.5 and 0.25 <= f.tail_energy <= 3.0
    if f.duration < 1.0:
        oneshot += 3
    elif f.duration < 1.5:
        oneshot += 2
    elif rhythmic:
        loop += 2
    elif f.tail_energy < 0.05 or f.strong_onsets <= 1:  # one hit dying away
        oneshot += 3
    else:
        oneshot += 1
    if loop == oneshot:
        return LOOP if rhythmic else ONESHOT
    return LOOP if loop > oneshot else ONESHOT


def _octave_distance(a: float, b: float) -> float:
    """How far a is from b or any octave of b, as a ratio (0.04 = 4%)."""
    x = math.log2(a / b)
    return abs(x - round(x)) * math.log(2)  # ~ relative difference for small values


def _to_octave(bpm: float, lo: float, hi: float) -> float:
    while bpm < lo:
        bpm *= 2
    while bpm >= hi:
        bpm /= 2
    return bpm


def _tidy(bpm: float) -> float:
    nearest = round(bpm)
    return float(nearest) if abs(bpm - nearest) < 0.2 else round(bpm, 1)


def loop_bpm(oenv: np.ndarray, sr: int, duration: float, hints: names.NameInfo) -> tuple[float | None, str]:
    """A loop's tempo and where it came from ("name" or "audio").

    Loops are cut to whole bars, so the length pins the tempo down to an octave: a 2.667 s loop is
    45, 90 or 180 BPM. On a 4,000-file test library the length agreed with the BPM in the file name
    93% of the time and librosa's beat-tracking estimate 18% of the time, so the length wins and the
    octave is the one inside PREFERRED_BPM, unless a bare number in the name ("Drum Loop 81") matches
    the length and so settles it. Very long files aren't cut loops, so they get the estimate.
    """
    if hints.bpm is not None:
        return hints.bpm, "name"
    if duration <= 0 or oenv.size < 8 or float(oenv.max()) <= 0:
        return None, "audio"
    one_bar = 240.0 / duration
    for hint in hints.bpm_hints:
        if _octave_distance(hint, one_bar) <= LENGTH_FIT_TOLERANCE:
            return hint, "name"
    lo, hi = PREFERRED_BPM
    if duration <= LONGEST_CUT_LOOP:
        return _tidy(_to_octave(one_bar, lo, hi)), "audio"
    estimate = float(librosa.feature.tempo(onset_envelope=oenv, sr=sr, hop_length=HOP)[0])
    return _tidy(_to_octave(estimate, lo, hi)), "audio"


def root_note(y: np.ndarray, sr: int) -> int | None:
    """MIDI note of a pitched one-shot (a tuned 808, a tom, a vocal note), or None if unpitched.

    pyin finds a "pitch" in noise too, so noisy sounds (hats, cymbals, claps) are ruled out first by
    spectral flatness: in testing, pitched kicks, toms and 808s sat near 0, hats and claps above 0.02.
    """
    segment = y[int(0.02 * sr) : int(NOTE_SECONDS * sr)]
    if segment.size < 2048:
        return None
    rms = librosa.feature.rms(y=segment, frame_length=2048, hop_length=512)[0]
    audible = rms > 0.1 * rms.max() if rms.size and rms.max() > 0 else np.zeros(rms.size, dtype=bool)
    if not audible.any():
        return None
    flatness = librosa.feature.spectral_flatness(y=segment, n_fft=2048, hop_length=512)[0][: len(audible)]
    if float(np.median(flatness[audible[: len(flatness)]])) > MAX_FLATNESS:
        return None
    f0, voiced, _ = librosa.pyin(segment, fmin=30.0, fmax=2000.0, sr=sr, frame_length=2048)
    n = min(len(audible), len(voiced))
    audible, voiced, f0 = audible[:n], voiced[:n], f0[:n]
    pitched = voiced & audible & ~np.isnan(f0)
    if pitched.sum() < max(3, 0.5 * audible.sum()):
        return None
    midi = librosa.hz_to_midi(f0[pitched])
    if float(np.std(midi)) > 1.0:  # sliding or unstable pitch: no single root note
        return None
    return int(round(float(np.median(midi))))
