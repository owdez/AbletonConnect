"""LiveBridge ControlSurface: startup, the main-thread pump, crash safety, shutdown."""

import json
import socket
import subprocess
import sys
import time

import pytest
from helpers import LineClient

import LiveBridge.bridge as bridge_mod
from LiveBridge import create_instance
from LiveBridge.dispatch import COMMANDS, command


@pytest.fixture
def make_bridge(live, monkeypatch):
    created = []

    def make(port=0):
        monkeypatch.setattr(bridge_mod, "load_port", lambda: (port, None))
        b = create_instance(object())
        created.append(b)
        return b

    yield make
    for b in created:
        if not b._closing:
            b.disconnect()


def request(b, client, cmd, args=None, rid=1, ticks=200):
    """Send a request and drive Live's ticks from this (the 'main') thread."""
    client.send({"id": rid, "cmd": cmd, "args": args or {}})
    client.sock.settimeout(0.01)
    for _ in range(ticks):
        b.tick()
        try:
            return client.recv()
        except (socket.timeout, BlockingIOError):
            continue
    raise AssertionError("no reply to %s" % cmd)


def connect(b):
    c = LineClient(b._server.port)
    c.send({"id": "h", "cmd": "hello", "args": {"protocol": 1}})
    info = c.recv()
    assert info["ok"]
    return c, info["result"]


def test_starts_server_and_answers_through_the_pump(make_bridge):
    b = make_bridge()
    c, info = connect(b)
    assert info["live_version"] == "12.4.7"
    reply = request(b, c, "get_session_info")
    assert reply["ok"] and reply["result"]["tempo"] == 120.0
    assert any("listening on 127.0.0.1" in line for line in b.log_lines)


def test_lom_is_only_touched_inside_ticks(make_bridge):
    b = make_bridge()
    c, _ = connect(b)
    c.send({"id": 1, "cmd": "set_tempo", "args": {"bpm": 99}})
    time.sleep(0.2)
    assert b.song().tempo == 120.0  # nothing happens until Live's main thread ticks
    c.sock.settimeout(0.01)
    for _ in range(50):
        b.tick()
        try:
            reply = c.recv()
            break
        except socket.timeout:
            continue
    assert reply["ok"] and b.song().tempo == 99.0


def test_handler_exception_becomes_error_and_pump_keeps_running(make_bridge):
    @command("boom_test")
    def boom(ctx):
        raise ValueError("kaboom")

    try:
        b = make_bridge()
        c, _ = connect(b)
        reply = request(b, c, "boom_test")
        assert reply["ok"] is False
        assert reply["error"]["type"] == "internal"
        assert "kaboom" in reply["error"]["message"]
        assert request(b, c, "ping", rid=2)["result"] == {"pong": True}
        assert any("boom_test failed" in line and "Traceback" in line for line in b.log_lines)
    finally:
        COMMANDS.pop("boom_test", None)


def test_pump_survives_a_failing_tick(make_bridge, monkeypatch):
    b = make_bridge()
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise RuntimeError("tick broke")

    monkeypatch.setattr(b, "_tick", broken)
    for _ in range(5):
        b.tick()
    assert calls["n"] == 5  # rescheduled every time
    assert sum("pump tick failed" in line for line in b.log_lines) == 1  # logged once, not every tick


def test_busy_port_does_not_crash_and_retries(make_bridge):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    b = make_bridge(port)
    assert b._server is None
    assert any("busy" in m for m in b.messages)
    blocker.close()
    for _ in range(bridge_mod.BIND_RETRY_TICKS + 2):
        b.tick()
    assert b._server is not None and b._server.port == port


def test_disconnect_stops_server(make_bridge):
    b = make_bridge()
    c, _ = connect(b)
    b.disconnect()
    assert c.is_closed()
    assert b.disconnected
    before = len(b._scheduled)
    b.tick()
    assert len(b._scheduled) <= before  # pump stops rescheduling


def test_tick_budget_spreads_a_burst(make_bridge, monkeypatch):
    @command("slow_test")
    def slow(ctx):
        time.sleep(0.015)
        return {}

    try:
        b = make_bridge()
        c, _ = connect(b)
        for i in range(10):
            c.send({"id": i, "cmd": "slow_test"})
        time.sleep(0.2)
        started = time.monotonic()
        b.tick()
        assert time.monotonic() - started < 0.1  # one tick doesn't run all ten
        assert b._requests.qsize() > 0
    finally:
        COMMANDS.pop("slow_test", None)


CLIENT = r"""
import json, socket, sys, time
def rt(s, obj):
    t = time.monotonic(); s.sendall((json.dumps(obj) + "\n").encode()); buf = b""
    while b"\n" not in buf: buf += s.recv(65536)
    return time.monotonic() - t
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
hello = rt(s, {"id": "h", "cmd": "hello", "args": {"protocol": 1}})
pings = [rt(s, {"id": i, "cmd": "ping", "args": {}}) for i in range(5)]
print(json.dumps({"hello": hello, "ping": max(pings)}))
"""


def run_starved(b, yield_s, monkeypatch):
    """Tick like Live: the main thread holds the GIL between ticks; other threads only run when it lets go."""
    monkeypatch.setattr(bridge_mod, "GIL_YIELD_S", yield_s)
    old = sys.getswitchinterval()
    proc = subprocess.Popen([sys.executable, "-c", CLIENT, str(b._server.port)], stdout=subprocess.PIPE, text=True)
    sys.setswitchinterval(5.0)  # no preemptive GIL handoff, like Live's C++ main loop
    try:
        give_up = time.monotonic() + 10
        while proc.poll() is None and time.monotonic() < give_up:
            busy_until = time.monotonic() + 0.1
            while time.monotonic() < busy_until:
                pass
            b.tick()
    finally:
        sys.setswitchinterval(old)
    if proc.poll() is None:
        proc.kill()
        return None
    return json.loads(proc.stdout.read())


def test_socket_threads_get_the_gil_every_tick(make_bridge, monkeypatch):
    b = make_bridge()
    timings = run_starved(b, bridge_mod.GIL_YIELD_S, monkeypatch)
    assert timings is not None, "client never finished"
    assert timings["hello"] < 0.35, timings
    assert timings["ping"] < 0.25, timings
