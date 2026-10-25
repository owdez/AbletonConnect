"""Thread-safe logging into Live's Log.txt.

ControlSurface.log_message belongs to Live's main thread. Socket threads append
to a deque instead and the main-thread pump flushes it on every tick.
"""

import collections
import traceback

PREFIX = "LiveBridge: "


class BridgeLog:
    def __init__(self, sink=None, maxlen=500):
        self._sink = sink
        self._pending = collections.deque(maxlen=maxlen)

    def set_sink(self, sink):
        self._sink = sink

    def info(self, message):
        self._pending.append(PREFIX + message)

    def error(self, message):
        self._pending.append(PREFIX + "ERROR " + message)

    def exception(self, message):
        self._pending.append(PREFIX + "ERROR " + message + "\n" + traceback.format_exc())

    def flush(self):
        """Main thread only."""
        sink = self._sink
        if sink is None:
            return
        while True:
            try:
                line = self._pending.popleft()
            except IndexError:
                return
            try:
                sink(line)
            except Exception:
                return
