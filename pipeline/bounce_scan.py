#!/usr/bin/env python3
"""
bounce_scan.py - detect bounces AND reply-based opt-outs, feed the breaker + suppression.

Two scans of the mailbox (both need gmail.readonly):
  - bounces: delivery-failure notices (mailer-daemon / postmaster) -> parse the RFC-3464
    Final-Recipient/Original-Recipient DSN field (NOT "first address in body", which can grab
    a quoted header or the sender) -> bounces.csv; hard (5.x.x) bounces -> suppression.
  - opt-outs: inbound replies containing stop/remove/unsubscribe language -> suppress the
    replier so they're never contacted again (deliverability, not compliance).
Never suppresses our own from_address. The breaker in send_outreach.py reads bounces.csv;
without this scan it has no signal.

REQUIRES gmail.readonly. The sender uses gmail.send only (least privilege), so this needs a
ONE-TIME re-consent before it works:
  1. add "https://www.googleapis.com/auth/gmail.readonly" to GMAIL_SCOPES in gmail_auth.py
  2. rm ~/.config/outreach-engine/token.json
  3. python3 pipeline/gmail_auth.py     (re-consents with the added scope)
Until then it exits cleanly (breaker stays inert; safe in dry-run). Documented pre-live step
in method/AUTOSEND.md.

  python3 pipeline/bounce_scan.py [--days N]
"""
import argparse, csv, json, os, re, sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
SEND = os.path.join(ROOT, "outreach", "send")
SEND_LOG = os.path.join(SEND, "send_log.csv")
BOUNCES = os.path.join(SEND, "bounces.csv")
SUPPRESSION = os.path.join(SEND, "suppression.csv")
CONFIG = os.path.join(SEND, "config.json")
READONLY = "https://www.googleapis.com/auth/gmail.readonly"
ADDR_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# RFC-3464 DSN fields carry the authoritative failed recipient
FINAL_RCPT_RE = re.compile(r"(?:Final|Original)-Recipient:\s*(?:rfc822;)?\s*([^\s<>]+@[^\s<>]+)", re.I)
STATUS_RE = re.compile(r"\b([45])\.\d{1,3}\.\d{1,3}\b")
# RFC 3464 machine-readable fields. Prefer these over the loose scan: a DSN quotes the original
# message, so the first 5.x.x in the body can belong to the text being reported on, not the report.
DSN_STATUS_RE = re.compile(r"^Status:\s*([45]\.\d{1,3}\.\d{1,3})", re.I | re.M)
DIAG_RE = re.compile(r"^Diagnostic-Code:\s*(.+)$", re.I | re.M)
DAEMON_HINTS = ("mailer-daemon", "postmaster", "mail delivery subsystem", "mail delivery system")
OPTOUT_RE = re.compile(r"\b(unsubscribe|opt[\s-]?out|remove me|take me off|stop emailing|stop contacting|do not (?:email|contact))\b", re.I)


def _norm(e): return (e or "").strip().lower().strip("<>")

def _from_address():
    try:
        return _norm(json.load(open(CONFIG)).get("from_address"))
    except Exception:
        return ""

def _existing(path, key):
    return {_norm(r.get(key)) for r in (list(csv.DictReader(open(path))) if os.path.exists(path) else [])}

def _append(path, cols, row):
    """cols is the SET this writer knows; an existing file's own header wins on ORDER and width.

    Same failure mode the send log hit on 2026-07-21: a header written under one column list, rows
    appended under another, every field silently shifted. Here it also fixes a narrower bug --
    bounces.csv has always declared a 'detail' column that callers never passed, so every row was
    written two commas short and the SMTP status was dropped on the floor.
    """
    new = not os.path.exists(path)
    if not new:
        with open(path, newline="") as f:
            hdr = next(csv.reader(f), [])
        if hdr:
            if not set(hdr) >= set(cols):
                raise SystemExit(f"[bounce] {path} schema mismatch — refusing to write.\n"
                                 f"        only in code: {sorted(set(cols) - set(hdr))}")
            cols = hdr
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new: w.writeheader()
        w.writerow(row)

def _classify(body):
    """(status_code, detail) from a DSN body. detail is what tells the two emergencies apart.

    A hard bounce is not one thing. 5.1.1 / 5.1.10 / 5.4.1 are 'this mailbox does not exist' --
    a list-quality problem, fixed by verifying addresses before send. 5.7.x is 'we are refusing
    your mail' -- a reputation or policy problem, where sending more makes it worse. Recording
    only hard/soft collapses them, and the remedies point in opposite directions.
    """
    dsn = DSN_STATUS_RE.search(body)
    loose = STATUS_RE.search(body)
    code = dsn.group(1) if dsn else (loose.group(0) if loose else "")
    diag = DIAG_RE.search(body)
    txt = " ".join(diag.group(1).split())[:160] if diag else ""
    return code, " ".join(x for x in (code, txt) if x)

