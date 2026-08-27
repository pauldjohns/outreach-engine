#!/usr/bin/env python3
"""bounces.csv must record WHY, not just hard/soft.

A hard bounce is two different emergencies wearing one label. 5.1.1 / 5.1.10 / 5.4.1 mean the
mailbox does not exist -- a list-quality problem, fixed by verifying addresses before send.
5.7.x means the receiver is refusing our mail -- a reputation problem, where sending more makes
it worse. bounces.csv declared a 'detail' column from the start and no caller ever passed it,
so every row was written two commas short and the SMTP status was dropped.

No network. Run: python3 pipeline/test_bounce_detail.py
"""
import csv, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import bounce_scan as bs

PASS = 0; FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1
    else: FAIL += 1; print(f"FAIL: {name}{(' — ' + detail) if detail else ''}")

# ---------- classification ----------
M365_UNKNOWN = """Delivery has failed to these recipients or groups:
farid@example-six.com
Reporting-MTA: dns; EUR05-VI1-obe.outbound.protection.outlook.com
Final-Recipient: rfc822;farid@example-six.com
Action: failed
Status: 5.1.10
Diagnostic-Code: smtp;550 5.1.10 RESOLVER.ADR.RecipientNotFound; Recipient not found by SMTP address lookup
"""
GOOGLE_UNKNOWN = """** Address not found **
Final-Recipient: rfc822; dana@example-four.co
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550-5.1.1 The email account that you tried to reach does not exist.
"""
POLICY = """Final-Recipient: rfc822; someone@example.com
Action: failed
Status: 5.7.1
Diagnostic-Code: smtp; 550 5.7.1 Message rejected due to sender reputation
"""
SOFT = """Final-Recipient: rfc822; someone@example.com
Action: delayed
Status: 4.2.2
Diagnostic-Code: smtp; 452 4.2.2 The recipient's mailbox is full
"""
# a DSN quotes the message it reports on; a stray code in the quoted part must not win
QUOTED = """Final-Recipient: rfc822; someone@example.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 user unknown
------ Original message ------
Subject: our 5.7.1 postmortem writeup
"""

for label, body, want_code, want_hard, want_in in [
        ("M365 recipient-not-found", M365_UNKNOWN, "5.1.10", True, "RESOLVER.ADR"),
        ("Google address-not-found", GOOGLE_UNKNOWN, "5.1.1", True, "does not exist"),
        ("policy / reputation",      POLICY,        "5.7.1", True, "reputation"),
        ("soft: mailbox full",       SOFT,          "4.2.2", False, "mailbox is full")]:
    code, det = bs._classify(body)
    check(f"{label}: code", code == want_code, f"got {code!r}")
    check(f"{label}: hard/soft", code.startswith("5") == want_hard, f"code {code!r}")
    check(f"{label}: detail carries the diagnostic", want_in in det, f"got {det!r}")

code, _ = bs._classify(QUOTED)
check("authoritative Status: beats a code quoted in the original message", code == "5.1.1", f"got {code!r}")

code, det = bs._classify("no status code anywhere in this body")
check("no code found -> soft, empty detail", not code.startswith("5") and det == "")

# the whole point: these two are both 'hard' and want opposite responses
h1, _ = bs._classify(M365_UNKNOWN); h2, _ = bs._classify(POLICY)
check("list-quality and reputation bounces are distinguishable",
      h1.startswith("5") and h2.startswith("5") and h1 != h2)

# ---------- _append honours an existing header ----------
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "bounces.csv")
    with open(p, "w", newline="") as f:
        csv.writer(f).writerow(["email", "type", "date", "detail"])
    bs._append(p, ["email", "type", "date", "detail"],
               {"email": "a@b.com", "type": "hard", "date": "2026-07-21", "detail": "5.1.1 smtp; 550"})
    rows = list(csv.DictReader(open(p)))
    check("detail is written", rows and rows[0].get("detail") == "5.1.1 smtp; 550",
          f"got {rows[0].get('detail') if rows else None!r}")
    check("row width matches the header", all(len(r) == 4 for r in csv.reader(open(p))))

with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "bounces.csv")
    with open(p, "w", newline="") as f:                      # header the code cannot satisfy
        csv.writer(f).writerow(["email", "type"])
    raised = False
    try:
        bs._append(p, ["email", "type", "date", "detail"], {"email": "a@b.com", "type": "hard"})
    except SystemExit:
        raised = True
    check("a column the file lacks raises rather than shifting fields", raised)

# ---------- the shipped file agrees with the writer ----------
shipped = os.path.join(ROOT, "outreach", "send", "bounces.csv")
if os.path.exists(shipped):
    with open(shipped, newline="") as f:
        hdr = next(csv.reader(f), [])
    check("shipped bounces.csv declares 'detail'", "detail" in hdr, str(hdr))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
