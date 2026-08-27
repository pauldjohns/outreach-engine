#!/usr/bin/env python3
"""Cleanup sweep for the ceiling worklist: mark BLANK-status rows whose email is a role inbox /
noreply / syntactically bad / disposable / no-MX as status='skipped', so the sender never attempts
them and they can't trip the hard-bounce circuit breaker.

Added 2026-07-17 after the breaker HALTed on seo@ / agent@ (role addresses --scrape-sites pulls off
contact pages). Mirrors ceiling_poll.worklist_retire: touches ONLY blank-status rows (never the operator's
own tracking edits), atomic .tmp+replace. Default DRY-RUN; pass --apply to write.

  python3 pipeline/queue_cleanup.py            # dry-run: counts + samples, no write
  python3 pipeline/queue_cleanup.py --apply    # mark bad rows skipped (run when the chain is idle)
  python3 pipeline/queue_cleanup.py --no-mx    # skip DNS/MX pass (role/noreply/syntax only)
"""
import argparse, csv, os, sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from send_outreach import is_role, norm       # reuse the LIVE role filter (single source of truth)
import validate_emails                         # check(): syntax / disposable / MX

WORKLIST = os.path.join(ROOT, "outreach", "worklist_review.csv")


def bad_reason(email, do_mx=True):
    """Why this email should be skipped, or None to keep. Order: cheap/local checks before DNS."""
    e = norm(email)
    if not e:
        return None                            # no email -> not selectable anyway; leave blank
    if "noreply" in e or "no-reply" in e:
        return "noreply"                       # is_role misses github-style numeric noreply locals
    if is_role(e):
        return "role"
    if do_mx:
        ok, why = validate_emails.check(e)     # bad_syntax / disposable / no_mx / ok
        if not ok:
            return why
    return None


def sweep(apply, do_mx, worklist=WORKLIST):
    if not os.path.exists(worklist):
        print(f"no worklist at {worklist}"); return 1
    print(f"worklist: {worklist}")
    with open(worklist) as f:
        rd = csv.DictReader(f); rows = list(rd); fields = rd.fieldnames
    rundate = date.today().isoformat()
    from collections import Counter
    reasons = Counter(); samples = {}
    flagged = 0
    blank_before = sum(1 for r in rows if not (r.get("status") or "").strip())
    for r in rows:
        if (r.get("status") or "").strip():
            continue                           # only ever touch blank-status rows
        reason = bad_reason(r.get("email"), do_mx=do_mx)
        if not reason:
            continue
        reasons[reason] += 1; flagged += 1
        samples.setdefault(reason, []).append(f"{r.get('email')}  [{r.get('owner_repo')}]")
        if apply:
            r["status"] = "skipped"
            r["notes"] = (r.get("notes") or "") + f"auto-cleanup:{reason} {rundate}"
    print(f"worklist rows: {len(rows)} | blank-status queued (before): {blank_before}")
    print(f"flagged bad emails: {flagged}  ->  {dict(reasons)}")
    for reason, exs in samples.items():
        print(f"  [{reason}] e.g. " + " ; ".join(exs[:4]) + (f"  (+{len(exs)-4} more)" if len(exs) > 4 else ""))
    print(f"clean queue remaining after sweep: {blank_before - flagged}")
    if apply and flagged:
        tmp = worklist + ".tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
        os.replace(tmp, worklist)
        print(f"APPLIED: marked {flagged} rows status=skipped (atomic write).")
    elif not apply:
        print("DRY-RUN: no write. Re-run with --apply to mark these rows.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the marks (default: dry-run)")
    ap.add_argument("--no-mx", action="store_true", help="skip the DNS/MX pass (role/noreply/syntax only)")
    ap.add_argument("--worklist", default=WORKLIST, help="worklist CSV path (default: this checkout's)")
    a = ap.parse_args()
    return sweep(apply=a.apply, do_mx=not a.no_mx, worklist=a.worklist)


if __name__ == "__main__":
    sys.exit(main())
