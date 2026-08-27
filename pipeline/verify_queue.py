#!/usr/bin/env python3
"""Verify queued addresses against Bouncer before we ever mail them, and record the verdict.

  python3 pipeline/verify_queue.py                          # dry-run: what would be spent, no calls
  python3 pipeline/verify_queue.py --apply                  # verify + write
  python3 pipeline/verify_queue.py --apply --esp other,barracuda,proofpoint,mimecast,none
  python3 pipeline/verify_queue.py --apply --limit 50

An MX check proves a DOMAIN accepts mail; it says nothing about whether the MAILBOX exists.
validate_emails passed 457/457 addresses as SENDABLE and 11.1% of what it cleared hard-bounced.
This closes that gap: four of the six bounces on 2026-07-21 were mailbox-level failures a real
RCPT probe resolves, and a bake-off against those six showed Bouncer catching three of the four.

Only `undeliverable` is terminal. Everything else -- risky, unknown, deliverable-on-an-accept-all
domain -- is RECORDED and stays sendable, so a week of sends produces bounce rates split by verdict
class and the harder rules get set from this campaign's own outcomes instead of a 40-row sample.
See method/VERIFY-PLAN.md.

Deliberately its own step rather than part of apollo_pull: verification there would sit inside a
transaction that has already spent Apollo credits, so a mid-run 402 would either waste them or --
worse, and this was a real drafting error -- write status=skipped across the batch and lock those
companies out of the campaign forever via existing_state(). Here an API failure costs nothing: the
rows stay unverified and the next cycle picks them up.

Touches ONLY blank-status rows, never the operator's tracking edits. Atomic .tmp + replace. Dry-run
by default. Idempotent: a row that already carries a verify_status is never re-billed.
"""
import argparse, csv, json, os, sys
from collections import Counter
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from send_outreach import norm
import verify_bakeoff as vb                 # the exact adapter every calibration number came from

WORKLIST = os.path.join(ROOT, "outreach", "worklist_review.csv")
# A verdict this program is willing to act on terminally. Anything else is recorded only.
TERMINAL = "undeliverable"
# If more than this share of a batch comes back undeliverable, something is wrong with the key,
# the vendor, or the status vocabulary -- not with the list. Abort rather than cull the queue.
CULL_CEILING = 0.40
CULL_MIN_N = 20


def _parse_detail(detail):
    """v_bouncer packs 'reason acceptAll=X provider=Y' into one string. Pull (accept_all, reason)."""
    accept_all = "acceptAll=yes" in (detail or "")
    reason = (detail or "").split(" acceptAll=")[0].strip()
    return accept_all, reason


def classify(raw_status, accept_all):
    """Bouncer's verdict -> what we store. Unrecognised statuses record, never cull.

    accept_all is kept OUT of the terminal decision on purpose: Bouncer's own abstention ran 0% on
    Google while Apollo flags 46.8% of the same Google rows accept-all. Two free signals that
    disagree systematically, on 172 of the 400 queued rows. Recording both is how that gets
    settled; acting on either now would be guessing at scale.
    """
    st = (raw_status or "").strip().lower()
    if st == "deliverable":
        return "deliverable_acceptall" if accept_all else "deliverable"
    if st in ("undeliverable", "risky", "unknown"):
        return st
    return st or "unknown"                  # a new vendor status records as itself, never culls


def _esp_culled(r, cull_esps):
    """A blank-status row on an operator-flagged bounce-prone host. Culled WITHOUT an API call.

    2026-07-21: Mimecast and Barracuda bounced heavily in this campaign's own data.
    Both are secure email gateways that quarantine silently and return no NDR, so the adaptive
    throttle can never see them fail -- pre-send is the only place they can be caught. Bouncer
    only ever abstained on them (risky/unknown/accept-all), so this is operator ground truth, not
    a verifier verdict. Reversible: drop the host from verify_cull_esps to send them again.
    """
    return (not (r.get("status") or "").strip()
            and norm(r.get("email"))
            and (r.get("esp") or "") in cull_esps)


def _needs_verify(r, esps, cull_esps=()):
    """Eligible for a (re)verify this run. Blank status only, never the operator's tracking edits.

    Re-verify is allowed in two cases beyond a never-seen row: a row whose verdict is 'unknown'
    (Bouncer's transient/greylist class -- freezing it as final on one timeout is a bug), and a row
    that carries a status but no verify_reason (verified before the reason column existed; the
    reason is worth one backfill so step 6 can read it). A settled deliverable/undeliverable/risky
    row with a reason on file is never re-billed.
    """
    if (r.get("status") or "").strip():
        return False
    if not norm(r.get("email")):
        return False
    if (r.get("esp") or "") in cull_esps:
        return False                         # culled by host rule; do not spend a credit on it
    if esps and (r.get("esp") or "") not in esps:
        return False
    vs = (r.get("verify_status") or "").strip()
    if not vs:
        return True
    if vs == "unknown":
        return True
    if not (r.get("verify_reason") or "").strip():
        return True                          # verdict on file but reason was never captured
    return False


