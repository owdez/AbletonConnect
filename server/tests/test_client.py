"""LiveClient against a fake LiveBridge socket: ids, timeouts, reconnects, diagnostics."""

import asyncio
import socket

import pytest
from fake_bridge import FakeBridge

from abletonconnect.client import ConnectionLost, LiveClient, LiveCommandError, LiveTimeout, LiveUnavailable


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def bridge():
    fb = await FakeBridge().start()
    yield fb
    await fb.stop()


def client_for(port: int, **kw) -> LiveClient:
    kw.setdefault("live_running", lambda: True)
    return LiveClient(port=port, **kw)


async def test_round_trip_and_handshake_info(bridge):
    c = client_for(bridge.port)
    assert await c.request("get_session_info", mutating=False) == {"cmd": "get_session_info", "args": {}}
    assert c.info["live_version"] == "12.4.7"
    await c.close()


async def test_concurrent_requests_matched_by_id_out_of_order(bridge):
    async def slow_first(req, fb):
        await asyncio.sleep(0.2 if req["args"]["n"] == 0 else 0)
        return {"id": req["id"], "ok": True, "result": req["args"]["n"]}

    # Answer concurrently so replies arrive out of order.
    async def handler(req, fb):
        asyncio.ensure_future(_reply_later(req, fb))
        return None

    async def _reply_later(req, fb):
        reply = await slow_first(req, fb)
        for w in fb._writers:
            await FakeBridge._send(w, reply)

    bridge.handler = handler
    c = client_for(bridge.port)
    results = await asyncio.gather(*(c.request("x", {"n": n}, mutating=False) for n in range(3)))
    assert results == [0, 1, 2]
    await c.close()


async def test_remote_error_becomes_live_command_error(bridge):
    bridge.handler = lambda req, fb: {
        "id": req["id"],
        "ok": False,
        "error": {"type": "not_found", "message": "No track 9.", "where": "get_track_info"},
    }
    c = client_for(bridge.port)
    with pytest.raises(LiveCommandError) as e:
        await c.request("get_track_info", {"track_index": 9}, mutating=False)
    assert e.value.type == "not_found" and str(e.value) == "No track 9."
    await c.close()


async def test_timeout_then_late_reply_is_ignored(bridge):
    held = []

    def handler(req, fb):
        if req["cmd"] == "slow":
            held.append(req)
            return None
        return {"id": req["id"], "ok": True, "result": "fast"}

    bridge.handler = handler
    c = client_for(bridge.port)
    with pytest.raises(LiveTimeout) as e:
        await c.request("slow", mutating=True, timeout=0.2)
    assert "may still be applied" in str(e.value)
    for w in bridge._writers:  # the late answer arrives now
        await FakeBridge._send(w, {"id": held[0]["id"], "ok": True, "result": "late"})
    assert await c.request("fast", mutating=False) == "fast"
    assert bridge.connections == 1  # same connection survived the timeout
    await c.close()


async def test_read_only_command_retried_after_drop(bridge):
    def handler(req, fb):
        if len(fb.requests) == 1:
            fb.drop_all()
            return None
        return {"id": req["id"], "ok": True, "result": "second try"}

    bridge.handler = handler
    c = client_for(bridge.port)
    assert await c.request("get_session_info", mutating=False) == "second try"
    assert len(bridge.requests) == 2 and bridge.connections == 2
    await c.close()


async def test_mutating_command_not_resent_after_drop(bridge):
    def handler(req, fb):
        fb.drop_all()
        return None

    bridge.handler = handler
    c = client_for(bridge.port)
    with pytest.raises(ConnectionLost) as e:
        await c.request("create_midi_track", mutating=True)
    assert "may or may not have been applied" in str(e.value)
    assert len(bridge.requests) == 1


async def test_reconnects_after_live_restart():
    port = free_port()
    fb = await FakeBridge().start(port)
    c = client_for(port)
    assert await c.request("ping", mutating=False)
    await fb.stop()
    await asyncio.sleep(0.05)
    assert not c.connected  # noticed the close while idle
    fb2 = await FakeBridge().start(port)
    assert await c.request("ping", mutating=True)  # mutating is fine: the old connection was known dead
    await fb2.stop()
    await c.close()


async def test_live_not_running_message():
    c = client_for(free_port(), live_running=lambda: False)
    with pytest.raises(LiveUnavailable) as e:
        await c.request("ping", mutating=False)
    assert "Ableton Live isn't running" in str(e.value)


async def test_script_not_loaded_message():
    port = free_port()
    c = client_for(port, live_running=lambda: True)
    with pytest.raises(LiveUnavailable) as e:
        await c.request("ping", mutating=False)
    msg = str(e.value)
    assert "Live is running" in msg and "Control Surface" in msg and "restart Live" in msg and str(port) in msg


async def test_protocol_mismatch():
    fb = await FakeBridge(info={"protocol": 99, "script_version": "9", "live_version": "12"}).start()
    c = client_for(fb.port)
    with pytest.raises(LiveUnavailable) as e:
        await c.request("ping", mutating=False)
    assert "protocol 99" in str(e.value) and "install.sh" in str(e.value)
    await fb.stop()


async def test_something_else_on_the_port():
    async def http_server(reader, writer):
        await reader.readline()
        writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(http_server, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    c = client_for(port)
    with pytest.raises(LiveUnavailable) as e:
        await c.request("ping", mutating=False)
    assert "isn't LiveBridge" in str(e.value)
    server.close()
    await server.wait_closed()


async def test_silent_listener_times_out_handshake():
    async def silent(reader, writer):
        await asyncio.sleep(5)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    c = client_for(port, handshake_timeout=0.2)
    with pytest.raises(LiveUnavailable):
        await c.request("ping", mutating=False)
    server.close()
