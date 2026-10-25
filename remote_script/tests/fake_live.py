"""A small fake of Live's Python API, enough to exercise LiveBridge outside Live.

install() puts fake `Live` and `_Framework.ControlSurface` modules into
sys.modules. It mimics the parts of the real API LiveBridge uses, including
Live's habit of raising RuntimeError for invalid operations.
"""

import math
import os
import sys
import types
import urllib.parse

# ---- parameters ---------------------------------------------------------------------------------


class FakeParam:
    def __init__(self, name, value=0.0, min=0.0, max=1.0, quantized=False, items=(), display=None, enabled=True):
        self.name = name
        self.original_name = name
        self.min = float(min)
        self.max = float(max)
        self.is_quantized = quantized
        self.value_items = tuple(items)
        self.is_enabled = enabled
        self._display = display
        self._value = float(value)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v):
        if v < self.min or v > self.max:
            raise RuntimeError("Invalid value")
        self._value = float(v)

    def str_for_value(self, v):
        if self._display:
            return self._display(v)
        if self.is_quantized and self.value_items:
            return self.value_items[int(round(v - self.min))]
        return "%.2f" % v

    def __str__(self):
        return self.str_for_value(self._value)


def volume_display(v):
    # Monotonic stand-in for Live's fader curve: 0 -> -inf, 0.85 -> 0 dB, 1 -> +6 dB.
    if v <= 0:
        return "-inf dB"
    if v >= 0.85:
        db = (v - 0.85) / 0.15 * 6.0
    else:
        db = 40.0 * math.log10(v / 0.85)
    if db < -70.0:
        return "-inf dB"
    return "%.1f dB" % db


def pan_display(v):
    if abs(v) < 0.005:
        return "C"
    return "%d%s" % (round(abs(v) * 50), "L" if v < 0 else "R")


class FakeMixer:
    def __init__(self, n_sends=0):
        self.volume = FakeParam("Track Volume", 0.85, 0.0, 1.0, display=volume_display)
        self.panning = FakeParam("Track Panning", 0.0, -1.0, 1.0, display=pan_display)
        self.sends = [FakeParam("%s-Send" % chr(65 + i), 0.0, 0.0, 1.0, display=volume_display) for i in range(n_sends)]


# ---- devices ------------------------------------------------------------------------------------


class DeviceType:
    def __init__(self, name):
        self._name = name

    def __str__(self):
        return self._name


class FakeDevice:
    def __init__(self, name, class_name=None, type_="instrument", parameters=None):
        self.name = name
        self.class_name = class_name or name
        self.type = DeviceType(type_)
        self.is_active = True
        self.can_have_chains = False
        self.chains = ()
        self.parameters = parameters if parameters is not None else [FakeParam("Device On", 1.0, 0.0, 1.0, quantized=True, items=("Off", "On"))]


class FakeChain:
    def __init__(self, name, devices=(), in_note=None):
        self.name = name
        self.devices = list(devices)
        self.mute = False
        if in_note is not None:  # Drum Rack pads (DrumChain); -1 is "All Notes"
            self.in_note = in_note


class FakeRack(FakeDevice):
    def __init__(self, name, chains, class_name="InstrumentGroupDevice", type_="instrument"):
        macros = [FakeParam("Macro %d" % (i + 1), 0.0, 0.0, 127.0) for i in range(8)]
        super().__init__(name, class_name, type_, [FakeParam("Device On", 1.0, 0.0, 1.0, quantized=True, items=("Off", "On"))] + macros)
        self.can_have_chains = True
        self.chains = list(chains)


