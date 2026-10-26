"""Full stack without Live: MCP tool -> LiveClient -> TCP -> real LiveBridge server, dispatcher and
handlers -> fake Live Object Model. Catches protocol drift between the two halves."""

import contextlib
import json
import queue
import sys
import threading
import time
from pathlib import Path

import pytest
from mcp import Client

REMOTE = Path(__file__).resolve().parents[2] / "remote_script"
sys.path[:0] = [str(REMOTE), str(REMOTE / "tests")]

import fake_live  # noqa: E402

fake_live.install()

from LiveBridge import handlers  # noqa: E402, F401
from LiveBridge.dispatch import Context, Dispatcher  # noqa: E402
from LiveBridge.log import BridgeLog  # noqa: E402
from LiveBridge.server import BridgeServer  # noqa: E402

import abletonconnect.server as mcp_server  # noqa: E402
from abletonconnect.client import LiveClient  # noqa: E402


class MainThread:
    """Plays Live's main thread: drains requests and advances jobs every 10 ms."""

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.requests = queue.Queue()
        self.thread_ids = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, request, reply):
        self.requests.put((request, reply))

    def _run(self):
        while not self._stop.is_set():
            deadline = time.monotonic() + 0.04
            while True:
                try:
                    request, reply = self.requests.get_nowait()
                except queue.Empty:
                    break
                self.thread_ids.add(threading.get_ident())
                self.dispatcher.handle(request, reply)
            self.dispatcher.run_jobs(deadline)
            self._stop.wait(0.01)

    def stop(self):
        self._stop.set()
        self._thread.join(1)


@pytest.fixture
def stack(monkeypatch):
    song, app = fake_live.reset()
    dispatcher = Dispatcher(Context(lambda: song, lambda: app), BridgeLog())
    main = MainThread(dispatcher)
    srv = BridgeServer(0, main.submit, {"protocol": 1, "script_version": "test", "live_version": "12.4.7"}, BridgeLog())
    srv.start()
    monkeypatch.setattr(mcp_server, "live", LiveClient(port=srv.port, live_running=lambda: True))
    yield song, main
    srv.stop()
    main.stop()


@contextlib.asynccontextmanager
async def connect():
    # Entered inside each test: anyio cancel scopes can't span a fixture's setup and teardown tasks.
    async with Client(mcp_server.mcp) as client:
        yield client
    await mcp_server.live.close()


async def tool(client, tool_name, **args):
    result = await client.call_tool(tool_name, args)
    text = result.content[0].text
    if result.is_error:
        return {"error": text}
    return json.loads(text)


async def test_tool_output_is_compact_json(stack):
    async with connect() as client:
        result = await client.call_tool("get_session_info", {})
        text = result.content[0].text
        assert "\n" not in text and ": " not in text
        assert result.structured_content is None


async def test_build_a_clip_through_every_layer(stack):
    async with connect() as client:
        song, main = stack
        track = await tool(client, "create_midi_track", name="Keys")
        index = track["created"]["index"]
        await tool(client, "create_clip", track_index=index, clip_slot_index=0, length=4)
        notes = [{"pitch": 60 + i, "start": i * 0.5, "duration": 0.5} for i in range(4)]
        added = await tool(client, "add_notes", track_index=index, clip_slot_index=0, notes=notes)
        assert added["clip"]["note_count"] == 4
        got = await tool(client, "get_clip_notes", track_index=index, clip_slot_index=0)
        assert [n["pitch"] for n in got["notes"]] == [60, 61, 62, 63]
        assert song.undo_steps == 3
        assert len(main.thread_ids) == 1  # every LOM call ran on the one "main" thread


async def test_browser_load_and_sound_design(stack):
    async with connect() as client:
        song, _ = stack
        found = await tool(client, "search_browser", query="operator", categories=["instruments"])
        path = found["results"][0]["path"]
        loaded = await tool(client, "load_browser_item", path=path, track_index=0)
        assert loaded["devices"][0]["name"] == "Operator"
        params = await tool(client, "get_device_parameters", track_index=0, device_index=0, name_filter="filter")
        assert {p["name"] for p in params["parameters"]} == {"Filter Freq", "Filter Type"}
        changed = await tool(client, "set_device_parameter", track_index=0, parameter="Filter Type", value="Highpass")
        assert changed["parameter"]["display"] == "Highpass" and changed["previous"]["display"] == "Lowpass"


