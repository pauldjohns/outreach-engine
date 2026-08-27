#!/usr/bin/env bash
# engine_loop.sh - the outreach "server". Runs one cycle, sleeps `runner_cadence_minutes`,
# repeats. Launched DETACHED (nohup) from a shell that has Documents access, because macOS TCC
# blocks a launchd agent from reading ~/Documents (a launchd timer errors "Operation not
# permitted"; a nohup child of a granted shell inherits access). The keepawake launchd agent
# keeps the Mac awake so this loop keeps ticking. Single-instance via a pidfile.
# Stop sending (soft): touch outreach/send/STOP. Stop the loop: pipeline/uninstall_engine.sh
# (or touch outreach/send/LOOP_STOP). Survives Terminal close; NOT a reboot - re-run go.sh after one.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
SEND="outreach/send"; PIDF="$SEND/.loop.pid"; mkdir -p "$SEND"

if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
  echo "[loop] already running (pid $(cat "$PIDF")) - not starting a second."; exit 0
fi
echo $$ > "$PIDF"
trap 'rm -f "$PIDF"' EXIT
rm -f "$SEND/LOOP_STOP"

CAD=$(python3 -c "import json;print(int(json.load(open('$SEND/config.json')).get('runner_cadence_minutes',30))*60)" 2>/dev/null || echo 1800)
echo "[loop] $(date '+%F %T %Z') started (pid $$, cadence ${CAD}s)"
while true; do
  bash pipeline/run_chain.sh
  [ -f "$SEND/LOOP_STOP" ] && { echo "[loop] LOOP_STOP present - exiting."; break; }
  sleep "$CAD"
done
