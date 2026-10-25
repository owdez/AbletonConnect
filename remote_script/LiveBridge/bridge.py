"""The ControlSurface that Live instantiates. Owns the socket server and the pump.

Threading: socket threads put (request, reply) into a queue.Queue. The pump
runs on Live's main thread, reschedules itself every tick with
schedule_message(1, ...), and is the only code that touches the Live Object
Model. schedule_message itself is never called from a socket thread: it
mutates ControlSurface state the main thread is iterating.

Live's main thread holds the GIL almost all the time, and Python only hands it
to a waiting thread after 5 ms of bytecode, which a pump tick never reaches.
Left alone, socket threads ran a few times a second (a `hello` took 0.2-1 s).
Each tick therefore starts with a 1 ms sleep that releases the GIL so the
socket threads can accept, read and queue requests, and the same tick answers
them.
"""

import queue
import time

import Live
from _Framework.ControlSurface import ControlSurface

from . import handlers  # noqa: F401  (registers commands)
from .config import load_port
from .dispatch import Context, Dispatcher
from .log import BridgeLog
from .protocol import HOST, PROTOCOL_VERSION, SCRIPT_VERSION
from .server import BridgeServer

TICK_BUDGET_S = 0.04
GIL_YIELD_S = 0.001
BIND_RETRY_TICKS = 50


class LiveBridge(ControlSurface):
    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self._closing = False
        self._server = None
        self._requests = queue.Queue()
        self._bind_wait = 0
        self._bind_failures = 0
        self._tick_failures = 0
        self._dispatcher = None
        self._log = BridgeLog(sink=self.log_message)
        try:
            self._port, warning = load_port()
            if warning:
                self._log.error(warning)
            self._dispatcher = Dispatcher(Context(self.song, Live.Application.get_application), self._log)
            self._info = {
                "protocol": PROTOCOL_VERSION,
                "script_version": SCRIPT_VERSION,
                "live_version": _live_version(),
            }
            self._start_server()
        except Exception:
            self._log.exception("startup failed")
        self._log.flush()
        self.schedule_message(1, self._pump)

    def disconnect(self):
        self._closing = True
        try:
            if self._server is not None:
                self._server.stop()
                self._log.info("stopped")
            if self._dispatcher is not None:
                self._dispatcher.cancel_jobs("Live is closing the LiveBridge script.")
        except Exception:
            self._log.exception("shutdown failed")
        self._log.flush()
        ControlSurface.disconnect(self)

    def _submit(self, request, reply):
        """Called from socket threads."""
        self._requests.put((request, reply))

    def _start_server(self):
        server = BridgeServer(self._port, self._submit, self._info, self._log, host=HOST)
        try:
            server.start()
        except OSError as e:
            self._bind_failures += 1
            self._bind_wait = BIND_RETRY_TICKS
            if self._bind_failures == 1:
                self._log.error(
                    "cannot listen on %s:%d (%s). Is another LiveBridge running, maybe selected in two "
                    "Control Surface slots? Retrying every %d s." % (HOST, self._port, e, BIND_RETRY_TICKS // 10)
                )
                self.show_message("LiveBridge: port %d is busy" % self._port)
            return
        self._server = server
        self._log.info(
            "listening on %s:%d (script %s, protocol %d, Live %s)"
            % (HOST, server.port, SCRIPT_VERSION, PROTOCOL_VERSION, self._info["live_version"])
        )
        self.show_message("LiveBridge listening on %s:%d" % (HOST, server.port))

    def _pump(self):
        if self._closing:
            return
        try:
            self._tick()
            self._tick_failures = 0
        except Exception:
            # Log the first failure and then every ~10 s, not every tick.
            if self._tick_failures % 100 == 0:
                self._log.exception("pump tick failed (%d in a row)" % (self._tick_failures + 1))
            self._tick_failures += 1
        finally:
            self._log.flush()
            if not self._closing:
                self.schedule_message(1, self._pump)

    def _tick(self):
        if self._dispatcher is None:
            return
        if self._server is None:
            self._bind_wait -= 1
            if self._bind_wait <= 0:
                self._start_server()
        time.sleep(GIL_YIELD_S)  # let socket threads run; see module docstring
        deadline = time.monotonic() + TICK_BUDGET_S
        while time.monotonic() < deadline:
            try:
                request, reply = self._requests.get_nowait()
            except queue.Empty:
                break
            self._dispatcher.handle(request, reply)
        self._dispatcher.run_jobs(deadline)


def _live_version():
    try:
        app = Live.Application.get_application()
        return "%d.%d.%d" % (app.get_major_version(), app.get_minor_version(), app.get_bugfix_version())
    except Exception:
        return "unknown"