async def test_mixer_and_transport(stack):
    async with connect() as client:
        song, _ = stack
        mixed = await tool(client, "set_mixer", track_index=1, volume_db=-3, pan=0.2, sends={0: 0.5}, solo=True)
        assert mixed["volume"]["display"] == "-3.0 dB" and mixed["sends"][0]["value"] == 0.5 and mixed["solo"] is True
        assert (await tool(client, "start_playback"))["playing"] is True
        assert (await tool(client, "stop_playback"))["playing"] is False
        assert (await tool(client, "set_tempo", bpm=140))["tempo"] == 140
        assert (await tool(client, "undo"))["undone"] is True


async def test_errors_reach_claude_as_tool_errors(stack):
    async with connect() as client:
        out = await tool(client, "get_track_info", track_index=42)
        assert "No track 42" in out["error"]
        out = await tool(client, "set_device_parameter", track_index=0, parameter=0)
        assert "exactly one" in out["error"]
        out = await tool(client, "get_track_info")
        assert "track_index is required" in out["error"]


async def test_clip_envelopes_through_every_layer(stack):
    async with connect() as client:
        song, _ = stack
        await tool(client, "create_clip", track_index=0, clip_slot_index=0, length=16, name="Build")
        ramp = {"start": 0, "end": 16, "from": "-24 dB", "to": "-6 dB", "curve": "ease_in"}
        drawn = await tool(client, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="volume", ramp=ramp)
        points = drawn["envelope"]["points"]
        assert (points[0]["display"], points[-1]["display"], len(points)) == ("-24.0 dB", "-6.0 dB", 17)
        throw = [
            {"time": 12, "value": "-inf dB", "curve": "hold"},
            {"time": 14, "value": "-3 dB"},
            {"time": 16, "value": "-inf dB"},
        ]
        await tool(client, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="send A", points=throw)
        listed = await tool(client, "get_clip_envelope", track_index=0, clip_slot_index=0)
        assert [e["parameter"] for e in listed["envelopes"]] == ["Volume", "Send A"]
        out = await tool(client, "set_clip_envelope", track_index=0, clip_slot_index=0, parameter="pan")
        assert "exactly one" in out["error"]
        out = await tool(
            client, "get_clip_envelope", track_index=0, clip_slot_index=0, parameter=0, device_index=0, device_path=[0]
        )
        assert "not both" in out["error"]
        cleared = await tool(client, "clear_clip_envelope", track_index=0, clip_slot_index=0)
        assert len(cleared["cleared"]) == 2 and cleared["envelopes_left"] == []
        assert song.undo_steps == 4


async def test_tool_list_matches_bridge_commands(stack):
    async with connect() as client:
        from LiveBridge.dispatch import COMMANDS

        tools = {t.name for t in (await client.list_tools()).tools}
        local = {"search_samples"}  # answered from the sample index, not by LiveBridge
        assert len(tools) == 38
        assert tools - local <= set(COMMANDS)


async def test_transport_through_every_layer(stack):
    async with connect() as client:
        song, _ = stack
        out = await tool(client, "jump_to", bar=25)
        assert (out["song_time"], out["bar"]) == (96.0, 25.0)
        out = await tool(client, "start_playback")
        assert out["playing"] is True and out["bar"] == 25.0
        out = await tool(client, "start_playback", bar=9)
        assert out["song_time"] == 32.0
        looped = await tool(client, "set_arrangement_loop", start_bar=17, length_bars=8, enabled=True)
        assert looped["loop"] == {"on": True, "start_bar": 17.0, "end_bar": 25.0}
        song.back_to_arranger = True
        assert (await tool(client, "get_session_info"))["back_to_arrangement"] is True
        assert (await tool(client, "back_to_arrangement"))["was_overridden"] is True
        assert "not both" in (await tool(client, "jump_to", bar=2, beat=4))["error"]
        assert "greater than or equal to 1" in (await tool(client, "jump_to", bar=0))["error"]


