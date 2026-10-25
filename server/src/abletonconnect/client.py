"""Async client for the LiveBridge Remote Script.

Connects lazily, performs the hello handshake, matches responses to requests by
id, and reconnects after Live restarts. Read-only commands are retried once on
a fresh connection if the old one dropped mid-request; mutating commands never
are, because there's no way to tell whether Live already applied them.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import HOST, resolve_port

PROTOCOL_VERSION = 1
CLIENT_NAME = "abletonconnect-mcp/0.1.0"
MAX_LINE_BYTES = 8 * 1024 * 1024
LOG_HINT = "~/Library/Preferences/Ableton/Live <version>/Log.txt"

log = logging.getLogger(__name__)


class LiveError(Exception):
    """Any failure talking to Live. The message is written for the user."""


class LiveUnavailable(LiveError):
    """Couldn't reach a working LiveBridge."""


class LiveTimeout(LiveError):
    pass


class ConnectionLost(LiveError):
    pass


class LiveCommandError(LiveError):
    """LiveBridge ran the command and reported an error."""

    def __init__(self, type_: str, message: str, where: str | None = None):
        super().__init__(message)
        self.type = type_
        self.where = where


def is_live_running() -> bool | None:
    """True/False from pgrep, None if we can't tell."""
    try:
        proc = subprocess.run(["pgrep", "-x", "Live"], capture_output=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0


def _encode(obj: Any) -> bytes:
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


@dataclass(eq=False)
class _Connection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    info: dict[str, Any]
    pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    closed: bool = False
    task: asyncio.Task[None] | None = None


class LiveClient:
    def __init__(
        self,
        host: str = HOST,
        port: int | None = None,
        *,
        connect_timeout: float = 2.0,
        handshake_timeout: float = 3.0,
        default_timeout: float = 10.0,
        live_running: Callable[[], bool | None] = is_live_running,
    ):
        self.host = host
        self.port = port if port is not None else resolve_port()
        self.connect_timeout = connect_timeout
        self.handshake_timeout = handshake_timeout
        self.default_timeout = default_timeout
        self._live_running = live_running
        self._conn: _Connection | None = None
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._ids = itertools.count(1)

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    @property
    def info(self) -> dict[str, Any] | None:
        return self._conn.info if self._conn else None

    async def request(
        self,
        cmd: str,
        args: dict[str, Any] | None = None,
        *,
        mutating: bool,
        timeout: float | None = None,
    ) -> Any:
        timeout = timeout or self.default_timeout
        attempts = 1 if mutating else 2
        for attempt in range(attempts):
            conn = await self._ensure_connected()
            try:
                return await self._send(conn, cmd, args or {}, timeout, mutating)
            except ConnectionLost:
                if attempt + 1 < attempts:
                    log.info("connection lost during %s, retrying on a new connection", cmd)
                    continue
                if mutating:
                    raise ConnectionLost(
                        f"The connection to Live dropped after `{cmd}` was sent, so it may or may not have been "
                        "applied. Check with get_session_info or get_track_info before retrying."
                    ) from None
                raise
        raise AssertionError("unreachable")

    async def close(self) -> None:
        if self._conn is not None:
            self._drop(self._conn, "client closed")

    # ---- internals --------------------------------------------------------------------------

    async def _send(self, conn: _Connection, cmd: str, args: dict[str, Any], timeout: float, mutating: bool) -> Any:
        request_id = str(next(self._ids))
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        conn.pending[request_id] = future
        try:
            async with self._write_lock:
                conn.writer.write(_encode({"id": request_id, "cmd": cmd, "args": args}))
                await conn.writer.drain()
        except (ConnectionError, OSError) as e:
            conn.pending.pop(request_id, None)
            self._drop(conn, f"write failed: {e}")
            raise ConnectionLost(f"Lost the connection to Live while sending `{cmd}`.") from e

        try:
            response = await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            conn.pending.pop(request_id, None)
            future.cancel()
            maybe = " It may still be applied once Live catches up." if mutating else ""
            raise LiveTimeout(
                f"Live didn't answer `{cmd}` within {timeout:g} s. Live's main thread may be busy "
                f"(loading a set, scanning plugins, or a dialog is open).{maybe}"
            ) from None

        if response.get("ok"):
            return response.get("result")
        err = response.get("error") or {}
        raise LiveCommandError(
            err.get("type", "error"), err.get("message", "LiveBridge returned an error."), err.get("where")
        )

    async def _ensure_connected(self) -> _Connection:
        conn = self._conn
        if conn is not None and not conn.closed:
            return conn
        async with self._connect_lock:
            conn = self._conn
            if conn is not None and not conn.closed:
                return conn
            self._conn = await self._connect()
            return self._conn

    async def _connect(self) -> _Connection:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, limit=MAX_LINE_BYTES), self.connect_timeout
            )
        except (OSError, TimeoutError) as e:
            raise self._unavailable(e) from None

        try:
            writer.write(
                _encode({"id": "hello", "cmd": "hello", "args": {"protocol": PROTOCOL_VERSION, "client": CLIENT_NAME}})
            )
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), self.handshake_timeout)
            reply = json.loads(line) if line else None
        except (OSError, TimeoutError, ValueError):
            reply = None
        if not isinstance(reply, dict) or not reply.get("ok") or not isinstance(reply.get("result"), dict):
            writer.close()
            raise LiveUnavailable(
                f"Something is listening on {self.host}:{self.port} but it isn't LiveBridge. "
                "Free the port or pick another one in ~/.config/abletonconnect/config.json "
                "(then restart Live)."
            )
        info = reply["result"]
        if info.get("protocol") != PROTOCOL_VERSION:
            writer.close()
            raise LiveUnavailable(
                f"LiveBridge in Live speaks protocol {info.get('protocol')}, this server speaks {PROTOCOL_VERSION}. "
                "Run scripts/install.sh to update the Remote Script, then restart Live."
            )
        conn = _Connection(reader, writer, info)
        conn.task = asyncio.create_task(self._read_loop(conn))
        log.info("connected to LiveBridge %s (Live %s)", info.get("script_version"), info.get("live_version"))
        return conn

    def _unavailable(self, exc: BaseException) -> LiveUnavailable:
        running = self._live_running()
        if running is False:
            return LiveUnavailable("Ableton Live isn't running. Start Live, then try again.")
        prefix = "Live is running, but" if running else "Couldn't reach Live:"
        return LiveUnavailable(
            f"{prefix} LiveBridge isn't answering on {self.host}:{self.port} ({exc.__class__.__name__}). "
            "In Live > Settings > Link, Tempo & MIDI, set a Control Surface slot to LiveBridge. "
            "If it's not in the list, run scripts/install.sh and restart Live; Live only loads Remote "
            f"Scripts at startup. Errors from the script are in {LOG_HINT}."
        )

    async def _read_loop(self, conn: _Connection) -> None:
        reason = "connection closed by Live"
        try:
            while True:
                line = await conn.reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("ignoring malformed line from LiveBridge: %r", line[:200])
                    continue
                future = conn.pending.pop(str(msg.get("id")), None)
                if future is not None and not future.done():
                    future.set_result(msg)
                else:
                    log.debug("dropping response for unknown or timed-out id %r", msg.get("id"))
        except (ConnectionError, OSError, ValueError, asyncio.LimitOverrunError) as e:
            reason = f"read failed: {e}"
        finally:
            self._drop(conn, reason)

    def _drop(self, conn: _Connection, reason: str) -> None:
        if conn.closed:
            return
        conn.closed = True
        log.info("LiveBridge connection dropped: %s", reason)
        for future in conn.pending.values():
            if not future.done():
                future.set_exception(ConnectionLost(f"Lost the connection to Live ({reason})."))
        conn.pending.clear()
        try:
            conn.writer.close()
        except Exception:
            pass
        if self._conn is conn:
            self._conn = None
