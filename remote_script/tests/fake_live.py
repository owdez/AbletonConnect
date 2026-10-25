"""A small fake of Live's Python API, enough to exercise LiveBridge outside Live.

install() puts fake `Live` and `_Framework.ControlSurface` modules into
sys.modules. It mimics the parts of the real API LiveBridge uses, including
Live's habit of raising RuntimeError for invalid operations.
"""

import math
import sys
import types

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


def make_operator():
    return FakeDevice(
        "Operator",
        parameters=[
            FakeParam("Device On", 1.0, 0.0, 1.0, quantized=True, items=("Off", "On")),
            FakeParam("Filter Freq", 0.5, 0.0, 1.0, display=lambda v: "%.0f Hz" % (20 * 1000 ** v)),
            FakeParam("Filter Type", 0.0, 0.0, 2.0, quantized=True, items=("Lowpass", "Highpass", "Bandpass")),
            FakeParam("Osc-A Level", 1.0, 0.0, 1.0),
            FakeParam("Locked", 0.0, 0.0, 1.0, enabled=False),
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
    def __init__(self, length, midi=True, name=""):
        self.name = name
        self.length = float(length)
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

    def fire(self):
        self.is_triggered = True
        if self.clip is not None:
            self.clip.is_triggered = True

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
        self.scale_name = "Major"
        self.scenes = [object() for _ in range(n_scenes)]
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

    def create_audio_track(self, index=-1):
        self._new_track("audio", index)

    def start_playing(self):
        self.is_playing = True

    def stop_playing(self):
        self.is_playing = False


# ---- browser ------------------------------------------------------------------------------------


class FakeBrowserItem:
    def __init__(self, name, children=(), loadable=False, device=False, folder=None, factory=None):
        self.name = name
        self.uri = "query:" + name
        self._children = list(children)
        self.is_loadable = loadable
        self.is_device = device
        self.is_folder = bool(children) and not device if folder is None else folder
        self.factory = factory

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
        self.user_folders = (FakeBrowserItem("Samples", [FakeBrowserItem("Drums", drums, folder=True)], folder=True),)
        self.sounds = FakeBrowserItem("Sounds", [], folder=True)
        self.hotswap_target = None
        self.loaded = []

    def load_item(self, item):
        self.loaded.append(item.name)
        track = self._song.view.selected_track
        if item.name.lower().endswith(".wav"):
            slot = self._song.view.highlighted_clip_slot
            if track.has_midi_input:
                track.devices.append(FakeDevice("Simpler"))
            elif slot is not None:
                slot.clip = FakeClip(4.0, midi=False, name=item.name)
            return
        if item.factory:
            track.devices.append(item.factory())
        else:
            track.devices.append(FakeDevice(item.name))


class FakeApplication:
    def __init__(self, song):
        self.browser = FakeBrowser(song)

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
