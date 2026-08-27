#!/usr/bin/env python3
"""test_send_authfatal.py - the sender must treat a dead/revoked OAuth token (invalid_grant /
RefreshError) as a RUN-FATAL, provably-not-delivered failure: roll back the write-ahead 'pending'
row for the in-flight recipient (so the lead is NOT parked as sent), write HALT, and abort the run
- while leaving AMBIGUOUS errors (timeout, 5xx, connection reset) unchanged (pending+error, treated
as sent per the write-ahead model). Regression cover for the 2026-07-16 outage where one dead token
burned 27 leads into permanent skip. No network. Run: python3 pipeline/test_send_authfatal.py"""
import csv, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_outreach as S
import gmail_auth
try:
    from google.auth.exceptions import RefreshError   # real type when google-auth is installed (prod/local)
except Exception:                                      # CI runs offline unit tests without google-auth; a
    class RefreshError(Exception):                     # synthetic stand-in still exercises _auth_fatal's
        pass                                           # "invalid_grant in str(e)" path (the CI-relevant one)

PASS = 0; FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ok   {name}")
    else: FAIL += 1; print(f"  FAIL {name}")

TEMPLATE = ("Subject: Quick note on {{first_name}}\n\n"
            "Hi {{first_name}}, saw your project and wanted to reach out. "
            "This body is comfortably over the twenty-character minimum.\n")
WORKLIST = ("email,company_domain,status\n"
            "a@x.com,a/one,\nb@x.com,b/two,\nc@x.com,c/three,\nd@x.com,d/four,\n")

def setup():
    """Fresh temp SEND dir + a live config; returns config_path. Repoints module globals so the
    real send_log / HALT / lock are never touched."""
    tmp = tempfile.mkdtemp(prefix="authfatal_")
    S.SEND = tmp
    S.STOP = os.path.join(tmp, "STOP"); S.HALT = os.path.join(tmp, "HALT")
    S.LOCK = os.path.join(tmp, ".send.lock"); S.SEND_LOG = os.path.join(tmp, "send_log.csv")
    S.BOUNCES = os.path.join(tmp, "bounces.csv"); S.SUPPRESSION = os.path.join(tmp, "suppression.csv")
    S.DRYRUN = os.path.join(tmp, "dryrun")
    tmpl = os.path.join(tmp, "template.md"); open(tmpl, "w").write(TEMPLATE)
    wl = os.path.join(tmp, "worklist.csv"); open(wl, "w").write(WORKLIST)
    cfg = {"from_address": "sender@example.com", "from_name": "the operator", "reply_to": "sender@example.com",
           "dry_run": False, "template_approved": True, "daily_cap": 100, "jitter_seconds": [0, 0],
           "one_per_domain": False,
           "send_windows": [["00:00", "23:59"]], "bounce_rate_halt": 0.06, "bounce_window": 50,
           "bounce_burst": [4, 10], "segments": [], "require_email_valid": False,
           "skip_role_addresses": False, "region_gate": False, "tz_scheduler": False,
           "template": tmpl, "worklist": wl}
    cfgp = os.path.join(tmp, "config.json"); json.dump(cfg, open(cfgp, "w"))
    return cfgp

def rows_of(mode=None):
    if not os.path.exists(S.SEND_LOG): return []
    rs = list(csv.DictReader(open(S.SEND_LOG)))
    return [r for r in rs if mode is None or r.get("mode") == mode]

def tos(mode): return {(r["to"] or "").lower() for r in rows_of(mode)}

# ---- Test 1: auth-fatal MID-LOOP -> rollback pending, HALT, abort ----
print("Test 1: mid-run invalid_grant rolls back pending, writes HALT, aborts")
cfgp = setup()
calls = {"n": 0}
def send_authdie(svc, to, subject, body, from_addr=None):
    calls["n"] += 1
    if calls["n"] == 1: return "mid_a"                      # 1st recipient succeeds
    raise RefreshError("invalid_grant: Token has been expired or revoked.",
                       {"error": "invalid_grant"})          # 2nd: token dies
gmail_auth.service = lambda: object()
gmail_auth.send = send_authdie
S.run(cfgp, None)
check("exactly 1 live row (a)", tos("live") == {"a@x.com"})
check("in-flight recipient b's pending was ROLLED BACK", "b@x.com" not in tos("pending"))
check("only a remains as pending (its live twin)", tos("pending") == {"a@x.com"})
check("no orphan_pending left (pending - live == empty)", tos("pending") - tos("live") == set())
check("HALT written", os.path.exists(S.HALT))
check("HALT names an auth failure", os.path.exists(S.HALT) and "auth" in open(S.HALT).read().lower())
check("c,d never attempted (no rows)", not ({"c@x.com", "d@x.com"} & (tos("pending") | tos("live") | tos("error"))))
already = tos("live") | tos("pending")
check("b,c,d remain selectable next run (not in already)", not ({"b@x.com", "c@x.com", "d@x.com"} & already))

# ---- Test 2: ambiguous error -> pending+error kept, NO halt, run continues (unchanged) ----
print("\nTest 2: ambiguous error keeps pending+error (treated as sent), no HALT, continues")
cfgp = setup()
calls2 = {"n": 0}
def send_flaky(svc, to, subject, body, from_addr=None):
    calls2["n"] += 1
    if calls2["n"] == 1: raise RuntimeError("network timeout / connection reset")
    return f"mid_{calls2['n']}"
gmail_auth.service = lambda: object()
gmail_auth.send = send_flaky
S.run(cfgp, None)
check("a kept as pending (parked as sent)", "a@x.com" in tos("pending"))
check("a has an error row", "a@x.com" in tos("error"))
check("a has NO live row", "a@x.com" not in tos("live"))
check("b,c,d delivered (live)", {"b@x.com", "c@x.com", "d@x.com"} <= tos("live"))
check("HALT NOT written (ambiguous is not run-fatal)", not os.path.exists(S.HALT))
check("all 4 attempted (no abort)", len(tos("pending")) == 4)

# ---- Test 3: auth-fatal at CLIENT BUILD -> HALT before any send, zero rows ----
print("\nTest 3: invalid_grant while building the client HALTs before any pending is written")
cfgp = setup()
def service_authdie():
    raise RefreshError("invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"})
gmail_auth.service = service_authdie
gmail_auth.send = lambda *a, **k: "should_not_be_called"
S.run(cfgp, None)
check("HALT written at build", os.path.exists(S.HALT))
check("no pending rows (nothing attempted)", tos("pending") == set())
check("no live rows", tos("live") == set())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