class FakeSimpler(FakeDevice):
    """Simpler's playback mode is a property (0 Classic, 1 One-Shot, 2 Slicing), not a parameter."""

    def __init__(self, name="Simpler"):
        super().__init__(
            name,
            "OriginalSimpler",
            parameters=[
                FakeParam("Device On", 1.0, 0.0, 1.0, quantized=True, items=("Off", "On")),
                FakeParam("Ve Decay", 0.5, 0.0, 1.0, display=time_display),
                FakeParam("Pe < Env", 0.0, -48.0, 48.0),
            ],
        )
        self._playback_mode = 0
        self._slicing_playback_mode = 0

    @property
    def playback_mode(self):
        return self._playback_mode

    @playback_mode.setter
    def playback_mode(self, value):
        if value not in (0, 1, 2):
            raise RuntimeError("Invalid playback mode")
        self._playback_mode = value

    @property
    def slicing_playback_mode(self):
        return self._slicing_playback_mode

    @slicing_playback_mode.setter
    def slicing_playback_mode(self, value):
        if value not in (0, 1, 2):
            raise RuntimeError("Invalid slicing playback mode")
        self._slicing_playback_mode = value


def make_instrument_rack():
    """An 'Arp'-style preset: a rack whose chain holds a MIDI effect, a synth and an effect rack."""
    fx = FakeRack("Space", [FakeChain("Wet", [FakeDevice("Reverb", type_="audio_effect")])], class_name="AudioEffectGroupDevice", type_="audio_effect")
    return FakeRack("Arp", [FakeChain("Lead", [FakeDevice("Arpeggiator", type_="midi_effect"), make_operator(), fx]), FakeChain("Sub")])


def make_drum_rack():
    pads = [FakeChain("Kick", [FakeSimpler("Kick")], in_note=36), FakeChain("Tom", [FakeSimpler("Tom")], in_note=45)]
    return FakeRack("Kit", pads, class_name="DrumGroupDevice")


def freq_display(v):
    # Like Live's filter frequency: exponential 20 Hz-20 kHz, switching to kHz at 1000.
    hz = 20 * 1000**v
    return "%.2f kHz" % (hz / 1000) if hz >= 1000 else "%.1f Hz" % hz


def time_display(v):
    # 1 ms - 60 s, exponential, switching to seconds at 1000 ms.
    ms = 10 ** (v * math.log10(60000))
    return "%.2f s" % (ms / 1000) if ms >= 1000 else "%.1f ms" % ms


SYNC_RATES = ("8", "6", "4", "3", "2", "1.5", "1", "3/4", "1/2", "3/8", "1/3", "5/16", "1/4", "3/16", "1/6", "1/8", "1/12", "1/16", "1/24", "1/32", "1/48", "1/64")


def make_operator():
    return FakeDevice(
        "Operator",
        parameters=[
            FakeParam("Device On", 1.0, 0.0, 1.0, quantized=True, items=("Off", "On")),
            FakeParam("Filter Freq", 0.5, 0.0, 1.0, display=freq_display),
            FakeParam("Filter Type", 0.0, 0.0, 2.0, quantized=True, items=("Lowpass", "Highpass", "Bandpass")),
            FakeParam("Osc-A Level", 1.0, 0.0, 1.0),
            FakeParam("Locked", 0.0, 0.0, 1.0, enabled=False),
            # Live reports synced rates as continuous 0-21 with no value_items.
            FakeParam("LFO Rate", 6.0, 0.0, 21.0, display=lambda v: SYNC_RATES[int(round(v))]),
            FakeParam("Env Decay", 0.5, 0.0, 1.0, display=time_display),
            FakeParam("Transpose", 0.0, -48.0, 48.0, display=lambda v: "%+d st" % round(v) if round(v) else "0 st"),
            # Analog's cutoff: no unit, "k" above 1000 ("22.0k", "400").
            FakeParam("F1 Freq", 1.0, 0.0, 1.0, display=lambda v: "%.1fk" % (30 * 733.3**v / 1000) if 30 * 733.3**v >= 1000 else "%.0f" % (30 * 733.3**v)),
        ],
    )


# ---- notes and clips ----------------------------------------------------------------------------


class MidiNoteSpecification:
    """Like Live's: keyword-constructed and write-only (reading start_time raises AttributeError)."""

    __slots__ = ("_fields",)

    def __init__(self, pitch, start_time, duration, velocity=100, mute=False, probability=1.0, velocity_deviation=0.0, release_velocity=64):
        object.__setattr__(self, "_fields", dict(pitch=pitch, start_time=start_time, duration=duration, velocity=velocity, mute=mute, probability=probability, velocity_deviation=velocity_deviation, release_velocity=release_velocity))


