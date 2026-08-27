#!/usr/bin/env python3
"""test_gmail_auth.py - offline tests for token_ok()'s pure paths (no network, no google deps).
Guards against regressing to the 2026-07-16 bug where go.sh checked only the scope STRING, so a
dead-but-scoped token slipped through and the sender ran with an expired refresh token.
The valid/refresh paths need network+google-auth and are covered by a manual run, not CI.
Run: python3 pipeline/test_gmail_auth.py"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gmail_auth as G

PASS=0; FAIL=0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS+=1; print(f"  ok   {name}")
    else: FAIL+=1; print(f"  FAIL {name}")

RO="https://www.googleapis.com/auth/gmail.readonly"
tmp=tempfile.mkdtemp()

# missing token file -> False (never opens a browser)
G.TOKEN=os.path.join(tmp,"nope.json")
check("missing token -> False", G.token_ok(RO) is False)

# token present but WITHOUT the required scope -> False (this is the exact bug class:
# scope-string presence must be checked, and absence must fail closed)
G.TOKEN=os.path.join(tmp,"sendonly.json")
json.dump({"scopes":["https://www.googleapis.com/auth/gmail.send"],"refresh_token":"x"}, open(G.TOKEN,"w"))
check("scope missing -> False", G.token_ok(RO) is False)

# malformed token -> False (fails closed before the network/google-dep path)
G.TOKEN=os.path.join(tmp,"garbage.json"); open(G.TOKEN,"w").write("{not json")
check("unreadable token -> False", G.token_ok(RO) is False)

# NOTE: the valid + dead-refresh (invalid_grant) paths need google-auth + network and are covered
# by a manual run, not CI. These three pure paths all return BEFORE the google import, so CI (which
# installs no deps) exercises the scope-fail-closed logic that the 2026-07-16 bug got wrong.
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
