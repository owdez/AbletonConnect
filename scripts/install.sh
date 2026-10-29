#!/usr/bin/env bash
# Install the LiveBridge Remote Script into Live's User Library.
#
#   scripts/install.sh            symlink (edits in the repo apply after a Live restart)
#   scripts/install.sh --copy     copy instead, if Live doesn't load the symlink
#   scripts/install.sh --check    did Live load it? Is the copy current? Where do Claude's registrations point?
#   scripts/install.sh --desktop  register the MCP server with the Claude desktop app
#   scripts/install.sh --uninstall
#
# Live only loads Remote Scripts at startup: restart Live after installing.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/remote_script/LiveBridge"
NAME="LiveBridge"
PORT=9878
CONFIG="$HOME/.config/abletonconnect/config.json"
DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
if [[ -f "$CONFIG" ]]; then
  PORT="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port", 9878))' "$CONFIG" 2>/dev/null || echo 9878)"
fi

# Newest "Live x.y.z" preferences folder.
prefs_dir() {
  ls -dt "$HOME/Library/Preferences/Ableton/Live "* 2>/dev/null | head -1
}

# The User Library location from Library.cfg, falling back to Live's default.
user_library() {
  local cfg path name
  cfg="$(prefs_dir)/Library.cfg"
  if [[ -f "$cfg" ]]; then
    path="$(sed -n '/<UserLibrary>/,/<\/UserLibrary>/s/.*<ProjectPath Value="\([^"]*\)".*/\1/p' "$cfg" | head -1)"
    name="$(sed -n '/<UserLibrary>/,/<\/UserLibrary>/s/.*<ProjectName Value="\([^"]*\)".*/\1/p' "$cfg" | head -1)"
    if [[ -n "$path" && -n "$name" && -d "$path/$name" ]]; then
      echo "$path/$name"
      return
    fi
  fi
  echo "$HOME/Music/Ableton/User Library"
}

DEST_DIR="$(user_library)/Remote Scripts"
DEST="$DEST_DIR/$NAME"

remove_existing() {
  if [[ -L "$DEST" ]]; then
    rm "$DEST"
  elif [[ -d "$DEST" ]]; then
    # Only delete a folder that is clearly a previous LiveBridge copy.
    if [[ -f "$DEST/protocol.py" && -f "$DEST/bridge.py" ]] && grep -q "LiveBridge" "$DEST/bridge.py"; then
      rm -rf "$DEST"
    else
      echo "Refusing to replace $DEST: it doesn't look like a LiveBridge install." >&2
      exit 1
    fi
  elif [[ -e "$DEST" ]]; then
    echo "Refusing to replace $DEST: unexpected file." >&2
    exit 1
  fi
}

# uv's absolute path: GUI apps like the Claude desktop app don't get your shell's PATH.
find_uv() {
  local c
  for c in "$(command -v uv 2>/dev/null || true)" /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [[ -n "$c" && -x "$c" ]]; then
      echo "$c"
      return
    fi
  done
  echo "uv not found. Install it from https://docs.astral.sh/uv/ and run this again." >&2
  exit 1
}

# The Claude desktop app keeps its config in memory and writes the whole file back when it saves
# its settings, dropping anything added while it runs (seen on 2026-10-26). So edit it only while
# the app is closed. "is running" checks without launching or scripting the app.
claude_app_running() {
  [[ "$(osascript -e 'application id "com.anthropic.claudefordesktop" is running' 2>/dev/null)" == "true" ]]
}

# Add or replace the "ableton" entry in the Claude desktop app's config, keeping everything else.
desktop() {
  local uv backup
  if claude_app_running; then
    echo "The Claude desktop app is running. It rewrites $DESKTOP_CONFIG" >&2
    echo "from memory when it saves its settings, which drops an entry added while it's open." >&2
    echo "Quit the Claude app (Cmd+Q), run scripts/install.sh --desktop again from Terminal, then reopen it." >&2
    exit 1
  fi
  uv="$(find_uv)"
  mkdir -p "$(dirname "$DESKTOP_CONFIG")"
  if [[ -f "$DESKTOP_CONFIG" ]]; then
    backup="$DESKTOP_CONFIG.bak-$(date +%Y%m%d-%H%M%S)"
    cp -p "$DESKTOP_CONFIG" "$backup"
    echo "Backed up $DESKTOP_CONFIG"
    echo "       to $backup"
  fi
  /usr/bin/python3 "$REPO/scripts/mcp_config.py" register "$DESKTOP_CONFIG" "$uv" "$REPO/server"
  echo "Open the Claude desktop app to load it."
}

# Where Claude Code (user scope) and the desktop app run the MCP server from.
registrations() {
  /usr/bin/python3 "$REPO/scripts/mcp_config.py" show "$HOME/.claude.json" "$DESKTOP_CONFIG" "$REPO/server" || true
}

check() {
  local prefs log
  prefs="$(prefs_dir)"
  log="$prefs/Log.txt"
  echo "Repo:     '$REPO'"
  if [[ "$REPO" =~ [[:space:]]$ || "$REPO" =~ [[:space:]]/ ]]; then
    echo "          WARNING: a folder in this path ends in a space. The scripts quote it and work, but"
    echo "          anything that splits or trims paths (unquoted shell, some apps) will break."
    echo "          README > 'Moving the repo' has the steps to rename it."
  fi
  echo "Install:  $DEST"
  if [[ -L "$DEST" ]]; then
    echo "          symlink -> $(readlink "$DEST")"
    if [[ "$(readlink "$DEST")" != "$SRC" ]]; then
      echo "          WARNING: the link doesn't point at this repo. Run scripts/install.sh again."
    fi
  elif [[ -d "$DEST" ]]; then
    local differ
    differ="$(diff -rq --exclude=__pycache__ --exclude='*.pyc' "$SRC" "$DEST" 2>/dev/null | wc -l | tr -d ' ' || true)"
    if [[ "$differ" == 0 ]]; then
      echo "          copy, same as the repo"
    else
      echo "          copy, OUT OF DATE: $differ file(s) differ from the repo."
      echo "          Run scripts/install.sh --copy and restart Live."
    fi
  else
    echo "          NOT INSTALLED (run scripts/install.sh)"
  fi
  registrations
  echo "Log:      $log"
  if pgrep -x Live >/dev/null; then
    echo "Live:     running"
  else
    echo "Live:     not running"
  fi
  if [[ -f "$log" ]]; then
    # Only look at the log since Live last started.
    local since
    since="$(grep -n "info: Started: Live" "$log" 2>/dev/null | tail -1 | cut -d: -f1 || true)"
    [[ -z "$since" ]] && since=1
    local recent
    recent="$(tail -n +"$since" "$log" | grep -E "LiveBridge" || true)"
    if [[ -n "$recent" ]]; then
      echo "Log lines since Live started:"
      echo "$recent" | tail -15 | sed 's/^/          /'
    else
      echo "          No LiveBridge lines since Live started."
      echo "          Is LiveBridge selected under Settings > Link, Tempo & MIDI > Control Surface?"
      if [[ -L "$DEST" ]]; then
        echo "          If it's missing from that list, Live may not follow the symlink: run scripts/install.sh --copy and restart Live."
      fi
    fi
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port:     127.0.0.1:$PORT is listening ($(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | awk 'NR==2{print $1}'))"
  else
    echo "Port:     nothing listening on $PORT"
  fi
}

case "${1:-}" in
  --check)
    check
    exit 0
    ;;
  --desktop)
    desktop
    exit 0
    ;;
  --uninstall)
    remove_existing
    echo "Removed $DEST. Restart Live."
    exit 0
    ;;
  --copy)
    MODE="copy"
    ;;
  ""|--link)
    MODE="link"
    ;;
  *)
    sed -n '2,10p' "$0"
    exit 2
    ;;
esac

mkdir -p "$DEST_DIR"
remove_existing
if [[ "$MODE" == link ]]; then
  ln -s "$SRC" "$DEST"
  echo "Linked  $DEST -> $SRC"
else
  mkdir "$DEST"
  rsync -a --exclude "__pycache__" --exclude "*.pyc" "$SRC/" "$DEST/"
  echo "Copied  $SRC -> $DEST"
  echo "        Re-run with --copy after every change to remote_script/."
fi

if pgrep -x Live >/dev/null; then
  echo
  echo "Live is running. Restart it: Live only loads Remote Scripts at startup."
fi
cat <<EOF

Then in Live > Settings > Link, Tempo & MIDI, set an empty Control Surface slot to
"$NAME" with Input: None and Output: None. Check it loaded with: scripts/install.sh --check
EOF