class FakeNote:
    def __init__(self, note_id, spec):
        self.__dict__.update(spec._fields)
        self.note_id = note_id


class FakeClip:
    def __init__(self, length, midi=True, name="", start_time=0.0):
        self.name = name
        self.color = 0x00BFAF
        self.length = float(length)
        self.start_time = float(start_time)  # position on the timeline (arrangement clips)
        self.end_time = float(start_time) + float(length)
        self.start_marker = 0.0
        self.end_marker = float(length)
        self.is_midi_clip = midi
        self.is_audio_clip = not midi
        self.looping = True
        self.loop_start = 0.0
        self.loop_end = float(length)
        self.is_playing = False
        self.is_triggered = False
        self.file_path = None if midi else "/tmp/sample.wav"
        self._notes = []
        self._next_id = 1

    def add_new_notes(self, specs):
        ids = []
        for spec in specs:
            if not isinstance(spec, MidiNoteSpecification):
                raise TypeError("Python argument types did not match C++ signature")
            self._notes.append(FakeNote(self._next_id, spec))
            ids.append(self._next_id)
            self._next_id += 1
        return ids

    def get_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        return tuple(
            n
            for n in self._notes
            if from_pitch <= n.pitch < from_pitch + pitch_span and from_time <= n.start_time < from_time + time_span
        )

    def remove_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        doomed = set(id(n) for n in self.get_notes_extended(from_pitch, pitch_span, from_time, time_span))
        self._notes = [n for n in self._notes if id(n) not in doomed]


class FakeClipSlot:
    def __init__(self, track):
        self._track = track
        self.clip = None
        self.is_playing = False
        self.is_triggered = False
        self.has_stop_button = True

    @property
    def has_clip(self):
        return self.clip is not None

    def create_clip(self, length):
        if not self._track.has_midi_input:
            raise RuntimeError("Can only create MIDI clips on MIDI tracks")
        if self.clip is not None:
            raise RuntimeError("Clip slot is not empty")
        self.clip = FakeClip(length)

    def create_audio_clip(self, path):
        if self._track.has_midi_input:
            raise RuntimeError("Can only create audio clips on audio tracks")
        if self.clip is not None:
            raise RuntimeError("Clip slot is not empty")
        if not os.path.isfile(path):
            raise RuntimeError("Invalid file path")
        self.clip = FakeClip(4.0, midi=False, name=os.path.splitext(os.path.basename(path))[0])
        self.clip.file_path = path

    def fire(self):
        self.is_triggered = True
        if self.clip is not None:
            self.clip.is_triggered = True

    def delete_clip(self):
        if self.clip is None:
            raise RuntimeError("No clip in slot")
        self.clip = None

    def stop(self):
        self.is_triggered = False
        if self.clip is not None:
            self.clip.is_playing = False


# ---- tracks and song ----------------------------------------------------------------------------


class Routing:
    def __init__(self, name):
        self.display_name = name


