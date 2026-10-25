"""Wire format shared by the socket server and the dispatcher.

One JSON object per line:
  request   {"id": ..., "cmd": "name", "args": {...}}
  response  {"id": ..., "ok": true, "result": {...}}
            {"id": ..., "ok": false, "error": {"type": "...", "message": "...", "where": "..."}}
"""

import json

PROTOCOL_VERSION = 1
SCRIPT_VERSION = "0.1.0"
HOST = "127.0.0.1"
DEFAULT_PORT = 9878
MAX_LINE_BYTES = 8 * 1024 * 1024


class BridgeError(Exception):
    """An expected failure with a message meant for the user (bad index, wrong track type...)."""

    def __init__(self, type_, message):
        Exception.__init__(self, message)
        self.type = type_
        self.message = message


def encode(obj):
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def ok(request_id, result):
    return {"id": request_id, "ok": True, "result": result}


def error(request_id, type_, message, where=None):
    err = {"type": type_, "message": message}
    if where:
        err["where"] = where
    return {"id": request_id, "ok": False, "error": err}
