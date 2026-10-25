"""A scriptable fake LiveBridge socket for testing the client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

Handler = Callable[[dict[str, Any], "FakeBridge"], Awaitable[dict[str, Any] | None] | dict[str, Any] | None]


class FakeBridge:
    """Speaks the LiveBridge protocol. `handler` decides each reply; returning None sends nothing."""

    def __init__(self, handler: Handler | None = None, info: dict[str, Any] | None = None):
        self.handler = handler or (
            lambda req, fb: {"id": req["id"], "ok": True, "result": {"cmd": req["cmd"], "args": req["args"]}}
        )
        self.info = info or {"protocol": 1, "script_version": "fake", "live_version": "12.4.7"}
        self.requests: list[dict[str, Any]] = []
        self.connections = 0
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self, port: int = 0) -> FakeBridge:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        self.drop_all()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def drop_all(self) -> None:
        for w in self._writers:
            w.close()
        self._writers.clear()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.append(writer)
        try:
            while line := await reader.readline():
                req = json.loads(line)
                if req["cmd"] == "hello":
                    await self._send(writer, {"id": req["id"], "ok": True, "result": self.info})
                    continue
                self.requests.append(req)
                reply = self.handler(req, self)
                if asyncio.iscoroutine(reply):
                    reply = await reply
                if reply is not None:
                    await self._send(writer, reply)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()