class FakeTrack:
    def __init__(self, name, kind="midi", n_scenes=8, n_sends=0):
        self.name = name
        self.color = 0x3366FF
        self._mute = False
        self.solo = False
        self.arm = False
        self.kind = kind
        self.has_midi_input = kind == "midi"
        self.has_audio_input = kind in ("audio", "return", "master")
        self.is_foldable = kind == "group"
        self.can_be_armed = kind in ("midi", "audio")
        self.is_grouped = False
        self.group_track = None
        self.devices = []
        self.clip_slots = [FakeClipSlot(self) for _ in range(n_scenes)] if kind in ("midi", "audio", "group") else []
        self.mixer_device = FakeMixer(0 if kind == "master" else n_sends)
        self.playing_slot_index = -1
        self.fired_slot_index = -1
        self.input_routing_type = Routing("All Ins")
        self.output_routing_type = Routing("Master")
        self.arrangement_clips = []
        # Live's meters: momentary peaks per channel, and the larger one held for ~1 s.
        self.output_meter_left = 0.0
        self.output_meter_right = 0.0
        self.output_meter_level = 0.0

    @property
    def has_audio_output(self):
        # A MIDI track only outputs audio once it has an instrument.
        return self.kind != "midi" or any(str(d.type) == "instrument" for d in self.devices)

    @property
    def mute(self):
        if self.kind == "master":
            raise RuntimeError("Master track has no mute")
        return self._mute

    @mute.setter
    def mute(self, value):
        if self.kind == "master":
            raise RuntimeError("Master track has no mute")
        self._mute = value

    def stop_all_clips(self):
        for slot in self.clip_slots:
            slot.stop()

    # Arrangement (Live 11/12 API)

    def create_midi_clip(self, start_time, length):
        if not self.has_midi_input:
            raise RuntimeError("Clips can only be created on MIDI tracks")
        if not 0 <= start_time <= 1576800:
            raise RuntimeError("Time out of range")
        clip = FakeClip(length, midi=True, start_time=start_time)
        self.arrangement_clips.append(clip)
        return clip

    def duplicate_clip_to_arrangement(self, clip, destination_time):
        if clip.is_midi_clip != self.has_midi_input:
            raise RuntimeError("Clip type doesn't match the track")
        copy = FakeClip(clip.length, midi=clip.is_midi_clip, name=clip.name, start_time=destination_time)
        copy._notes = list(clip._notes)
        self.arrangement_clips.append(copy)

    def delete_clip(self, clip):
        self.arrangement_clips.remove(clip)


class FakeScene:
    def __init__(self, song, name=""):
        self._song = song
        self.name = name

    def fire(self, force_legato=False, can_select_scene_on_launch=True):
        index = self._song.scenes.index(self)
        for track in self._song.tracks:
            if index < len(track.clip_slots) and track.clip_slots[index].has_clip:
                track.clip_slots[index].fire()
        self._song.is_playing = True


SCALES = ("Major", "Minor", "Dorian", "Mixolydian", "Lydian", "Phrygian", "Locrian", "Harmonic Minor")


class SongView:
    def __init__(self):
        self.selected_track = None
        self.highlighted_clip_slot = None


class FakeSong:
    def __init__(self, n_scenes=8):
        self.n_scenes = n_scenes
        self._tempo = 120.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.is_playing = False
        self.current_song_time = 0.0
        self.loop = False
        self.loop_start = 0.0
        self.loop_length = 16.0
        self.metronome = False
        self.root_note = 0
        self._scale_name = "Major"
        self.scenes = [FakeScene(self) for _ in range(n_scenes)]
        self.return_tracks = [FakeTrack("A-Reverb", "return", n_sends=2), FakeTrack("B-Delay", "return", n_sends=2)]
        self.tracks = [FakeTrack("1-MIDI", "midi", n_scenes, n_sends=2), FakeTrack("2-Audio", "audio", n_scenes, n_sends=2)]
        self.master_track = FakeTrack("Master", "master")
        self.view = SongView()
        self.view.selected_track = self.tracks[0]
        self.undo_log = []
        self.undo_depth = 0
        self.undo_steps = 0
        self.can_redo = False

    @property
    def tempo(self):
        return self._tempo

    @tempo.setter
    def tempo(self, value):
        if not 20 <= value <= 999:
            raise RuntimeError("Tempo out of range")
        self._tempo = float(value)
        self.undo_log.append(("tempo", value))

    @property
    def scale_name(self):
        return self._scale_name

    @scale_name.setter
    def scale_name(self, name):
        match = [s for s in SCALES if s.lower() == name.lower()]
        if not match:
            raise RuntimeError("Invalid scale name")
        self._scale_name = match[0]

    @property
    def can_undo(self):
        return self.undo_steps > 0

    def begin_undo_step(self):
        self.undo_depth += 1

    def end_undo_step(self):
        self.undo_depth -= 1
        self.undo_steps += 1

    def undo(self):
        self.undo_steps -= 1
        self.can_redo = True

    def _new_track(self, kind, index):
        track = FakeTrack("%d-%s" % (len(self.tracks) + 1, kind.upper() if kind == "midi" else "Audio"), kind, self.n_scenes, len(self.return_tracks))
        if index == -1:
            self.tracks.append(track)
        elif 0 <= index <= len(self.tracks):
            self.tracks.insert(index, track)
        else:
            raise RuntimeError("Index out of range")

    def create_midi_track(self, index=-1):
        self._new_track("midi", index)

    def delete_track(self, index):
        if len(self.tracks) == 1:
            raise RuntimeError("Cannot delete the last track")
        del self.tracks[index]

    def delete_return_track(self, index):
        del self.return_tracks[index]
        for track in self.tracks + self.return_tracks:
            del track.mixer_device.sends[index]

    def create_scene(self, index=-1):
        scene = FakeScene(self)
        at = len(self.scenes) if index == -1 else index
        self.scenes.insert(at, scene)
        for track in self.tracks:
            track.clip_slots.insert(at, FakeClipSlot(track))
        return scene

    def delete_scene(self, index):
        if len(self.scenes) == 1:
            raise RuntimeError("Cannot delete the last scene")
        del self.scenes[index]
        for track in self.tracks:
            del track.clip_slots[index]

    def create_audio_track(self, index=-1):
        self._new_track("audio", index)

    def start_playing(self):
        self.is_playing = True

    def stop_playing(self):
        self.is_playing = False


