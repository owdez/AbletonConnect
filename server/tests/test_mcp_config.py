"""scripts/mcp_config.py, which install.sh and install.ps1 run to read and edit Claude's MCP registrations."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mcp_config.py"
spec = importlib.util.spec_from_file_location("mcp_config", SCRIPT)
mcp_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp_config)


def test_register_keeps_other_servers_and_settings(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    mcp_config.register(str(path), "/bin/uv", "/repo/server")
    data = json.loads(path.read_text())
    assert data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["ableton"] == {
        "command": "/bin/uv",
        "args": ["--directory", "/repo/server", "run", "abletonconnect-mcp"],
    }


def test_register_creates_a_missing_file_and_refuses_broken_json(tmp_path):
    path = tmp_path / "new.json"
    mcp_config.register(str(path), "uv", "server")
    assert list(json.loads(path.read_text())["mcpServers"]) == ["ableton"]
    path.write_text("{broken")
    with pytest.raises(ValueError):
        mcp_config.register(str(path), "uv", "server")
    assert path.read_text() == "{broken"


def test_show_reports_each_registration(tmp_path, capsys):
    claude = tmp_path / ".claude.json"
    claude.write_text(
        json.dumps({"mcpServers": {"ableton": {"command": "uv", "args": ["--directory", "/old", "run"]}}})
    )
    desktop = tmp_path / "desktop.json"
    desktop.write_text(
        json.dumps({"mcpServers": {"ableton": {"command": "uv", "args": ["--directory", "/repo/server/"]}}})
    )
    mcp_config.show(str(claude), str(desktop), "/repo/server")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "MCP:      Claude Code: '/old'"
    assert "isn't this repo's server folder" in out[1]
    assert out[2] == "          Desktop app: '/repo/server/'"  # a trailing slash is the same folder
    assert len(out) == 3


def test_show_says_how_to_register(tmp_path, capsys):
    mcp_config.show(str(tmp_path / "missing.json"), str(tmp_path / "missing2.json"), "/repo/server")
    out = capsys.readouterr().out
    assert "claude mcp add --scope user ableton" in out and mcp_config.DESKTOP_HINT in out
