import json
import socket
import time

from LiveBridge import handlers  # noqa: F401  (registers commands)
from LiveBridge.dispatch import Context, Dispatcher
from LiveBridge.log import BridgeLog


def make_dispatcher(song, app, lines=None):
    sink = lines.append if lines is not None else (lambda line: None)
    return Dispatcher(Context(lambda: song, lambda: app), BridgeLog(sink=sink))


def call(dispatcher, cmd, max_ticks=2000, **args):
    """Run a command the way the pump does, including multi-tick jobs."""
    replies = []
    dispatcher.handle({"id": 1, "cmd": cmd, "args": args}, replies.append)
    ticks = 0
    while not replies and ticks < max_ticks:
        dispatcher.run_jobs(time.monotonic() + 0.04)
        ticks += 1
    assert replies, "%s never replied" % cmd
    reply = replies[0]
    reply["_ticks"] = ticks
    return reply


def result(dispatcher, cmd, **args):
    reply = call(dispatcher, cmd, **args)
    assert reply["ok"], reply
    return reply["result"]


def failure(dispatcher, cmd, **args):
    reply = call(dispatcher, cmd, **args)
    assert not reply["ok"], reply
    return reply["error"]


class LineClient:
    def __init__(self, port, timeout=3.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.buf = b""

    def send_raw(self, data):
        self.sock.sendall(data)

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def recv(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("connection closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def is_closed(self, wait=2.0):
        self.sock.settimeout(wait)
        try:
            return self.sock.recv(1) == b""
        except (ConnectionResetError, BrokenPipeError):
            return True
        except socket.timeout:
            return False

    def close(self):
        self.sock.close()