# ---- browser ------------------------------------------------------------------------------------


class FakeBrowserItem:
    """`tempo`: a loop whose tempo Live adopts when it's the first clip in the set."""

    def __init__(self, name, children=(), loadable=False, device=False, folder=None, factory=None, tempo=None, uri=None):
        self.name = name
        self.uri = uri or "query:" + name
        self._children = list(children)
        self.is_loadable = loadable
        self.is_device = device
        self.is_folder = bool(children) and not device if folder is None else folder
        self.factory = factory
        self.tempo = tempo

    @property
    def children(self):
        return tuple(self._children)

    @property
    def iter_children(self):
        return iter(self._children)


def _instrument(name):
    return FakeBrowserItem(name, children=[FakeBrowserItem("%s Preset" % name, loadable=True)], loadable=True, device=True, factory=lambda: make_operator() if name == "Operator" else FakeDevice(name))


class FakeBrowser:
    def __init__(self, song):
        self._song = song
        self.instruments = FakeBrowserItem("Instruments", [_instrument("Operator"), _instrument("Wavetable"), _instrument("Drift")], folder=True)
        self.audio_effects = FakeBrowserItem(
            "Audio Effects",
            [FakeBrowserItem("EQ Eight", loadable=True, device=True, factory=lambda: FakeDevice("EQ Eight", type_="audio_effect")), FakeBrowserItem("Reverb", loadable=True, device=True, factory=lambda: FakeDevice("Reverb", type_="audio_effect"))],
            folder=True,
        )
        self.midi_effects = FakeBrowserItem("MIDI Effects", [FakeBrowserItem("Arpeggiator", loadable=True, device=True)], folder=True)
        self.plugins = FakeBrowserItem("Plugins", [FakeBrowserItem("VST3", [FakeBrowserItem("Serum 2", loadable=True, device=True)], folder=True)], folder=True)
        self.user_library = FakeBrowserItem("User Library", [FakeBrowserItem("Presets", [FakeBrowserItem("Lo/Fi Keys.adg", loadable=True)], folder=True)], folder=True)
        drums = [FakeBrowserItem("Kick %02d.wav" % i, loadable=True) for i in range(250)]
        loops = [FakeBrowserItem("Cymbal Loop.wav", loadable=True, tempo=92.675)]
        # The uri points at a folder that doesn't exist (an unmounted drive) until mount_user_folder().
        samples = FakeBrowserItem("Samples", [FakeBrowserItem("Drums", drums, folder=True), FakeBrowserItem("Loops", loops, folder=True)], folder=True, uri="userfolder:/Volumes/Not%20Mounted/Samples")
        self.user_folders = (samples,)
        self.samples = FakeBrowserItem("Samples", [FakeBrowserItem("80s Beat 90 bpm.wav", loadable=True, tempo=90.0, uri="query:Samples#FileId_3983")], folder=True)
        self.sounds = FakeBrowserItem("Sounds", [], folder=True)
        self.hotswap_target = None
        self.loaded = []

    def mount_user_folder(self, root):
        """Write every sample in the user folder under `root` and point the folder's uri there."""
        folder = self.user_folders[0]
        folder.uri = "userfolder:" + urllib.parse.quote(os.path.join(root, folder.name))

        def write(item, path):
            for child in item.children:
                if child.is_folder:
                    write(child, os.path.join(path, child.name))
                else:
                    os.makedirs(path, exist_ok=True)
                    with open(os.path.join(path, child.name), "wb") as f:
                        f.write(b"RIFF")

        write(folder, os.path.join(root, folder.name))

    def load_item(self, item):
        # Like Live 12.4.7: the load is an undo step of its own, whatever undo step the script has
        # open, and the first loop in a set also sets the tempo as a second step.
        self.loaded.append(item.name)
        song = self._song
        song.undo_steps += 1
        track = song.view.selected_track
        if item.name.lower().endswith(".wav"):
            slot = song.view.highlighted_clip_slot
            if track.has_midi_input:
                track.devices.append(FakeDevice("Simpler"))
            elif slot is not None:
                first = not any(s.has_clip for t in song.tracks for s in t.clip_slots)
                slot.clip = FakeClip(4.0, midi=False, name=os.path.splitext(item.name)[0])
                if first and item.tempo:
                    song._tempo = item.tempo
                    song.undo_steps += 1
            return
        if item.factory:
            track.devices.append(item.factory())
        else:
            track.devices.append(FakeDevice(item.name))


