"""Where Claude Code and the Claude desktop app run the "ableton" MCP server from, and adding it to
the desktop app. install.sh and install.ps1 run this: stdlib only, Python 3.9 (macOS's python3).

    mcp_config.py register <desktop config> <uv> <server folder>
    mcp_config.py show <~/.claude.json> <desktop config> <server folder>
"""

import json
import os
import sys

WINDOWS = sys.platform == "win32"
DESKTOP_HINT = r"scripts\install.ps1 -Desktop" if WINDOWS else "scripts/install.sh --desktop"


def register(path, uv, server):
    """Add or replace the "ableton" entry, keeping everything else."""
    data, mode = {}, 0o600
    if os.path.exists(path):
        mode = os.stat(path).st_mode & 0o777
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if text.strip():
            data = json.loads(text)  # refuse to touch a file that isn't valid JSON
    servers = data.setdefault("mcpServers", {})
    servers["ableton"] = {"command": uv, "args": ["--directory", server, "run", "abletonconnect-mcp"]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    with open(path, encoding="utf-8") as f:
        json.load(f)
    print(f"Registered 'ableton' in {path} ({len(servers)} MCP server{'' if len(servers) == 1 else 's'}).")


def where(entry):
    args = entry.get("args") or []
    return (
        args[args.index("--directory") + 1]
        if "--directory" in args[:-1]
        else " ".join([entry.get("command", "")] + args)
    )


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def show(claude_json, desktop_json, expected):
    for n, (label, path) in enumerate((("Claude Code", claude_json), ("Desktop app", desktop_json))):
        try:
            with open(path, encoding="utf-8") as f:
                entry = (json.load(f).get("mcpServers") or {}).get("ableton")
        except (OSError, ValueError):
            entry = None
        head = ("MCP:      " if n == 0 else " " * 10) + label + ":"
        if entry is None:
            hint = "claude mcp add --scope user ableton ... (see README)" if n == 0 else DESKTOP_HINT
            print(f"{head} not registered ({hint})")
            continue
        directory = where(entry)
        print(f"{head} {directory!r}")
        if not same_path(directory, expected):
            print(f"          WARNING: that isn't this repo's server folder ({expected!r}). Register it again.")


def main(argv):
    if len(argv) == 4 and argv[0] == "register":
        register(*argv[1:])
    elif len(argv) == 4 and argv[0] == "show":
        show(*argv[1:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
