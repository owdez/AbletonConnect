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


async def test_tool_list_matches_bridge_commands(stack):
    async with connect() as client:
        from LiveBridge.dispatch import COMMANDS

        tools = {t.name for t in (await client.list_tools()).tools}
        local = {"search_samples"}  # answered from the sample index, not by LiveBridge
        assert len(tools) == 20
        assert tools - local <= set(COMMANDS)
