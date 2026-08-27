#!/usr/bin/env python3
"""The send log must survive a column being added to it.

2026-07-21: commit 98e6b1c added esp/refreshed to LOG_COLS at positions 6-7 while the live
send_log.csv carried them appended at the END (the backfill put them there). append_log() writes
a header only when the file is new, so every subsequent row went in LOG_COLS order into a file
whose header said otherwise. Read back, message_id landed under 'mode' -- and run() derives
live_log, sent_today and the dedup set from mode == 'live'. sent_today would have read 0 on every
cycle, re-granting the full daily cap every 30 minutes against a shared sending address.

No network. Run: python3 pipeline/test_send_logschema.py
"""
import csv, io, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import send_outreach as so

PASS = 0; FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1
    else: FAIL += 1; print(f"FAIL: {name}{(' — ' + detail) if detail else ''}")

ROW = {"ts": "2026-07-22T00:05:00", "to": "a@b.com", "company_domain": "b.com",
       "segment": "REV_A_staging", "zone": "UTC+02", "esp": "google", "refreshed": "2026-07-09",
       "subject": "quick question", "message_id": "18f2ac9d1e4b",
       "run_id": "20260722T000500", "mode": "live"}

# ---------- the shipped log agrees with the code ----------
shipped = os.path.join(ROOT, "outreach", "send", "send_log.csv")
if os.path.exists(shipped):
    with open(shipped, newline="") as f:
        hdr = next(csv.reader(f), [])
    check("shipped send_log.csv declares the same COLUMN SET as LOG_COLS",
          set(hdr) == set(so.LOG_COLS),
          f"only in file: {sorted(set(hdr) - set(so.LOG_COLS))}; "
          f"only in code: {sorted(set(so.LOG_COLS) - set(hdr))}")
    check("every shipped row has one field per declared column",
          all(len(r) == len(hdr) for r in csv.reader(open(shipped)) if r),
          f"ragged rows: {[i for i, r in enumerate(csv.reader(open(shipped)), 1) if r and len(r) != len(hdr)][:8]}")

# ---------- appending honours the file's own column ORDER ----------
# The order below is deliberately NOT LOG_COLS order: it is the pre-98e6b1c order with the two
# new columns appended at the end, which is exactly what the live file looks like.
LEGACY = ["ts", "to", "company_domain", "segment", "zone", "subject", "message_id",
          "run_id", "mode", "esp", "refreshed"]
check("fixture order differs from LOG_COLS (otherwise this test proves nothing)",
      LEGACY != so.LOG_COLS and set(LEGACY) == set(so.LOG_COLS))

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "send_log.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEGACY); w.writeheader(); w.writerow(ROW)
    orig = so.SEND_LOG
    try:
        so.SEND_LOG = path
        so.append_log({**ROW, "ts": "2026-07-22T00:10:00", "to": "c@d.com"})
        rows = list(csv.DictReader(open(path)))
    finally:
        so.SEND_LOG = orig

    check("appended row round-trips through the file's header", len(rows) == 2)
    got = rows[-1]
    for col in LEGACY:
        want = ROW[col] if col not in ("ts", "to") else {"ts": "2026-07-22T00:10:00", "to": "c@d.com"}[col]
        check(f"appended row: {col} reads back intact", got.get(col) == want,
              f"got {got.get(col)!r}, want {want!r}")
    # the one that actually matters: run() keys the cap, the dedup set and the throttle off this
    check("mode == 'live' survives the append (cap/dedup/throttle depend on it)",
          got.get("mode") == "live", f"got {got.get('mode')!r}")

# ---------- a genuinely different column SET must fail loudly, not silently shift ----------
with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "send_log.csv")
    with open(path, "w", newline="") as f:
        csv.writer(f).writerow(["ts", "to", "mode"])          # missing columns entirely
    orig = so.SEND_LOG
    raised = False
    try:
        so.SEND_LOG = path
        so.append_log(ROW)
    except SystemExit:
        # deliberately SystemExit, not Exception: it must propagate past the per-recipient
        # `except Exception` handlers in the send loop and abort the run, not burn the batch
        raised = True
    finally:
        so.SEND_LOG = orig
    check("a column-SET mismatch raises rather than writing a shifted row", raised)
    check("nothing was appended after the schema mismatch",
          len(list(csv.reader(open(path)))) == 1)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
