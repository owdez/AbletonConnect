"""Settings in ~/.config/abletonconnect/config.json, the file LiveBridge and the MCP server also read.

    {
      "sample_folders": [
        "/Volumes/Samples Drive/Samples",
        {"path": "~/Music/Breaks", "browser": "user_folders/Music/Breaks"}
      ],
      "sample_index": "~/Library/Application Support/AbletonConnect/samples.db"
    }

"browser" is where the folder shows up in Live's browser. The default, user_folders/<folder name>,
is right for a folder added under Places in Live's browser. Set it for a folder that sits inside one.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path("~/.config/abletonconnect/config.json").expanduser()
INDEX_ENV = "ABLETONCONNECT_SAMPLE_INDEX"


def default_index(windows: bool) -> Path:
    """~/Library/Application Support/AbletonConnect/samples.db, or %LOCALAPPDATA%\\AbletonConnect\\samples.db
    on Windows."""
    if windows:
        return Path(os.environ.get("LOCALAPPDATA") or "~/AppData/Local").expanduser() / "AbletonConnect" / "samples.db"
    return Path("~/Library/Application Support/AbletonConnect/samples.db").expanduser()


DEFAULT_INDEX = default_index(sys.platform == "win32")


class ConfigError(Exception):
    """The config can't be used. The message says what to fix."""


@dataclass(frozen=True)
class SampleFolder:
    path: str  # absolute; not resolved, so a folder on an unmounted drive keeps its path
    browser: str  # the folder's path in Live's browser, e.g. "user_folders/Samples"


def escape(name: str) -> str:
    """Same escaping as LiveBridge's browser paths (remote_script/LiveBridge/handlers/browser.py)."""
    return name.replace("\\", "\\\\").replace("/", "\\/")


def browser_path(prefix: str, rel: str) -> str:
    return "/".join([prefix, *(escape(part) for part in rel.split("/"))])


def load(path: Path = CONFIG_PATH) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigError(f"Can't read {path}: {e}") from e
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ConfigError(f"{path} isn't valid JSON ({e}). Fix it and try again.") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object.")
    return data


def normalize(path: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def make_folder(path: str, browser: str | None = None) -> SampleFolder:
    path = normalize(path)
    return SampleFolder(path=path, browser=browser or "user_folders/" + escape(os.path.basename(path)))


def sample_folders(config: dict, where: Path = CONFIG_PATH) -> list[SampleFolder]:
    entries = config.get("sample_folders", [])
    if not isinstance(entries, list):
        raise ConfigError(f'"sample_folders" in {where} must be a list of folder paths.')
    folders: list[SampleFolder] = []
    for entry in entries:
        if isinstance(entry, str) and entry.strip():
            folder = make_folder(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry["path"].strip():
            browser = entry.get("browser")
            if browser is not None and (not isinstance(browser, str) or "/" not in browser.strip("/")):
                raise ConfigError(
                    f'"browser" for {entry["path"]} in {where} must be a Live browser path like "user_folders/Samples".'
                )
            folder = make_folder(entry["path"], browser.strip("/") if browser else None)
        else:
            raise ConfigError(f'Each entry in "sample_folders" in {where} must be a path or {{"path": ...}}.')
        if any(f.path == folder.path for f in folders):
            continue
        for other in folders:
            inner, outer = sorted((folder.path, other.path), key=len, reverse=True)
            if inner.startswith(outer + os.sep):
                raise ConfigError(f"{inner} is inside {outer} in {where}. List only one of them.")
        folders.append(folder)
    return folders


def index_path(config: dict) -> Path:
    env = os.environ.get(INDEX_ENV)
    if env:
        return Path(env).expanduser()
    value = config.get("sample_index")
    if value is None:
        return DEFAULT_INDEX
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f'"sample_index" in {CONFIG_PATH} must be a file path.')
    return Path(value).expanduser()
