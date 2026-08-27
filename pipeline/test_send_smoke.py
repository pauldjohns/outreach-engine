#!/usr/bin/env python3
"""End-to-end smoke test: actually CALL run() in dry-run mode.

Why this exists. On 2026-07-20 the suite was 206 tests green and CI passed, while
send_outreach.run() crashed on its first line of zone-quota work with
UnboundLocalError: a function-local `from collections import Counter` shadowed the
module-level import, leaving Counter unbound earlier in the same function. Every
unit test exercised helpers directly, so nothing ever entered run() and the crash
was invisible until a render was requested by hand. It would have hit the LIVE
path identically.

This test drives the whole function against a temp config and worklist: no Gmail,
no network, nothing sent. It is the cheapest possible guard against "all the parts
pass, the whole thing does not run".

Run: python3 pipeline/test_send_smoke.py
"""
import csv, json, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import send_outreach as so

PASS = 0; FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1
    else: FAIL += 1; print(f"FAIL: {name}{(' — ' + detail) if detail else ''}")

TEMPLATE = ("Subject: a note for {{first_name}}\n\n"
            "Hi {{first_name}}, this body is comfortably past the minimum length gate.\n")

ROWS = [
    # spread across zones so the quota code actually runs
    ("ana@alpha.dev",   "Ana",   "Madrid, , Spain",                        "REV_A_staging"),
    ("ben@bravo.io",    "Ben",   "Berlin, Berlin, Germany",                "REV_B_bugreports"),
    ("cara@charlie.co", "Cara",  "Boston, Massachusetts, United States",   "REV_C_repro"),
    ("dan@delta.dev",   "Dan",   "Denver, Colorado, United States",        "REV_A_staging"),
    ("eve@echo.io",     "Eve",   "Vancouver, British Columbia, Canada",    "REV_B_bugreports"),
    ("",                "Ghost", "Nowhere",                                "REV_A_staging"),   # no email
]

with tempfile.TemporaryDirectory() as td:
    # repoint every module global so the real send log / HALT / lock are untouched
    so.SEND = td
    so.SEND_LOG = os.path.join(td, "send_log.csv")
    so.SUPPRESSION = os.path.join(td, "suppression.csv")
    so.BOUNCES = os.path.join(td, "bounces.csv")
    so.STOP = os.path.join(td, "STOP"); so.HALT = os.path.join(td, "HALT")
    so.LOCK = os.path.join(td, ".lock"); so.DRYRUN = os.path.join(td, "dryrun")

    tmpl = os.path.join(td, "t.md"); open(tmpl, "w").write(TEMPLATE)
    wl = os.path.join(td, "wl.csv")
    with open(wl, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["email", "first_name", "owner_location", "segment",
                                          "status", "contacted_on", "channel", "company_domain"])
        w.writeheader()
        for e, n, loc, seg in ROWS:
            w.writerow({"email": e, "first_name": n, "owner_location": loc, "segment": seg,
                        "status": "", "contacted_on": "", "channel": "", "company_domain": ""})

    cfg = {"dry_run": True, "template_approved": False,
           "from_address": "sender@example.com", "from_name": "Test Sender",
           "worklist": wl, "template": tmpl, "segments": [],
           "require_email_valid": False, "skip_role_addresses": True,
           "one_per_domain": True, "zone_quota": True, "eu_share": 0.5,
           "tz_scheduler": True, "default_timezone": "America/New_York",
           "target_hour": 9, "window_before_hours": 1, "window_after_hours": 2,
           "runner_cadence_minutes": 30, "daily_cap": 100, "jitter_seconds": [0, 0],
           "send_windows": [["00:00", "23:59"]],
           "bounce_rate_halt": 0.05, "bounce_window": 100, "bounce_burst": [4, 10]}
    cfgp = os.path.join(td, "config.json"); json.dump(cfg, open(cfgp, "w"))

    # THE POINT OF THIS TEST: run() must complete without raising.
    raised = None
    try:
        so.run(cfgp, limit=None, ignore_window=True)
    except Exception as e:
        raised = f"{type(e).__name__}: {e}"
    check("run() completes without raising", raised is None, raised or "")

    rendered = sorted(os.listdir(so.DRYRUN)) if os.path.isdir(so.DRYRUN) else []
    check("dry-run wrote a file per selectable row", len(rendered) == 5,
          f"got {len(rendered)}: {rendered}")
    check("the row with no email was skipped", not any("ghost" in f.lower() for f in rendered))
    check("nothing was actually sent (no live log)",
          not os.path.exists(so.SEND_LOG) or
          not [r for r in so.read_csv(so.SEND_LOG) if r.get("mode") == "live"])

    body = "\n".join(open(os.path.join(so.DRYRUN, f)).read() for f in rendered)
    check("greeting merged a real name", "Hi Ana," in body and "Hi Cara," in body)
    check("no unrendered merge tokens", "{{" not in body)
    check("zone stamped on the render header", "TZ: Europe/Madrid" in body and "TZ: America/Denver" in body)

    # run() again with the same log: must stay idempotent and still not raise
    raised2 = None
    try:
        so.run(cfgp, limit=None, ignore_window=True)
    except Exception as e:
        raised2 = f"{type(e).__name__}: {e}"
    check("second run() also completes", raised2 is None, raised2 or "")

    # and with the quota switched off, so both branches of that code are executed
    cfg["zone_quota"] = False; json.dump(cfg, open(cfgp, "w"))
    raised3 = None
    try:
        so.run(cfgp, limit=None, ignore_window=True)
    except Exception as e:
        raised3 = f"{type(e).__name__}: {e}"
    check("run() completes with zone_quota disabled", raised3 is None, raised3 or "")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
