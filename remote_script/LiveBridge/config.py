"""Port configuration.

Live is usually launched from the Dock, so it never sees shell environment
variables. Both LiveBridge and the MCP server read the same JSON file instead.
"""

import json
import os

from .protocol import DEFAULT_PORT

CONFIG_PATH = os.path.expanduser("~/.config/abletonconnect/config.json")


def load_port(path=CONFIG_PATH):
    """Return (port, warning). A broken config falls back to the default port."""
    if not os.path.exists(path):
        return DEFAULT_PORT, None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        port = data.get("port", DEFAULT_PORT)
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            return DEFAULT_PORT, "invalid port %r in %s, using %d" % (port, path, DEFAULT_PORT)
        return port, None
    except Exception as e:
        return DEFAULT_PORT, "could not read %s (%s), using %d" % (path, e, DEFAULT_PORT)
