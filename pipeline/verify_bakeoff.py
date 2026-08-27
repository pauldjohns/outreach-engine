#!/usr/bin/env python3
"""Score email-verification vendors against OUR bounces, not their benchmark.

  python3 pipeline/verify_bakeoff.py                    # dry run: shows the panel, calls nothing
  python3 pipeline/verify_bakeoff.py --live             # calls the APIs (needs keys)
  python3 pipeline/verify_bakeoff.py --live --vendor bouncer --controls 20

SENDS NO MAIL. Read-only against the send log; the only writes are the report.

Why this exists. Every published verifier benchmark is either run by a vendor that wins it, or
scored on a convention that hides the failure we care about (counting "unknown" as "not attempted"
rather than "wrong"). We have something better: six addresses that provably bounced, with the SMTP
code each one returned. That is ground truth nobody can spin, and four of the six are the kind a
real RCPT probe should catch:

  5.1.10 RecipientNotFound          example-one.com          definitive rejection
  5.4.1  Access denied              example-two.de         M365 DBEB rejecting an unknown recipient
  5.4.1  Access denied              example-three.com  same
  5.2.1  account inactive           example-four.co      Google, mailbox disabled

DBEB is on by default for any tenant whose recipients all live in Exchange Online, and Microsoft
documents that exact NDR string, so those two are NOT catch-all tenants -- they reject at the edge.
A vendor that misses them is not doing an SMTP probe and cannot help us.

The other two are in the panel as SHOULD-NOT-CATCH. They are not bad addresses:

  5.4.14 hop count exceeded         example-five.com      a mail loop on the RECIPIENT's side
  5.7.23 SPF violation              example-six.com       -all plus forwarding without SRS

A vendor that condemns those is over-condemning, which costs ICP. We check both directions.

Negative controls are addresses we sent to that returned no NDR. Read that honestly: it is weaker
evidence than a bounce. M365 and Workspace quarantine silently (see README), so "no NDR" means
"did not hard bounce", not "good mailbox". A vendor marking some of them risky is not necessarily
wrong -- which is why they are reported, not scored pass/fail.

PII: every address in the panel is a real third party's work email, and running --live discloses
them to the vendor. That is why the default is a dry run and why --controls is capped.

Keys, mode 600, outside the repo (same shape as apollo.env):
  ~/.config/<campaign>/verify.env
      BOUNCER_API_KEY=...
      ZEROBOUNCE_API_KEY=...
"""
import argparse, csv, json, os, sys, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
SEND = os.path.join(ROOT, "outreach", "send")
SEND_LOG = os.path.join(SEND, "send_log.csv")
BOUNCES = os.path.join(SEND, "bounces.csv")
ENV = os.path.expanduser("~/.config/<campaign>/verify.env")
REPORT = os.path.join(SEND, "verify_bakeoff.json")

# The verdict we expect on each known bounce, and why. The six recipients below were replaced
# with placeholders when this repo was published: the SMTP codes are the content, the addresses
# belonged to real people. Swap in your own bounces before running this.
# The verdict we expect on each known bounce, and why. "catch" means a competent RCPT probe should
# call it bad; "pass" means the address is fine and condemning it is a false positive on our side.
EXPECTED = {
    "alice@example-one.com":                    ("catch", "5.1.10 RecipientNotFound"),
    "bruno@example-two.de":          ("catch", "5.4.1 M365 DBEB, unknown recipient"),
    "carla@example-three.com": ("catch", "5.4.1 M365 DBEB, unknown recipient"),
    "dana@example-four.co":           ("catch", "5.2.1 Google account inactive"),
    "erik@example-five.com":               ("pass",  "5.4.14 hop count - recipient's mail loop"),
    "farid@example-six.com":             ("pass",  "5.7.23 SPF on a forwarded hop"),
}


def load_key(name):
    k = os.environ.get(name, "").strip()
    if k:
        return k
    if os.path.exists(ENV):
        for line in open(ENV):
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return ""

def _get(url, headers=None, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 ** attempt * 2); continue
            # never surface the URL: ZeroBounce puts the API key in the query string
            return {"_error": f"HTTP {e.code}"}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt < tries - 1:
                time.sleep(2 ** attempt * 2); continue
            return {"_error": type(e).__name__}
    return {"_error": "exhausted"}


# ---------- vendors ----------
# Each returns (normalised_verdict, raw_status, detail). Normalised vocabulary:
#   bad     -> vendor says do not send
#   good    -> vendor says safe to send
#   unsure  -> catch-all / unknown / risky: the vendor is abstaining
# Abstention is a THIRD outcome on purpose. Collapsing it into good is the failure mode that makes
# a verifier useless here, and collapsing it into bad is what silently deletes half the ICP.

