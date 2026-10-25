"""TCP server for LiveBridge. Never touches the Live Object Model.

Threads:
  accept thread      accepts connections on 127.0.0.1
  reader per conn    splits lines, answers `hello`, hands requests to submit()
  writer per conn    sends whatever send() couldn't send right away

Live's embedded Python lets background threads run only when the main thread
releases the GIL, roughly once per tick. A reply left to the writer thread
waits an extra ~100 ms, so send() writes directly when nothing is pending, and
the writer only handles leftovers (big replies, a slow client).

Connection sockets are non-blocking, so that direct send can never stall
Live's main thread. (MSG_DONTWAIT on a blocking socket does block on macOS.)
The threads wait with selectors, which use kqueue on macOS: select() fails for
file descriptors above 1024, and Live raises its own limit to 8192.

The first line on a connection must be a JSON `hello`. Anything else (for
example a browser's "POST / HTTP/1.1") closes the connection, which stops web
pages from smuggling commands in a request body.
"""

import json
import selectors
import socket
import threading

from .protocol import HOST, MAX_LINE_BYTES, encode, error, ok

WAIT_S = 0.5


class Connection:
    def __init__(self, sock, addr, server):
        self.sock = sock
        self.addr = addr
        self._server = server
        self._cond = threading.Condition()
        self._pending = bytearray()  # bytes the writer thread still has to send
        self._inflight = False  # writer is sending outside the lock
        self._closed = False
        self.handshaken = False

    @property
    def closed(self):
        return self._closed

    def start(self):
        for target, name in ((self._read_loop, "reader"), (self._write_loop, "writer")):
            t = threading.Thread(target=target, name="LiveBridge-%s-%s" % (name, self.addr[1]))
            t.daemon = True
            t.start()

    def send(self, message):
        """Send a response. Safe from any thread, never blocks on I/O, a no-op once closed."""
        if self._closed:
            return
        try:
            data = encode(message)
        except (TypeError, ValueError) as e:
            data = encode(error(message.get("id"), "internal", "Response could not be encoded: %s" % e))
        with self._cond:
            if self._closed:
                return
            if not self._pending and not self._inflight:
                try:
                    sent = self.sock.send(data)
                except (BlockingIOError, InterruptedError):
                    sent = 0
                except OSError:
                    return  # the reader thread notices the dead socket and closes
                data = data[sent:]
            if data:
                self._pending += data
                self._cond.notify()

    def close_after_pending(self, timeout=1.0):
        """Let the writer flush what's pending (e.g. a final error), then close."""
        with self._cond:
            self._cond.wait_for(lambda: self._closed or (not self._pending and not self._inflight), timeout)
        self.close()

    def close(self):
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._server._forget(self)

    def _read_loop(self):
        buf = bytearray()
        sel = selectors.DefaultSelector()
        try:
            sel.register(self.sock, selectors.EVENT_READ)
            while not self._closed:
                if not sel.select(WAIT_S):
                    continue
                try:
                    chunk = self.sock.recv(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    break
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        if len(buf) > MAX_LINE_BYTES:
                            self._server.log.error("closing %s: line longer than %d bytes" % (self._peer(), MAX_LINE_BYTES))
                            return
                        break
                    line = bytes(buf[:nl])
                    del buf[: nl + 1]
                    if not self._handle_line(line):
                        return
        except (OSError, ValueError):
            pass  # socket closed under us
        except Exception:
            self._server.log.exception("reader for %s crashed" % self._peer())
        finally:
            sel.close()
            self.close_after_pending()

    def _handle_line(self, line):
        """Return False to close the connection."""
        line = line.strip()
        if not line:
            return True
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            msg = None
        if not isinstance(msg, dict) or not isinstance(msg.get("cmd"), str):
            if not self.handshaken:
                self._server.log.info("rejected %s: first line was not a LiveBridge hello" % self._peer())
                return False
            self.send(error(None, "bad_request", "Expected a JSON object with a string 'cmd'."))
            return True

        request_id = msg.get("id")
        cmd = msg["cmd"]
        args = msg.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            self.send(error(request_id, "bad_request", "'args' must be a JSON object."))
            return self.handshaken

        if cmd == "hello":
            info = dict(self._server.info)
            client_protocol = args.get("protocol")
            if client_protocol != info.get("protocol"):
                self._server.log.info("client %s speaks protocol %r, bridge speaks %r" % (self._peer(), client_protocol, info.get("protocol")))
            self.handshaken = True
            self.send(ok(request_id, info))
            return True
        if not self.handshaken:
            self.send(error(request_id, "handshake_required", "Send 'hello' first."))
            return False

        self._server.submit({"id": request_id, "cmd": cmd, "args": args}, self.send)
        return True

    def _write_loop(self):
        sel = selectors.DefaultSelector()
        try:
            sel.register(self.sock, selectors.EVENT_WRITE)
            while True:
                with self._cond:
                    self._cond.wait_for(lambda: self._closed or self._pending)
                    if self._closed:
                        return
                    chunk = memoryview(bytes(self._pending))
                    del self._pending[:]
                    self._inflight = True
                try:
                    while chunk and not self._closed:
                        if not sel.select(WAIT_S):
                            continue
                        try:
                            chunk = chunk[self.sock.send(chunk) :]
                        except (BlockingIOError, InterruptedError):
                            continue
                finally:
                    with self._cond:
                        self._inflight = False
                        self._cond.notify_all()
        except (OSError, ValueError):
            pass  # socket closed under us
        except Exception:
            self._server.log.exception("writer for %s crashed" % self._peer())
        finally:
            sel.close()
            self.close()

    def _peer(self):
        return "%s:%s" % (self.addr[0], self.addr[1])


class BridgeServer:
    def __init__(self, port, submit, info, log, host=HOST, max_connections=8):
        self.host = host
        self.port = port
        self.submit = submit
        self.info = info
        self.log = log
        self.max_connections = max_connections
        self._sock = None
        self._thread = None
        self._stopping = threading.Event()
        self._conns = set()
        self._conns_lock = threading.Lock()

    def start(self):
        """Bind and start accepting. Raises OSError if the port is taken."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen(8)
            sock.settimeout(0.5)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, name="LiveBridge-accept")
        self._thread.daemon = True
        self._thread.start()

    def stop(self, timeout=1.0):
        self._stopping.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        with self._conns_lock:
            conns = list(self._conns)
        for conn in conns:
            conn.close()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def connection_count(self):
        with self._conns_lock:
            return len(self._conns)

    def _forget(self, conn):
        with self._conns_lock:
            self._conns.discard(conn)

    def _accept_loop(self):
        while not self._stopping.is_set():
            try:
                sock, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                self.log.exception("accept failed")
                self._stopping.wait(0.5)
                continue
            try:
                self._admit(sock, addr)
            except Exception:
                self.log.exception("could not set up connection from %s" % (addr,))
                try:
                    sock.close()
                except OSError:
                    pass

    def _admit(self, sock, addr):
        sock.settimeout(1.0)  # only for the "busy" refusal below
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self._conns_lock:
            full = len(self._conns) >= self.max_connections
        if full:
            self.log.info("refusing %s:%s, %d connections open" % (addr[0], addr[1], self.max_connections))
            try:
                sock.sendall(encode(error(None, "busy", "Too many LiveBridge connections.")))
            except OSError:
                pass
            sock.close()
            return
        sock.setblocking(False)
        conn = Connection(sock, addr, self)
        with self._conns_lock:
            self._conns.add(conn)
        conn.start()
