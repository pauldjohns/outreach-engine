#!/usr/bin/env python3
"""
validate_emails.py - cheap pre-send email validation (no paid API, no send).

Three checks, cheapest first, to cull addresses that would hard-bounce BEFORE we
send to them (dead domains are the #1 hard-bounce cause, and hard bounces are
what damage sender reputation):

  1. syntax      - RFC-ish regex; drops malformed.
  2. MX record   - DNS lookup via `dig`/`host` (macOS built-in, no pip dep). A domain
                   with no MX (and no A fallback) cannot receive mail -> drop.
  3. disposable  - static blocklist of throwaway domains.

Deliberately NOT done: live SMTP RCPT probing (mailbox-level existence). It's slow,
unreliable, and can hurt our own sending IP. Mailbox-level misses are caught
reactively by the sender's circuit breaker instead. This cheap tier removes most
hard bounces for ~zero cost.

  python3 pipeline/validate_emails.py                          # report on the ceiling worklist
  python3 pipeline/validate_emails.py --write                  # also add email_valid/email_check columns
  python3 pipeline/validate_emails.py --worklist path.csv
Result columns (with --write): email_valid (true/false), email_check (ok|bad_syntax|no_mx|disposable|no_email)
"""
import argparse, csv, os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
DEFAULT_WL = os.path.join(ROOT, "outreach", "worklist_review.csv")
SYNTAX = re.compile(r"^[a-zA-Z0-9._%+\-]+@([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})$")
DISPOSABLE = {"mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org",
              "throwaway.email", "yopmail.com", "trashmail.com", "getnada.com", "sharklasers.com",
              "maildrop.cc", "fakeinbox.com", "dispostable.com"}
_mx_cache = {}

def _parse_mx(mx_out):
    """(has_real_mx, is_null_mx) from `dig +short mx` output.

    RFC 7505: a lone "0 ." is a NULL MX — the domain states it accepts no mail at all. That is
    authoritative, so a null MX must not fall through to the A-record check. Typo'd freemail
    domains are the live case: gmail.cm publishes a null MX *and* an A record, so testing the
    dig output for truthiness read it as deliverable and user12345@gmail.cm was sent to
    on 07-18. It hard-bounced and helped trip the breaker at 7/100."""
    real, null = [], False
    for line in (mx_out or "").splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        exchange = parts[-1]
        if exchange == ".":
            null = True
        else:
            real.append(exchange)
    return bool(real), (null and not real)


def has_mx(domain):
    """True if the domain has an MX record (or an A record as mail fallback). Cached per domain."""
    if domain in _mx_cache:
        return _mx_cache[domain]
    ok = False
    try:
        out = subprocess.run(["dig", "+short", "mx", domain], capture_output=True, text=True, timeout=8).stdout.strip()
        real_mx, null_mx = _parse_mx(out)
        if real_mx:
            ok = True
        elif null_mx:
            ok = False  # RFC 7505 — an explicit refusal; the A record does NOT override it
        else:  # no MX at all -> some domains still accept mail on the A record
            a = subprocess.run(["dig", "+short", "a", domain], capture_output=True, text=True, timeout=8).stdout.strip()
            ok = bool(a)
    except Exception:
        ok = False  # DNS failure -> treat as unverifiable; conservative = invalid
    _mx_cache[domain] = ok
    return ok

def check(email):
    e = (email or "").strip().lower()
    if not e:
        return False, "no_email"
    m = SYNTAX.match(e)
    if not m:
        return False, "bad_syntax"
    domain = m.group(1)
    if domain in DISPOSABLE:
        return False, "disposable"
    if not has_mx(domain):
        return False, "no_mx"
    return True, "ok"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worklist", default=DEFAULT_WL)
    ap.add_argument("--write", action="store_true", help="add email_valid/email_check columns in place")
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.worklist)))
    emailed = [r for r in rows if (r.get("email") or "").strip()]
    print(f"validating {len(emailed)} emails in {os.path.relpath(a.worklist, ROOT)} "
          f"({len(rows)-len(emailed)} rows have no email)", flush=True)

    def work(r):
        valid, reason = check(r.get("email"))
        r["email_valid"] = "true" if valid else "false"
        r["email_check"] = reason
        return r
    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(work, emailed))
    for r in rows:
        r.setdefault("email_valid", ""); r.setdefault("email_check", "no_email")

    from collections import Counter
    reasons = Counter(r["email_check"] for r in emailed)
    valid = sum(1 for r in emailed if r["email_valid"] == "true")
    print("\n=== validation result (emailed rows) ===")
    for k, v in reasons.most_common():
        print(f"  {k:12} {v:4}  ({100*v//max(1,len(emailed))}%)")
    bounce_risk = len(emailed) - valid
    print(f"  {'SENDABLE':12} {valid:4}  ({100*valid//max(1,len(emailed))}%)")
    print(f"  would-cull   {bounce_risk:4}  (culled before send -> not counted against your bounce rate)")

    if a.write:
        cols = list(rows[0].keys())
        for c in ("email_valid", "email_check"):
            if c not in cols: cols.append(c)
        with open(a.worklist, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
        print(f"\nwrote email_valid/email_check into {os.path.relpath(a.worklist, ROOT)}")
    else:
        print("\n(report only; re-run with --write to add the columns)")

if __name__ == "__main__":
    main()
