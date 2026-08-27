#!/usr/bin/env python3
"""verify_queue must never turn a vendor failure into a permanent decision.

The drafting error this pins: an earlier design routed unrecognised verdicts to the non-sending
branch. Bouncer's adapter returns 'error' on HTTP 402/401, so credit exhaustion mid-run would have
written status=skipped across the batch -- and existing_state() reads every row regardless of
status, so those companies leave the addressable universe permanently. Turning the feature off
would not undo it.

Also pins that only 'undeliverable' is terminal. risky / unknown / deliverable-on-accept-all are
recorded and stay sendable, so the harder rules get set from real bounce outcomes later.

No network: the Bouncer client is stubbed. Run: python3 pipeline/test_verify_queue.py
"""
import csv, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import apollo_pull as ap
import verify_queue as vq
import verify_bakeoff as vb

PASS = 0; FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1
    else: FAIL += 1; print(f"FAIL: {name}{(' — ' + detail) if detail else ''}")

FIELDS = ap.COLS

def _wl(path, specs):
    rows = []
    for spec in specs:
        email, status, esp, verify = spec[:4]
        reason = spec[4] if len(spec) > 4 else ("done" if verify else "")
        r = {c: "" for c in FIELDS}
        r.update({"email": email, "status": status, "esp": esp, "verify_status": verify,
                  "verify_reason": reason, "first_name": "T", "company_domain": email.split("@")[1]})
        rows.append(r)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader(); w.writerows(rows)

def _read(path):
    return {r["email"]: r for r in csv.DictReader(open(path))}

def _stub(mapping):
    """Replace the Bouncer client. mapping: email -> (verdict, raw, detail)."""
    def fake(email, key):
        return mapping.get(email, ("unsure", "unknown", ""))
    vb.v_bouncer, vq.vb.v_bouncer = fake, fake


# ---------- detail parsing ----------
check("_parse_detail pulls accept_all", vq._parse_detail("low_deliverability acceptAll=yes provider=out") == (True, "low_deliverability"))
check("_parse_detail no accept_all", vq._parse_detail("low_quality acceptAll=no provider=google.com") == (False, "low_quality"))
check("_parse_detail empty", vq._parse_detail("") == (False, ""))


# ---------- classify ----------
check("deliverable -> deliverable", vq.classify("deliverable", False) == "deliverable")
check("deliverable + acceptAll -> recorded separately, not culled",
      vq.classify("deliverable", True) == "deliverable_acceptall")
check("undeliverable -> undeliverable", vq.classify("undeliverable", False) == "undeliverable")
check("risky -> risky", vq.classify("risky", False) == "risky")
check("a status nobody has seen before records as itself and is NOT terminal",
      vq.classify("brand_new_status", False) == "brand_new_status" != vq.TERMINAL)
check("empty status -> unknown, not terminal", vq.classify("", False) == "unknown")

# ---------- a vendor error writes nothing ----------
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "wl.csv")
    _wl(p, [("a@one.com", "", "google", ""), ("b@two.com", "", "google", "")])
    _stub({"a@one.com": ("error", "HTTP 402", "")})
    vq.sweep(apply=True, limit=0, esps=set(), worklist=p, key="k")
    got = _read(p)
    check("402 on the first row writes NO verdict", got["a@one.com"]["verify_status"] == "")
    check("402 does not mark status=skipped", got["a@one.com"]["status"] == "")
    check("the untouched row is also left alone", got["b@two.com"]["status"] == "")

# ---------- partial success is kept, the rest retried later ----------
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "wl.csv")
    _wl(p, [("a@one.com", "", "google", ""), ("b@two.com", "", "google", ""),
            ("c@three.com", "", "google", "")])
    _stub({"a@one.com": ("bad", "undeliverable", "acceptAll=no"),
           "b@two.com": ("error", "HTTP 402", "")})
    vq.sweep(apply=True, limit=0, esps=set(), worklist=p, key="k")
    got = _read(p)
    check("the verdict obtained before the error is kept",
          got["a@one.com"]["verify_status"] == "undeliverable")
    check("and it is marked skipped", got["a@one.com"]["status"] == "skipped")
    check("the row that errored stays unverified for next run", got["b@two.com"]["verify_status"] == "")
    check("rows after the error are untouched", got["c@three.com"]["verify_status"] == "")

