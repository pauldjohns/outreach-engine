#!/usr/bin/env bash
# run_chain.sh - one outreach cycle, run by engine_loop.sh every ~30 min.
# HOLDS (does nothing but a heartbeat) while config dry_run:true; runs the live
# chain once armed via pipeline/go.sh (dry_run:false). The timezone scheduler in
# send_outreach.py decides who is in their local morning each fire. Discovery +
# bounce scan are throttled to ~hourly so the 30-min send cadence doesn't over-poll.
# Whole-cycle mkdir lock: an overlapping run is a no-op (never double-sends).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SEND="outreach/send"
STAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"
mkdir -p "$SEND"
# (chain.log grows only ~a few KB/day of heartbeat + send lines; the loop owns the append fd, so
#  run_chain must NOT rotate it here - truncating the loop's open file would orphan its writes.)

# whole-cycle lock via atomic mkdir (macOS has no flock). A live send legitimately runs for
# HOURS, so we do NOT take over on mtime age (that would tear out a running cycle mid-send and
# let poll+send run concurrently, corrupting the worklist). Take over only if the owner PID is
# actually dead, with an 8h absolute backstop (> worst-case send pass). The Python fcntl
# .send.lock is the double-send backstop.
LOCKDIR="$SEND/.chain.lock.d"
if [ -d "$LOCKDIR" ]; then
  opid="$(cat "$LOCKDIR/pid" 2>/dev/null || echo '')"
  age="$(( $(date +%s) - $(stat -f %m "$LOCKDIR" 2>/dev/null || echo 0) ))"
  if { [ -n "$opid" ] && ! kill -0 "$opid" 2>/dev/null; } || [ "$age" -ge 28800 ]; then
    rm -rf "$LOCKDIR" 2>/dev/null || true    # owner process dead, or 8h stale backstop
  fi
fi
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  echo "[$STAMP] previous cycle still running (pid $(cat "$LOCKDIR/pid" 2>/dev/null)) - skipping this slot."; exit 0
fi
echo $$ > "$LOCKDIR/pid"
trap '[ "$(cat "$LOCKDIR/pid" 2>/dev/null)" = "$$" ] && rm -rf "$LOCKDIR" 2>/dev/null; true' EXIT
if [ -f "$SEND/STOP" ]; then echo "[$STAMP] STOP present - skipping."; exit 0; fi

# holding until armed: config dry_run:true means "not live yet"
DRY="$(python3 -c "import json;print(json.load(open('$SEND/config.json')).get('dry_run',True))" 2>/dev/null || echo True)"
if [ "$DRY" != "False" ]; then
  echo "[$STAMP] holding (dry_run) - engine armed but not live. Run pipeline/go.sh to start."; exit 0
fi

# --- live chain ---
# discovery + bounce/opt-out scan at most hourly (send runs every fire)
# Top up the worklist only when it is running short. UNSENT counts rows with no status; we keep
# QUEUE_DAYS x cap queued so the engine never runs dry mid-window, while credits are still spent
# in proportion to what actually sends. Four days covers a Tue-Fri run (the operator, 2026-07-20).
CAP=$(python3 -c "import json;print(int(json.load(open('$SEND/config.json')).get('daily_cap',25)))" 2>/dev/null || echo 25)
UNSENT=$(python3 -c "
import csv,os
p='outreach/worklist_review.csv'
print(sum(1 for r in csv.DictReader(open(p)) if not (r.get('status') or '').strip()) if os.path.exists(p) else 0)
" 2>/dev/null || echo 0)
QUEUE_DAYS=4
TOPUP=$(( CAP * QUEUE_DAYS - UNSENT )); [ "$TOPUP" -lt 0 ] && TOPUP=0

POLLED="$SEND/.last_poll"
if [ ! -f "$POLLED" ] || [ "$(( $(date +%s) - $(stat -f %m "$POLLED" 2>/dev/null || echo 0) ))" -ge 3300 ]; then
  echo "[$STAMP] queue: $UNSENT unsent, cap $CAP/day, target ${QUEUE_DAYS}d, top-up $TOPUP"
  echo "[$STAMP] source (apollo_pull)"
  python3 pipeline/apollo_pull.py --target "$TOPUP" || echo "[$STAMP] apollo_pull failed (continuing)"
  echo "[$STAMP] validate new addresses"
  python3 pipeline/validate_emails.py --write || echo "[$STAMP] validate_emails failed (continuing)"
  # Mailbox-level verification. validate_emails proves a DOMAIN accepts mail; this proves the
  # MAILBOX exists. Runs before the send below, so nothing is mailed unverified. Failure is safe:
  # rows simply keep a blank verify_status and the next cycle retries them, which is exactly
  # today's behaviour. Only 'undeliverable' is terminal -- see method/VERIFY-PLAN.md.
  VERIFY_ON=$(python3 -c "import json;print(json.load(open('$SEND/config.json')).get('verify_enabled') is True)" 2>/dev/null || echo False)
  if [ "$VERIFY_ON" = "True" ]; then
    VERIFY_MAX=$(python3 -c "import json;print(int(json.load(open('$SEND/config.json')).get('verify_max_per_run',150)))" 2>/dev/null || echo 150)
    echo "[$STAMP] verify queued addresses (Bouncer, max $VERIFY_MAX)"
    python3 pipeline/verify_queue.py --apply --limit "$VERIFY_MAX" || echo "[$STAMP] verify_queue failed (continuing, rows stay unverified)"
  fi
  echo "[$STAMP] bounce + opt-out scan"
  python3 pipeline/bounce_scan.py || echo "[$STAMP] bounce_scan failed (continuing)"
  touch "$POLLED"
fi

if [ -f "$SEND/HALT" ]; then
  echo "[$STAMP] HALT present ($(cat "$SEND/HALT")) - not sending; clear HALT to resume."
else
  echo "[$STAMP] send (timezone-windowed)"
  python3 pipeline/send_outreach.py
fi

# conversion dashboard (segments · sends · reply/account rates) — rebuilt ONCE each morning at/after
# 07:30 in $LOCAL_TZ. Runs inside this TCC-granted loop because launchd/cron can't
# read ~/Documents on this Mac. The 30-min cadence means it fires on the first cycle at/after 07:30
# (so by ~08:00 local). The timezone is forced so it holds even if the machine's timezone changes.
# .last_metrics stores the last-built date in $LOCAL_TZ. Failure here must never break the chain.
METRICS="$SEND/.last_metrics"
LOCAL_TZ="${LOCAL_TZ:-America/New_York}"   # set to the operator's timezone
MTODAY="$(TZ="$LOCAL_TZ" date '+%Y-%m-%d')"
MMIN=$(( 10#$(TZ="$LOCAL_TZ" date '+%H')*60 + 10#$(TZ="$LOCAL_TZ" date '+%M') ))   # minutes since local midnight
if [ "$(cat "$METRICS" 2>/dev/null)" != "$MTODAY" ] && [ "$MMIN" -ge 450 ]; then   # 450 = 07:30
  echo "[$STAMP] build metrics dashboard (daily 07:30 MT)"
  python3 pipeline/metrics.py || echo "[$STAMP] build_metrics failed (continuing)"
  echo "$MTODAY" > "$METRICS"
fi
echo "[$STAMP] cycle done."