async def test_transport_tools_are_never_resent(stack, monkeypatch):
    # The client resends only read-only commands after a reconnect; moving the playhead or the
    # loop twice, or pressing Back to Arrangement twice, must not happen silently.
    sent = []
    request = mcp_server.live.request

    async def spy(cmd, args, *, mutating, timeout=None):
        sent.append((cmd, mutating))
        return await request(cmd, args, mutating=mutating, timeout=timeout)

    monkeypatch.setattr(mcp_server.live, "request", spy)
    async with connect() as client:
        await tool(client, "jump_to", beat=4)
        await tool(client, "start_playback", bar=2)
        await tool(client, "set_arrangement_loop", enabled=True)
        await tool(client, "back_to_arrangement")
        await tool(client, "get_session_info")
    assert sent == [
        ("jump_to", True),
        ("start_playback", True),
        ("set_arrangement_loop", True),
        ("back_to_arrangement", True),
        ("get_session_info", False),
    ]


async def test_loading_leaves_arm_and_selection_alone(stack):
    async with connect() as client:
        song, _ = stack
        song.view.selected_track = song.tracks[1]
        out = await tool(client, "load_browser_item", path="instruments/Operator", track_index=0)
        assert out["arm"] is False and out["arm_restored"] == ["1-MIDI"]
        assert song.view.selected_track is song.tracks[1] and not any(t.arm for t in song.tracks)
        info = await tool(client, "get_session_info")
        assert info["selected_track"] == {"type": "track", "index": 1}


async def test_song_layout_through_every_layer(stack):
    async with connect() as client:
        song, main = stack
        await tool(client, "create_clip", track_index=0, clip_slot_index=0, length=4, name="Intro")
        notes = [{"pitch": 36, "start": float(i), "duration": 0.25} for i in range(4)]
        await tool(client, "add_notes", track_index=0, clip_slot_index=0, notes=notes)
        assert (await tool(client, "set_scene", scene_index=0, name="Intro"))["name"] == "Intro"
        assert (await tool(client, "set_track", track_index=0, name="Kick"))["name"] == "Kick"
        assert (await tool(client, "set_scale", root="E", scale="Phrygian"))["scale"] == "E Phrygian"
        laid = await tool(client, "arrange_scenes", sections=[{"scene": 0, "bars": 8}])
        assert laid["sections"][0] == {"scene": 0, "name": "Intro", "start_bar": 1.0, "bars": 8.0, "clips": 1}
        info = await tool(client, "get_track_info", track_index=0)
        assert info["arrangement"] == [{"index": 0, "name": "Intro", "start_bar": 1.0, "end_bar": 9.0}]
        renamed = await tool(client, "set_clip", track_index=0, arrangement_index=0, name="Intro A")
        assert renamed["clip"]["name"] == "Intro A"
        out = await tool(client, "set_device_parameter", track_index=0, parameter="Filter Freq", value="120 Hz")
        assert "No device 0" in out["error"]  # no device yet: errors still come back as tool errors
        deleted = await tool(client, "delete_track", track_index=1)
        assert deleted["remaining"] == [{"index": 0, "name": "Kick"}]


async def test_racks_simpler_and_meters_through_every_layer(stack):
    async with connect() as client:
        song, _ = stack
        song.tracks[0].devices = [fake_live.make_instrument_rack(), fake_live.make_drum_rack()]
        info = await tool(client, "get_track_info", track_index=0)
        tom = info["devices"][1]["chains"][1]["devices"][0]
        assert tom["path"] == [1, 1, 0] and tom["playback_mode"] == "Classic"
        changed = await tool(
            client,
            "set_device_property",
            track_index=0,
            device_path=tom["path"],
            property="playback_mode",
            value="One-Shot",
        )
        assert changed["property"]["value"] == "One-Shot" and changed["previous"] == "Classic"
        out = await tool(
            client,
            "set_device_parameter",
            track_index=0,
            device_path=[0, 0, 1],
            parameter="Filter Type",
            value="Highpass",
        )
        assert out["parameter"]["display"] == "Highpass" and out["path"] == [0, 0, 1]
        out = await tool(client, "get_device_parameters", track_index=0, device_index=0, device_path=[0])
        assert "not both" in out["error"]

        assert "transport is stopped" in (await tool(client, "get_meters"))["error"]
        song.is_playing = True
        song.master_track.output_meter_left = song.master_track.output_meter_right = 64 / 76  # -6 dBFS
        meters = await tool(client, "get_meters", duration=2.5)
        assert meters["master"] == {"name": "Master", "peak_db": -6.0, "avg_db": -6.0} and len(meters["tracks"]) == 2
