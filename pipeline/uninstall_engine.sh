#!/usr/bin/env bash
# uninstall_engine.sh - full teardown: stop sending, stop the engine loop, unload the keep-awake
# agent, let the Mac sleep again. Leaves your data (send_log, suppression, worklist) intact.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
LA="$HOME/Library/LaunchAgents"; SEND="outreach/send"

touch "$SEND/STOP" "$SEND/LOOP_STOP"          # stop sending + tell the loop to exit at next tick
if [ -f "$SEND/.loop.pid" ]; then
  kill "$(cat "$SEND/.loop.pid" 2>/dev/null)" 2>/dev/null && echo "engine loop stopped" || true
  rm -f "$SEND/.loop.pid"
fi
dst="$LA/ai.outreach.keepawake.plist"
launchctl unload "$dst" 2>/dev/null && echo "keep-awake agent unloaded" || echo "keep-awake not loaded"
rm -f "$dst"
echo "torn down. Data preserved. Re-arm later: pipeline/install_engine.sh then pipeline/go.sh."