def v_bouncer(email, key):
    r = _get("https://api.usebouncer.com/v1.1/email/verify?"
             + urllib.parse.urlencode({"email": email, "timeout": 30}),
             {"x-api-key": key})
    if "_error" in r:
        return "error", r["_error"], ""
    st = (r.get("status") or "").lower()
    detail = " ".join(x for x in [r.get("reason") or "",
                                  f"acceptAll={(r.get('domain') or {}).get('acceptAll')}",
                                  f"provider={r.get('provider') or '?'}"] if x)
    return ({"undeliverable": "bad", "deliverable": "good"}.get(st, "unsure"), st, detail)

def v_zerobounce(email, key):
    # NOTE: ZeroBounce takes the key as a query parameter. Their design, not ours -- keep it out of
    # logs and out of the report file.
    r = _get("https://api.zerobounce.net/v2/validate?"
             + urllib.parse.urlencode({"api_key": key, "email": email, "ip_address": ""}))
    if "_error" in r:
        return "error", r["_error"], ""
    st = (r.get("status") or "").lower(); sub = (r.get("sub_status") or "").lower()
    # ZeroBounce returns accept_all UNDER status=valid. Treating that as good is precisely the
    # false positive we are testing for, so it is demoted here rather than trusted.
    if sub in ("accept_all", "role_based_catch_all", "role_based_accept_all"):
        return "unsure", f"{st}/{sub}", "accept_all demoted from valid"
    return ({"invalid": "bad", "spamtrap": "bad", "do_not_mail": "bad",
             "abuse": "bad", "valid": "good"}.get(st, "unsure"),
            f"{st}/{sub}" if sub else st, r.get("mx_record") or "")

VENDORS = {"bouncer": ("BOUNCER_API_KEY", v_bouncer),
           "zerobounce": ("ZEROBOUNCE_API_KEY", v_zerobounce)}


def panel(n_controls):
    """Known bounces (labelled) + addresses we sent to that never returned an NDR (unlabelled)."""
    bounced = {r["email"].strip().lower() for r in csv.DictReader(open(BOUNCES)) if r.get("email")}
    sent, seen = [], set()
    for r in csv.DictReader(open(SEND_LOG)):
        e = (r.get("to") or "").strip().lower()
        if r.get("mode") == "live" and e and e not in seen:
            seen.add(e); sent.append((e, r.get("esp") or "?"))
    known = [(e, esp) for e, esp in sent if e in bounced]
    for e in EXPECTED:                                   # a bounce with no send row still counts
        if e not in {x for x, _ in known}:
            known.append((e, "?"))
    controls = [(e, esp) for e, esp in sent if e not in bounced][:n_controls]
    return known, controls


