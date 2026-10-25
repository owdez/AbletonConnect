"""Socket protocol: framing, handshake, rejection of non-LiveBridge clients."""

import queue
import threading
import time

import pytest
from helpers import LineClient

import LiveBridge.server as server_mod
from LiveBridge.log import BridgeLog
from LiveBridge.protocol import ok
from LiveBridge.server import BridgeServer

INFO = {"protocol": 1, "script_version": "test", "live_version": "12.4.7"}


class EchoMain:
    """Stands in for Live's main thread: answers each request with its args."""

    def __init__(self):
        self.requests = queue.Queue()
        self.seen = []
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, request, reply):
        self.requests.put((request, reply))

    def _run(self):
        while not self._stop:
            try:
                request, reply = self.requests.get(timeout=0.05)
            except queue.Empty:
                continue
            self.seen.append(request)
            if request["cmd"] == "unencodable":
                reply(ok(request["id"], {"bad": object()}))
            else:
                reply(ok(request["id"], {"echo": request["args"]}))

    def stop(self):
        self._stop = True


@pytest.fixture
def bridge():
    main = EchoMain()
    srv = BridgeServer(0, main.submit, INFO, BridgeLog())
    srv.start()
    yield srv, main
    srv.stop()
    main.stop()


def hello(client):
    client.send({"id": "h", "cmd": "hello", "args": {"protocol": 1}})
    return client.recv()


def test_hello_returns_bridge_info(bridge):
    srv, _ = bridge
    c = LineClient(srv.port)
    reply = hello(c)
    assert reply == {"id": "h", "ok": True, "result": INFO}


def test_request_round_trip_keeps_ids(bridge):
    srv, _ = bridge
    c = LineClient(srv.port)
    hello(c)
    c.send({"id": 7, "cmd": "x", "args": {"a": 1}})
    assert c.recv() == {"id": 7, "ok": True, "result": {"echo": {"a": 1}}}


def test_command_before_hello_is_refused_and_closed(bridge):
    srv, main = bridge
    c = LineClient(srv.port)
    c.send({"id": 1, "cmd": "set_tempo", "args": {"bpm": 90}})
    reply = c.recv()
    assert reply["error"]["type"] == "handshake_required"
    assert c.is_closed()
    assert main.seen == []


def test_http_request_is_dropped_without_running_anything(bridge):
    """A web page POSTing to the port must not get a command through."""
    srv, main = bridge
    c = LineClient(srv.port)
    body = '{"id":1,"cmd":"set_tempo","args":{"bpm":60}}\n'
    c.send_raw(("POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: text/plain\r\n\r\n" + body).encode())
    assert c.is_closed()
    time.sleep(0.1)
    assert main.seen == []


def test_several_messages_in_one_packet_and_split_messages(bridge):
    srv, _ = bridge
    c = LineClient(srv.port)
    c.send_raw(b'{"id":"h","cmd":"hello","args":{"protocol":1}}\n{"id":1,"cmd":"a"}\n{"id":2,"cmd":"b"}\n')
    replies = [c.recv() for _ in range(3)]
    assert [r["id"] for r in replies] == ["h", 1, 2]
    c.send_raw(b'{"id":3,"cmd"')
    time.sleep(0.05)
    c.send_raw(b':"c","args":{"k":"v"}}\n')
    assert c.recv() == {"id": 3, "ok": True, "result": {"echo": {"k": "v"}}}


def test_bad_json_after_handshake_gets_error_and_connection_stays(bridge):
    srv, _ = bridge
    c = LineClient(srv.port)
    hello(c)
    c.send_raw(b"not json\n")
    assert c.recv()["error"]["type"] == "bad_request"
    c.send({"id": 9, "cmd": "still-here"})
    assert c.recv()["id"] == 9


def test_non_object_args_rejected(bridge):
    srv, _ = bridge
    c = LineClient(srv.port)
    hello(c)
    c.send({"id": 1, "cmd": "x", "args": [1, 2]})
    assert c.recv()["error"]["type"] == "bad_request"


def test_oversized_line_closes_connection(bridge, monkeypatch):
    monkeypatch.setattr(server_mod, "MAX_LINE_BYTES", 1024)
    srv, main = bridge
    c = LineClient(srv.port)
    hello(c)
    c.send_raw(b"x" * 5000)
    assert c.is_closed()


def test_unencodable_result_becomes_error_not_crash(bridge):
    srv, _ = bridge
    c = LineClient(srv.port)
    hello(c)
    c.send({"id": 1, "cmd": "unencodable"})
    reply = c.recv()
    assert reply["ok"] is False and reply["error"]["type"] == "internal"
    c.send({"id": 2, "cmd": "after"})
    assert c.recv()["id"] == 2


def test_max_connections(bridge):
    srv, _ = bridge
    srv.max_connections = 2
    clients = [LineClient(srv.port) for _ in range(2)]
    for c in clients:
        hello(c)
    extra = LineClient(srv.port)
    assert extra.recv()["error"]["type"] == "busy"
    assert extra.is_closed()


def test_stop_closes_clients_quickly(bridge):
    srv, _ = bridge
    c = LineClient(srv.port)
    hello(c)
    started = time.monotonic()
    srv.stop()
    assert c.is_closed()
    assert time.monotonic() - started < 2.0
    assert srv.connection_count == 0


def test_binds_loopback_only(bridge):
    srv, _ = bridge
    assert srv._sock.getsockname()[0] == "127.0.0.1"


class Recorder:
    """Captures the reply callback so a test can call send() itself, like the main thread does."""

    def __init__(self):
        self.replies = queue.Queue()

    def submit(self, request, reply):
        self.replies.put((request, reply))


@pytest.fixture
def recording_bridge():
    rec = Recorder()
    srv = BridgeServer(0, rec.submit, INFO, BridgeLog())
    srv.start()
    yield srv, rec
    srv.stop()


def test_big_and_small_replies_arrive_whole_and_in_order(recording_bridge):
    srv, rec = recording_bridge
    c = LineClient(srv.port, timeout=10)
    hello(c)
    c.send({"id": 1, "cmd": "x"})
    _, reply = rec.replies.get(timeout=2)
    big = "x" * (3 * 1024 * 1024)  # far bigger than the socket buffer: forces a partial send
    reply(ok(1, {"blob": big}))
    for i in range(2, 6):
        reply(ok(i, {"n": i}))
    first = c.recv()
    assert first["id"] == 1 and len(first["result"]["blob"]) == len(big)
    assert [c.recv()["id"] for _ in range(4)] == [2, 3, 4, 5]


def test_send_never_blocks_when_client_stops_reading(recording_bridge):
    srv, rec = recording_bridge
    c = LineClient(srv.port)
    hello(c)
    c.send({"id": 1, "cmd": "x"})
    _, reply = rec.replies.get(timeout=2)
    started = time.monotonic()
    for i in range(20):
        reply(ok(i, {"blob": "y" * (1024 * 1024)}))  # 20 MiB the client never reads
    assert time.monotonic() - started < 1.0
