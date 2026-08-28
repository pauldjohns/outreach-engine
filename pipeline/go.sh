#!/usr/bin/env bash
# go.sh - "GO". Arms the outreach engine to send live. Idempotent. Run it from Terminal.
#   1. grants Gmail read access (one-time browser consent) so the circuit breaker
#      and reply-opt-out scan work,
#   2. flips config to live (dry_run:false, template_approved:true),
#   3. makes sure the engine loop is running (starts it, detached, if not),
#   4. clears any STOP and fires one cycle now so in-window recipients go immediately.
# The engine loop takes over from there (every 30 min, for as long as the Mac is up).
# To stop: pipeline/stop.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
SEND="outreach/send"; CFG="$SEND/config.json"
TOKEN="$HOME/.config/outreach-engine/token.json"

echo "== arming the outreach engine =="

# 1. ensure the token has gmail.readonly AND actually works. Re-consent if not.
#    (token_ok exercises a real refresh, so a dead-but-scoped token — e.g. an expired refresh
#    token — triggers re-consent instead of silently slipping through. 2026-07-16 fix.)
NEED=$(python3 - <<'PY'
import sys, warnings; warnings.filterwarnings("ignore")
sys.path.insert(0, "pipeline")
try:
    import gmail_auth
    print("no" if gmail_auth.token_ok("https://www.googleapis.com/auth/gmail.readonly") else "yes")
except Exception:
    print("yes")
PY
)
if [ "$NEED" = "yes" ]; then
  echo "  granting Gmail access (send + bounce/opt-out read) - a browser window will open, approve it..."
  rm -f "$TOKEN"
  python3 pipeline/gmail_auth.py || { echo "  consent failed - not arming. Re-run pipeline/go.sh."; exit 1; }
else
  echo "  Gmail token present, scoped, and refreshes OK."
fi

# 2. flip to live
python3 - <<'PY'
import json
p = "outreach/send/config.json"
c = json.load(open(p))
c["dry_run"] = False
c["template_approved"] = True
json.dump(c, open(p, "w"), indent=1)
print("  config: dry_run=false, template_approved=true")
PY

# 3. (re)start the engine loop under THIS shell's Documents access (you're running go.sh from
#    Terminal), replacing any holding loop from install so the LIVE loop definitely has the grant.
OLD="$(cat "$SEND/.loop.pid" 2>/dev/null || echo '')"
[ -n "$OLD" ] && kill "$OLD" 2>/dev/null || true
sleep 1; rm -f "$SEND/.loop.pid"
nohup bash pipeline/engine_loop.sh >> "$SEND/chain.log" 2>&1 &
disown 2>/dev/null || true; sleep 2
echo "  engine loop running (pid $(cat "$SEND/.loop.pid" 2>/dev/null))"

# 4. clear STOP/HALT and fire one cycle now
rm -f "$SEND/STOP" "$SEND/HALT"
echo "  firing first cycle now (in-window recipients send immediately)..."
bash pipeline/run_chain.sh || true
echo ""
echo "== ARMED. Live, and the engine loop keeps it running (every 30 min, while the Mac is up). =="
echo "   watch:  tail -f $SEND/chain.log"
echo "   sent:   column -s, -t $SEND/send_log.csv | less -S     (or the dashboard)"
echo "   stop:   pipeline/stop.sh   |   after a reboot: re-run pipeline/go.sh"