# ---------- only undeliverable is terminal ----------
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "wl.csv")
    _wl(p, [("d@a.com", "", "google", ""), ("r@b.com", "", "google", ""),
            ("u@c.com", "", "google", ""), ("k@d.com", "", "google", ""),
            ("x@e.com", "", "google", "")])
    _stub({"d@a.com": ("good", "deliverable", "clean acceptAll=no provider=google.com"),
           "r@b.com": ("unsure", "risky", "risky acceptAll=yes provider=out"),
           "u@c.com": ("unsure", "unknown", "unknown acceptAll=no provider=?"),
           "k@d.com": ("good", "deliverable", "clean acceptAll=yes provider=out"),
           "x@e.com": ("bad", "undeliverable", "invalid_email acceptAll=no provider=google.com")})
    vq.sweep(apply=True, limit=0, esps=set(), worklist=p, key="k")
    got = _read(p)
    for email, want_v, want_sendable in [("d@a.com", "deliverable", True),
                                         ("r@b.com", "risky", True),
                                         ("u@c.com", "unknown", True),
                                         ("k@d.com", "deliverable_acceptall", True),
                                         ("x@e.com", "undeliverable", False)]:
        check(f"{want_v}: verdict recorded", got[email]["verify_status"] == want_v,
              got[email]["verify_status"])
        check(f"{want_v}: {'stays sendable' if want_sendable else 'is culled'}",
              (got[email]["status"] == "") is want_sendable, got[email]["status"])
    check("the cull carries a dated note", "verify:undeliverable" in got["x@e.com"]["notes"])
    check("the risky sub-reason is persisted", got["r@b.com"]["verify_reason"] == "risky", got["r@b.com"]["verify_reason"])
    check("a clean row's reason is persisted too", got["d@a.com"]["verify_reason"] == "clean")

# ---------- re-verify: backfill a missing reason, re-check a transient unknown ----------
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "wl.csv")
    _wl(p, [("has@a.com", "", "google", "deliverable", "clean"),      # verdict + reason -> settled
            ("bare@b.com", "", "google", "deliverable", ""),          # verdict, no reason -> backfill
            ("trans@c.com", "", "google", "unknown", "greylisted")])  # transient -> re-check
    seen = []
    def fake(email, key):
        seen.append(email); return ("good", "deliverable", "clean acceptAll=no provider=google.com")
    vb.v_bouncer = vq.vb.v_bouncer = fake
    vq.sweep(apply=True, limit=0, esps=set(), worklist=p, key="k")
    check("a settled verdict WITH a reason is not re-billed", "has@a.com" not in seen)
    check("a verdict missing its reason IS re-verified (backfill)", "bare@b.com" in seen)
    check("a transient 'unknown' IS re-verified", "trans@c.com" in seen)
    got = _read(p)
    check("backfill fills the reason", got["bare@b.com"]["verify_reason"] == "clean")
    check("re-checked unknown updates to its new verdict", got["trans@c.com"]["verify_status"] == "deliverable")

# ---------- eligibility ----------
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "wl.csv")
    _wl(p, [("sent@a.com", "sent", "google", ""), ("skip@b.com", "skipped", "google", ""),
            ("done@c.com", "", "google", "deliverable"), ("new@d.com", "", "google", "")])
    seen = []
    def fake(email, key):
        seen.append(email); return ("good", "deliverable", "acceptAll=no")
    vb.v_bouncer = vq.vb.v_bouncer = fake
    vq.sweep(apply=True, limit=0, esps=set(), worklist=p, key="k")
    check("an already-sent row is never verified", "sent@a.com" not in seen)
    check("an already-skipped row is never verified", "skip@b.com" not in seen)
    check("a row that already has a verdict is never re-billed", "done@c.com" not in seen)
    check("only the fresh blank row is verified", seen == ["new@d.com"], str(seen))

