#!/usr/bin/env bash
# stop.sh - STOP sending immediately. The launchd agent keeps running but skips the
# send step while outreach/send/STOP exists. Resume with: rm outreach/send/STOP
# (or re-run pipeline/go.sh). To fully tear down the service: pipeline/uninstall_engine.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
touch "outreach/send/STOP"
echo "STOP set - no further sends. Already-in-flight send (if any) finishes its current message."
echo "resume: rm outreach/send/STOP    |    tear down: pipeline/uninstall_engine.sh"
