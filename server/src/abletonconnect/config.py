"""Where to find LiveBridge and the user's files. Shares ~/.config/abletonconnect/config.json with
the Remote Script."""

import json
import os
import sys
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 9878
CONFIG_DIR = Path("~/.config/abletonconnect").expanduser()
CONFIG_PATH = CONFIG_DIR / "config.json"
WINDOWS = sys.platform == "win32"


def install_script() -> str:
    return r"scripts\install.ps1" if WINDOWS else "scripts/install.sh"


def live_prefs_dirs() -> list[Path]:
    """Live's preferences folders (Library.cfg, Log.txt), newest first. macOS:
    ~/Library/Preferences/Ableton/Live 12.4.7. Windows: %APPDATA%\\Ableton\\Live 12.4.7\\Preferences."""
    if WINDOWS:
        root = Path(os.environ.get("APPDATA") or "~/AppData/Roaming").expanduser() / "Ableton"
        folders = [p / "Preferences" for p in root.glob("Live *")]
    else:
        folders = list(Path("~/Library/Preferences/Ableton").expanduser().glob("Live *"))
    return sorted((p for p in folders if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)


def default_user_library() -> Path:
    """Where Live puts the User Library when Library.cfg doesn't say otherwise."""
    return Path("~/Documents/Ableton/User Library" if WINDOWS else "~/Music/Ableton/User Library").expanduser()


def log_hint() -> str:
    if WINDOWS:
        return r"%APPDATA%\Ableton\Live <version>\Preferences\Log.txt"
    return "~/Library/Preferences/Ableton/Live <version>/Log.txt"


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
