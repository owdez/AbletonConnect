"""Where to find LiveBridge and the user's files. Shares ~/.config/abletonconnect/config.json with
the Remote Script."""

import json
import os
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 9878
CONFIG_DIR = Path("~/.config/abletonconnect").expanduser()
CONFIG_PATH = CONFIG_DIR / "config.json"


def resolve_port() -> int:
    env = os.environ.get("ABLETONCONNECT_PORT")
    if env:
        return int(env)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        port = data.get("port", DEFAULT_PORT)
        if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535:
            return port
    except (OSError, ValueError, AttributeError):
        pass
    return DEFAULT_PORT


def config_file(name: str, env: str) -> Path:
    """A machine-specific file in ~/.config/abletonconnect; the environment variable overrides it
    (the tests use that)."""
    override = os.environ.get(env)
    return Path(override).expanduser() if override else CONFIG_DIR / name
