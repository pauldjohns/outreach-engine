#!/usr/bin/env bash
# install_engine.sh - bring the outreach engine up on this Mac. Two parts:
#   1. keepawake launchd agent (caffeinate -dis) - keeps the Mac awake 24/7. launchd CAN run this
#      (it touches no protected files) and it survives reboot.
#   2. the engine loop (engine_loop.sh) - runs the 30-min cycle. Started DETACHED from THIS shell
#      because macOS TCC blocks launchd from ~/Documents, but a nohup child of a granted shell
#      (Terminal) inherits access. Run this from Terminal.
# After this the engine is RUNNING but HOLDING in dry-run - it sends nothing until pipeline/go.sh.
# Safe to re-run (idempotent).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
LA="$HOME/Library/LaunchAgents"; mkdir -p "$LA"; SEND="outreach/send"

# 1. keep-awake launchd agent (survives reboot; no file access so TCC-clean)
src="pipeline/ai.outreach.keepawake.plist"; dst="$LA/ai.outreach.keepawake.plist"
launchctl unload "$dst" 2>/dev/null || true
cp "$src" "$dst"; launchctl load "$dst"
echo "keep-awake agent loaded (caffeinate -dis)"

# 2. engine loop, detached (retains Documents access; survives Terminal close, not a reboot)
if [ -f "$SEND/.loop.pid" ] && kill -0 "$(cat "$SEND/.loop.pid" 2>/dev/null)" 2>/dev/null; then
  echo "engine loop already running (pid $(cat "$SEND/.loop.pid"))"
else
  nohup bash pipeline/engine_loop.sh >> "$SEND/chain.log" 2>&1 &
  disown 2>/dev/null || true
  sleep 2
  echo "engine loop started (pid $(cat "$SEND/.loop.pid" 2>/dev/null))"
fi

echo ""
echo "RUNNING + HOLDING in dry-run - nothing sends yet. Keep the Mac plugged into AC."
echo "verify:  launchctl list | grep keepawake   &&   tail -f $SEND/chain.log"
echo "arm it:  pipeline/go.sh"