# ---------- esp filter ----------
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "wl.csv")
    _wl(p, [("g@a.com", "", "google", ""), ("o@b.com", "", "other", ""),
            ("bc@c.com", "", "barracuda", "")])
    seen = []
    def fake(email, key):
        seen.append(email); return ("good", "deliverable", "acceptAll=no")
    vb.v_bouncer = vq.vb.v_bouncer = fake
    vq.sweep(apply=True, limit=0, esps={"other", "barracuda"}, worklist=p, key="k")
    check("--esp verifies only the named strata", sorted(seen) == ["bc@c.com", "o@b.com"], str(seen))

# ---------- the cull-rate tripwire ----------
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "wl.csv")
    _wl(p, [(f"u{i}@x.com", "", "google", "") for i in range(25)])
    vb.v_bouncer = vq.vb.v_bouncer = lambda e, k: ("bad", "undeliverable", "")
    vq.sweep(apply=True, limit=0, esps=set(), worklist=p, key="k")
    got = _read(p)
    check("a 100% cull rate aborts and writes nothing (bad key / vendor incident, not a bad list)",
          all(r["status"] == "" and r["verify_status"] == "" for r in got.values()))

# ---------- operator host-cull: throw out named SEG strata without an API call ----------
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "wl.csv")
    _wl(p, [("mc1@a.com", "", "mimecast", ""), ("mc2@b.com", "", "mimecast", "risky", "risky"),
            ("bc@c.com", "", "barracuda", ""), ("g@d.com", "", "google", "")])
    seen = []
    def fake(email, key):
        seen.append(email); return ("good", "deliverable", "clean acceptAll=no provider=google.com")
    vb.v_bouncer = vq.vb.v_bouncer = fake
    vq.sweep(apply=True, limit=0, esps=set(), worklist=p, key="k", cull_esps={"mimecast", "barracuda"})
    got = _read(p)
    check("a mimecast row is culled", got["mc1@a.com"]["status"] == "skipped")
    check("a mimecast row already verified is still culled", got["mc2@b.com"]["status"] == "skipped")
    check("its prior Bouncer verdict is preserved", got["mc2@b.com"]["verify_status"] == "risky")
    check("a fresh cull gets the host_bounces marker", got["mc1@a.com"]["verify_status"] == "host_bounces")
    check("the cull note names the host", "host_bounces(mimecast)" in got["mc1@a.com"]["notes"])
    check("a barracuda row is culled", got["bc@c.com"]["status"] == "skipped")
    check("no API call is spent on culled hosts", "mc1@a.com" not in seen and "bc@c.com" not in seen)
    check("the google row is still verified normally", "g@d.com" in seen and got["g@d.com"]["status"] == "")

with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "wl.csv")
    _wl(p, [("g@d.com", "", "google", "")])
    vb.v_bouncer = vq.vb.v_bouncer = lambda e, k: ("good", "deliverable", "clean acceptAll=no provider=g")
    vq.sweep(apply=True, limit=0, esps=set(), worklist=p, key="k", cull_esps=set())
    check("empty cull list culls nothing", _read(p)["g@d.com"]["status"] == "")

# ---------- dry run is inert ----------
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "wl.csv")
    _wl(p, [("a@one.com", "", "google", "")])
    called = []
    vb.v_bouncer = vq.vb.v_bouncer = lambda e, k: (called.append(e), ("bad", "undeliverable", ""))[1]
    vq.sweep(apply=False, limit=0, esps=set(), worklist=p, key="k")
    check("dry-run makes no API calls", not called)
    check("dry-run writes nothing", _read(p)["a@one.com"]["verify_status"] == "")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
