"""Output meters: Live's own track meters sampled over a few seconds while the set plays.

These are the meters Live draws next to each fader: peak meters, after the fader but before
the mute, so a muted track still shows its level. They are an interim mixing aid until the
Phase 3 analyzer plugin measures RMS and LUFS.

Measured in Live 12.4.7 (see CLAUDE.md): output_meter_left/right fall about 75 dB/s after
a peak, so a reading every tick (~100 ms) can miss up to ~7 dB of a drum hit.
output_meter_level holds the highest peak for 2 s and then falls just as fast, so it
catches every hit but also carries peaks from before the window. Hence:
- momentary readings count only after SETTLE_S, once a change made just before the call
  (a fader move, a mute) has fallen out of the meters;
- held readings count only after HOLD_FROM_S, when anything they hold happened inside
  the window, and only while not falling (a falling value is an older peak fading out).
"""

import math
import time

from ..dispatch import WAIT, command
from ..lom import meter_db, require_number, rnd, safe
from ..protocol import BridgeError

MIN_SECONDS, MAX_SECONDS = 2.5, 10.0
SETTLE_S = 1.0  # a meter falls from +6 dB to silence in about 1 s
HOLD_FROM_S = 2.3  # Live holds output_meter_level for 2.0 s


class _Meter:
    def __init__(self, group, index, track):
        self.group = group
        self.index = index
        self.track = track
        self.peak = 0.0
        self.power = 0.0  # sum of squared amplitudes of the momentary readings
        self.count = 0
        self.held = None

    def sample(self, elapsed):
        track = self.track
        left = safe(lambda: track.output_meter_left, 0.0) or 0.0
        right = safe(lambda: track.output_meter_right, 0.0) or 0.0
        held = safe(lambda: track.output_meter_level, 0.0) or 0.0
        if elapsed >= SETTLE_S:
            now = max(left, right)
            self.peak = max(self.peak, now)
            self.power += _amplitude(now) ** 2
            self.count += 1
        if elapsed >= HOLD_FROM_S and self.held is not None and held >= self.held - 1e-6:
            self.peak = max(self.peak, held)
        self.held = held


@command()
def get_meters(ctx, duration=3.0):
    duration = require_number(duration, "duration")
    if not MIN_SECONDS <= duration <= MAX_SECONDS:
        raise BridgeError("bad_args", "duration must be %s-%s seconds, got %s." % (MIN_SECONDS, MAX_SECONDS, duration))
    song = ctx.song
    if not song.is_playing:
        raise BridgeError("invalid_state", "The transport is stopped, so every meter would read silence. Start playback (start_playback, fire_scene or fire_clip), then call get_meters again.")

    groups = (("tracks", list(song.tracks)), ("returns", list(song.return_tracks)), ("master", [song.master_track]))
    meters, no_audio = [], set()
    for group, tracks in groups:
        for i, track in enumerate(tracks):
            meters.append(_Meter(group, i, track))
            if group == "tracks" and not safe(lambda: track.has_audio_output, True):
                no_audio.add(i)  # a MIDI track without an instrument

    start = time.monotonic()
    stopped = False
    while True:
        elapsed = time.monotonic() - start
        for m in meters:
            if not (m.group == "tracks" and m.index in no_audio):
                m.sample(elapsed)
        if not song.is_playing:
            stopped = True
            break
        if elapsed >= duration:
            break
        yield WAIT

    out = {"seconds": rnd(time.monotonic() - start, 1)}
    for group, _ in groups:
        out[group] = []
    for m in meters:
        entry = {"name": m.track.name} if m.group == "master" else {"index": m.index, "name": m.track.name}
        if m.group == "tracks" and m.index in no_audio:
            entry["audio"] = False
        elif m.peak <= 0.0:
            entry["silent"] = True
        else:
            entry["peak_db"] = rnd(meter_db(m.peak), 1)
            if m.count and m.power > 0.0:
                entry["avg_db"] = rnd(10.0 * math.log10(m.power / m.count), 1)
            if entry["peak_db"] > 0.0:
                entry["over"] = True
        if m.group != "master" and safe(lambda: m.track.mute, False):
            entry["mute"] = True
        out[m.group].append(entry)
    out["master"] = out["master"][0]
    if stopped:
        out["note"] = "The transport stopped after %s s, so these readings may be incomplete. Keep the set playing for the whole measurement." % out["seconds"]
    return out


def _amplitude(reading):
    return 0.0 if reading <= 0.0 else 10.0 ** (meter_db(reading) / 20.0)