def verify_rows(rows, key, limit, esps, cull_esps=(), log=print):
    """Call Bouncer for eligible rows. Returns (results, errors). Never raises on vendor failure."""
    todo = [r for r in rows if _needs_verify(r, esps, cull_esps)]
    if limit:
        todo = todo[:limit]
    results, errors = {}, 0
    for r in todo:
        e = norm(r["email"])
        verdict, raw, detail = vb.v_bouncer(e, key)
        if verdict == "error":
            # A 402/401/timeout must never be mistaken for a verdict about the mailbox. Stop:
            # continuing would spend the rest of the run re-learning the same failure.
            errors += 1
            log(f"[verify] vendor error on {e}: {raw} — stopping, {len(results)} verified so far")
            break
        accept_all, reason = _parse_detail(detail)
        results[e] = (classify(raw, accept_all), reason)
    return results, errors


def _load_cull_esps(worklist):
    """Operator host-cull list from config.json, next to the worklist. Empty if unset."""
    cfg = os.path.join(os.path.dirname(worklist), "send", "config.json")
    try:
        return {str(e).strip().lower() for e in json.load(open(cfg)).get("verify_cull_esps", []) if str(e).strip()}
    except (OSError, ValueError):
        return set()


def sweep(apply, limit, esps, worklist=WORKLIST, key=None, cull_esps=None):
    if not os.path.exists(worklist):
        print(f"no worklist at {worklist}"); return 1
    key = key if key is not None else vb.load_key("BOUNCER_API_KEY")
    cull_esps = _load_cull_esps(worklist) if cull_esps is None else set(cull_esps)
    with open(worklist, newline="") as f:
        rd = csv.DictReader(f); rows = list(rd); fields = list(rd.fieldnames or [])
    for col in ("verify_status", "verify_reason", "verify_date"):
        if col not in fields:
            print(f"[verify] worklist has no {col} column — run apollo_pull once to migrate"); return 1

    culled = [r for r in rows if _esp_culled(r, cull_esps)]
    eligible = [r for r in rows if _needs_verify(r, esps, cull_esps)]
    planned = eligible[:limit] if limit else eligible
    by_esp = Counter((r.get("esp") or "?") for r in planned)
    backfill = sum(1 for r in planned if (r.get("verify_status") or "").strip())
    print(f"worklist: {worklist}")
    if cull_esps:
        print(f"host-cull ({', '.join(sorted(cull_esps))}): {len(culled)} row(s) skipped without an API call")
    print(f"eligible (blank status, needs a verdict or reason{', esp filter' if esps else ''}): {len(eligible)}")
    print(f"would verify: {len(planned)}  ~${len(planned) * 0.008:.2f} at the 1k tier"
          + (f"  (incl. {backfill} re-verify: unknown or missing reason)" if backfill else ""))
    print(f"  by esp: {dict(by_esp)}")

    if not apply:
        print("DRY-RUN: no API calls, no write. Re-run with --apply.")
        return 0
    if not planned and not culled:
        print("nothing to do."); return 0
    if planned and not key:
        print("[verify] no BOUNCER_API_KEY — nothing done."); return 1

    results, errors = verify_rows(rows, key, limit, esps, cull_esps) if planned else ({}, 0)

    counts = Counter(v for v, _ in results.values())
    culls = counts.get(TERMINAL, 0)
    if len(results) >= CULL_MIN_N and culls / len(results) > CULL_CEILING:
        print(f"[verify] ABORT: {culls}/{len(results)} came back {TERMINAL} "
              f"({culls/len(results):.0%} > {CULL_CEILING:.0%} ceiling). That is a bad key, a vendor "
              f"incident or a changed status vocabulary, not a bad list. Nothing written.")
        return 1

    rundate = date.today().isoformat()
    marked = host_marked = 0
    for r in rows:
        # operator host-cull: skip without a verdict, note the host and why
        if _esp_culled(r, cull_esps):
            r["status"] = "skipped"
            if not (r.get("verify_status") or "").strip():
                r["verify_status"] = "host_bounces"
            sep = "; " if (r.get("notes") or "").strip() else ""
            r["notes"] = (r.get("notes") or "") + f"{sep}verify:host_bounces({r.get('esp')}) {rundate}"
            host_marked += 1
            continue
        got = results.get(norm(r.get("email")))
        if not got:
            continue
        v, reason = got
        r["verify_status"] = v
        r["verify_reason"] = reason
        r["verify_date"] = rundate
        if v == TERMINAL:
            r["status"] = "skipped"
            sep = "; " if (r.get("notes") or "").strip() else ""
            r["notes"] = (r.get("notes") or "") + f"{sep}verify:{v} {rundate}"
            marked += 1

    if not results and not host_marked:
        print("[verify] no verdicts returned — nothing written."); return 1

    tmp = worklist + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    os.replace(tmp, worklist)

    print(f"\nverified {len(results)}: {dict(counts)}")
    print(f"marked skipped: {marked} undeliverable + {host_marked} host-cull "
          f"(only '{TERMINAL}' and the host-cull list are terminal; risky/unknown stay sendable)")
    if errors:
        print(f"stopped early on a vendor error — {len(eligible) - len(results)} rows still unverified, "
              f"they will be picked up next run")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="make the calls and write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="verify at most N rows this run")
    ap.add_argument("--esp", default="", help="comma-separated esp filter, e.g. other,barracuda")
    ap.add_argument("--worklist", default=WORKLIST)
    a = ap.parse_args()
    esps = {s.strip() for s in a.esp.split(",") if s.strip()}
    return sweep(apply=a.apply, limit=a.limit, esps=esps, worklist=a.worklist)


if __name__ == "__main__":
    sys.exit(main())