def _decode(payload):
    import base64
    out = []
    def walk(p):
        b = p.get("body", {}).get("data")
        if b:
            try: out.append(base64.urlsafe_b64decode(b).decode("utf-8", "ignore"))
            except Exception: pass
        for part in p.get("parts", []) or []:
            walk(part)
    walk(payload)
    return "\n".join(out)

def _headers(full):
    return {h["name"].lower(): h["value"] for h in full.get("payload", {}).get("headers", [])}

def _suppress(email, reason, supp):
    if email and email not in supp:
        _append(SUPPRESSION, ["email", "reason", "added_on"],
                {"email": email, "reason": reason, "added_on": date.today().isoformat()})
        supp.add(email)

def _token_has_readonly(gmail_auth):
    try:
        return READONLY in json.load(open(gmail_auth.TOKEN)).get("scopes", [])
    except Exception:
        return False

def _rows(path):
    import csv as _csv
    return list(_csv.DictReader(open(path))) if os.path.exists(path) else []

def scan(days):
    import gmail_auth
    # Check the actual TOKEN scopes, not the code's requested scopes: the token only gains
    # readonly at go-live consent (pipeline/go.sh). Until then, degrade cleanly (breaker inert).
    if not _token_has_readonly(gmail_auth):
        print("[bounce] gmail.readonly not yet granted (run pipeline/go.sh) — skipping. Breaker inert; safe.")
        return 0
    import warnings; warnings.filterwarnings("ignore")
    from googleapiclient.discovery import build
    svc = build("gmail", "v1", credentials=gmail_auth._creds(), cache_discovery=False)
    me = _from_address()
    known_b = _existing(BOUNCES, "email"); supp = _existing(SUPPRESSION, "email")
    today = date.today().isoformat(); nb = no = 0
    # Scope to THIS campaign. The mailbox is shared with the sibling campaign, so an unscoped scan
    # records the other campaign's bounces here: on 2026-07-20 this repo had sent nothing and still
    # logged 2 bounces, both the sibling campaign's. Harmless for the breaker (it intersects bounces with this
    # repo's own send log) but it makes bounces.csv unreadable and inflates the apparent rate.
    # Empty send log -> record nothing, which is correct: we cannot have bounced yet.
    ours = {_norm(r.get("to")) for r in _rows(SEND_LOG) if r.get("to")}

    # --- bounces ---
    q = (f'newer_than:{days}d subject:("Delivery Status Notification" OR "Undelivered" OR '
         f'"Undeliverable" OR "failure" OR "Address not found" OR "Mail delivery failed")')
    for m in svc.users().messages().list(userId="me", q=q, maxResults=200).execute().get("messages", []):
        full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        if not any(h in _headers(full).get("from", "").lower() for h in DAEMON_HINTS):
            continue
        body = _decode(full.get("payload", {})) + " " + full.get("snippet", "")
        fr = FINAL_RCPT_RE.search(body)                      # authoritative recipient first
        recip = _norm(fr.group(1)) if fr else None
        if not recip:                                        # fallback: first non-daemon, non-self address
            recip = next((a for a in (_norm(x) for x in ADDR_RE.findall(body))
                          if a != me and not any(h in a for h in ("mailer-daemon", "postmaster", "googlemail.com", "google.com"))), None)
        if not recip or recip == me or recip in known_b:
            continue
        if recip not in ours:            # another campaign's bounce on the shared mailbox
            continue
        code, detail = _classify(body)
        btype = "hard" if code.startswith("5") else "soft"
        _append(BOUNCES, ["email", "type", "date", "detail"],
                {"email": recip, "type": btype, "date": today, "detail": detail})
        known_b.add(recip); nb += 1
        if btype == "hard":
            _suppress(recip, "hard_bounce", supp)

    # --- reply opt-outs ---
    for m in svc.users().messages().list(
            userId="me", q=f"newer_than:{days}d in:inbox", maxResults=200).execute().get("messages", []):
        full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        frm = _headers(full).get("from", "").lower()
        if any(h in frm for h in DAEMON_HINTS):
            continue
        text = (full.get("snippet", "") + " " + _decode(full.get("payload", {})))[:4000]
        if not OPTOUT_RE.search(text):
            continue
        addr = ADDR_RE.search(frm)
        replier = _norm(addr.group(0)) if addr else None
        if replier and replier != me and replier not in supp:
            if replier not in ours:   # not someone we mailed -> not our opt-out to record
                continue
            _suppress(replier, "reply_optout", supp); no += 1

    print(f"[bounce] {nb} new bounces, {no} reply opt-outs recorded.")
    return nb + no

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=2)
    a = ap.parse_args(); scan(a.days)
