#!/usr/bin/env bash
# Follow Live's Log.txt, showing LiveBridge lines and Remote Script errors.
#   scripts/tail_log.sh        LiveBridge + Python errors
#   scripts/tail_log.sh --all  everything
set -euo pipefail

PREFS="$(ls -dt "$HOME/Library/Preferences/Ableton/Live "* 2>/dev/null | head -1)"
LOG="$PREFS/Log.txt"
if [[ ! -f "$LOG" ]]; then
  echo "No Log.txt under $PREFS" >&2
  exit 1
fi
echo "Following $LOG" >&2
if [[ "${1:-}" == "--all" ]]; then
  exec tail -n 100 -F "$LOG"
fi
# Tracebacks from a failing import show up as RemoteScriptError lines.
tail -n 400 -F "$LOG" | grep --line-buffered -E "LiveBridge|RemoteScriptError|Traceback|Error:"