def queue_sample(per_stratum, esps=("microsoft365", "google")):
    """Stratified sample of the UNSENT queue: equal counts per mail host.

    The bounce panel answers "does the vendor catch bad addresses". This answers the question that
    actually decides affordability: how often does it ABSTAIN, on a mix that matches what we are
    about to send? The controls in the bounce panel were 76% Google, and Google is the easy case.

    Deterministic sample (fixed seed, sorted input) so a re-run scores the same addresses and two
    vendors can be compared on identical inputs.
    """
    import random
    rows = [r for r in csv.DictReader(open(os.path.join(ROOT, "outreach", "worklist_review.csv")))
            if not (r.get("status") or "").strip() and (r.get("email") or "").strip()]
    out = []
    for esp in esps:
        pool = sorted({(r["email"].strip().lower(), esp) for r in rows if (r.get("esp") or "") == esp})
        out += random.Random(20260721).sample(pool, min(per_stratum, len(pool)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually call the vendor APIs")
    ap.add_argument("--queue-sample", type=int, metavar="N",
                    help="instead of the bounce panel, score N unsent rows PER mail host "
                         "(microsoft365 and google) to measure the abstention rate")
    ap.add_argument("--vendor", action="append", choices=sorted(VENDORS),
                    help="repeatable; default is every vendor with a key on file")
    ap.add_argument("--controls", type=int, default=25,
                    help="how many non-bounced sent addresses to include (default 25)")
    ap.add_argument("--zb-prospects-ok", action="store_true",
                    help="confirm Verify+ is OFF before letting ZeroBounce see prospect addresses")
    a = ap.parse_args()

    if a.queue_sample:
        known, controls = [], queue_sample(a.queue_sample)
    else:
        known, controls = panel(max(0, a.controls))
    chosen = a.vendor or sorted(VENDORS)
    keys = {v: load_key(VENDORS[v][0]) for v in chosen}

    # Interlock. ZeroBounce's Verify+ falls back to SENDING a real email to anything it cannot
    # resolve, from its own throwaway domains. Harmless against addresses we own; not harmless
    # against prospects who have never heard of us. Require an explicit opt-in for that combination.
    if "zerobounce" in chosen and (a.queue_sample or controls or known) and not a.zb_prospects_ok:
        sys.exit("[bakeoff] REFUSING: this panel contains third-party prospect addresses and "
                 "ZeroBounce's Verify+ can fall back to emailing what it cannot resolve.\n"
                 "          Turn Verify+ OFF in the ZeroBounce account, then pass "
                 "--zb-prospects-ok. Or run --vendor bouncer, which has no send path.")

    print(f"panel: {len(known)} known bounces (labelled) + {len(controls)} sent-no-NDR controls")
    for e, esp in known:
        exp, why = EXPECTED.get(e, ("?", "not in the labelled set"))
        print(f"   {'SHOULD CATCH' if exp == 'catch' else 'should PASS  ' if exp == 'pass' else '?':<13} "
              f"{e:<36} {esp:<13} {why}")
    print()
    for v in chosen:
        print(f"   {v:<11} key {'present' if keys[v] else 'MISSING -> ' + ENV}")

    if not a.live:
        calls = len(chosen) * (len(known) + len(controls))
        print(f"\ndry run — nothing called. --live would make {calls} API call(s) and disclose "
              f"{len(known) + len(controls)} real contact addresses to "
              f"{len(chosen)} vendor(s).")
        return 0
    missing = [v for v in chosen if not keys[v]]
    if missing:
        sys.exit(f"\nno key for: {', '.join(missing)}. Put it in {ENV} (chmod 600).")

    # Merge into any existing report rather than replacing it: vendors are often run one at a time
    # (a rate limit, a missing key, or an interlock), and a clobbered file loses the comparison the
    # whole exercise exists to make.
    out = {"panel": {}, "vendors": {}}
    if os.path.exists(REPORT):
        try:
            out = json.load(open(REPORT))
            out.setdefault("vendors", {})
        except (json.JSONDecodeError, OSError):
            pass
    out["panel"] = {"known": len(known), "controls": len(controls)}
    for v in chosen:
        fn = VENDORS[v][1]; key = keys[v]
        res = {"known": {}, "controls": {}, "score": {}}
        print(f"\n=== {v} ===")
        hits = misses = overreach = 0
        for e, esp in known:
            verdict, raw, detail = fn(e, key)
            exp = EXPECTED.get(e, ("?", ""))[0]
            if exp == "catch":
                ok = verdict == "bad"; hits += ok; misses += not ok
            elif exp == "pass":
                ok = verdict != "bad"; overreach += not ok
            else:
                ok = None
            mark = "" if ok is None else ("  ok" if ok else "  MISS")
            print(f"  {e:<36} {verdict:<7} {raw:<22} {detail[:40]}{mark}")
            res["known"][e] = {"expected": exp, "verdict": verdict, "raw": raw, "detail": detail}
        cbad = cunsure = cgood = 0
        for e, esp in controls:
            verdict, raw, detail = fn(e, key)
            cbad += verdict == "bad"; cunsure += verdict == "unsure"; cgood += verdict == "good"
            res["controls"][e] = {"esp": esp, "verdict": verdict, "raw": raw}
        n_catch = sum(1 for e, _ in known if EXPECTED.get(e, ("?",))[0] == "catch")
        res["score"] = {"catchable_found": hits, "catchable_total": n_catch,
                        "missed": misses, "over_condemned_good": overreach,
                        "controls_bad": cbad, "controls_unsure": cunsure, "controls_good": cgood}
        print(f"  -- caught {hits}/{n_catch} catchable · over-condemned {overreach}/2 not-bad · "
              f"controls: {cgood} good / {cunsure} unsure / {cbad} bad")
        out["vendors"][v] = res

    json.dump(out, open(REPORT, "w"), indent=1)          # keys never enter this file
    print(f"\nwrote {REPORT}")
    print("read it as: catchable_found is the number that matters. controls_unsure is the share of "
          "your queue a vendor will abstain on -- that is the ICP you would lose if you drop "
          "everything it cannot resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