class AppView:
    def __init__(self):
        self.shown = []

    def show_view(self, name):
        self.shown.append(name)


class FakeApplication:
    def __init__(self, song):
        self.browser = FakeBrowser(song)
        self.view = AppView()

    def get_major_version(self):
        return 12

    def get_minor_version(self):
        return 4

    def get_bugfix_version(self):
        return 7


# ---- module installation ------------------------------------------------------------------------


class FakeControlSurface:
    """Mimics _Framework.ControlSurface's scheduling: tick() runs due callbacks."""

    song_factory = FakeSong

    def __init__(self, c_instance):
        self._c_instance = c_instance
        self._song = STATE["song"]
        self._scheduled = []
        self.messages = []
        self.log_lines = []
        self.disconnected = False

    def song(self):
        return self._song

    def schedule_message(self, delay_in_ticks, callback, parameter=None):
        self._scheduled.append([delay_in_ticks, callback])

    def tick(self):
        due, keep = [], []
        for entry in self._scheduled:
            entry[0] -= 1
            (due if entry[0] <= 0 else keep).append(entry)
        self._scheduled = keep
        for _, callback in due:
            callback()

    def log_message(self, *args):
        self.log_lines.append(" ".join(str(a) for a in args))

    def show_message(self, message):
        self.messages.append(message)

    def disconnect(self):
        self.disconnected = True


STATE = {}


def reset(n_scenes=8):
    song = FakeSong(n_scenes)
    STATE["song"] = song
    STATE["app"] = FakeApplication(song)
    return song, STATE["app"]


def install():
    live = types.ModuleType("Live")
    live.Application = types.ModuleType("Live.Application")
    live.Application.get_application = lambda: STATE["app"]
    live.Clip = types.ModuleType("Live.Clip")
    live.Clip.MidiNoteSpecification = MidiNoteSpecification
    sys.modules["Live"] = live
    sys.modules["Live.Application"] = live.Application
    sys.modules["Live.Clip"] = live.Clip

    framework = types.ModuleType("_Framework")
    control_surface = types.ModuleType("_Framework.ControlSurface")
    control_surface.ControlSurface = FakeControlSurface
    framework.ControlSurface = control_surface
    sys.modules["_Framework"] = framework
    sys.modules["_Framework.ControlSurface"] = control_surface
    reset()
