#!/usr/bin/env python3
"""Tests for the two rules specific to this campaign (the operator, 2026-07-20):

  1. ONE SEND PER COMPANY DOMAIN, ever. All three variants share a near-identical shape, so
     several copies landing in one corporate tenant is what trips a shared spam filter.
  2. first_name is the ONLY merge field. Never derived from the email local-part.

Plus the Apollo sourcing filters. Network-free. Run: python3 pipeline/test_review_rules.py
"""
import csv, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import send_outreach as so
import apollo_pull as A

PASS = 0; FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1
    else: FAIL += 1; print(f"FAIL: {name}")

CFG = {"segments": []}
def rows(*emails):
    return [{"email": e, "status": ""} for e in emails]

# ---------- one send per domain, within a run ----------
out = so.select(rows("a@acme.io", "b@acme.io", "c@other.dev"), CFG, set(), set(), 100, None)
check("two people at one domain -> one send", len(out) == 2)
check("the kept pair spans both domains",
      {r["email"].split("@")[1] for r in out} == {"acme.io", "other.dev"})
check("first row at the domain is the one kept", out[0]["email"] == "a@acme.io")

# ---------- one send per domain, ACROSS runs (send-log history) ----------
out = so.select(rows("new@acme.io", "fresh@other.dev"), CFG, set(), set(), 100, None,
                sent_domains={"acme.io"})
check("domain already in the send log is skipped", [r["email"] for r in out] == ["fresh@other.dev"])

# ---------- freemail is exempt, or the campaign ends on send one ----------
out = so.select(rows("x@gmail.com", "y@gmail.com", "z@gmail.com"), CFG, set(), set(), 100, None)
check("freemail is exempt from the domain cap", len(out) == 3)
out = so.select(rows("x@gmail.com"), CFG, set(), set(), 100, None, sent_domains={"gmail.com"})
check("freemail not blocked by send-log history", len(out) == 1)
check("gmail is in the freemail set", "gmail.com" in so.FREEMAIL)
check("a corporate domain is not", "acme.io" not in so.FREEMAIL)

# ---------- the rule can be switched off ----------
out = so.select(rows("a@acme.io", "b@acme.io"), {"segments": [], "one_per_domain": False},
                set(), set(), 100, None)
check("one_per_domain:false disables the rule", len(out) == 2)

# ---------- domain rule must not override the harder gates ----------
out = so.select(rows("a@acme.io"), CFG, {"a@acme.io"}, set(), 100, None)
check("suppression still wins", out == [])
out = so.select(rows("a@acme.io"), CFG, set(), {"a@acme.io"}, 100, None)
check("already-sent still wins", out == [])

# ---------- first_name is the only merge field ----------
check("name comes from the column", so.render("Hi {{first_name}},", {"first_name": "Jordan"}) == "Hi Jordan,")
check("blank name falls back to 'there'", so.render("Hi {{first_name}},", {"first_name": ""}) == "Hi there,")
check("missing column falls back to 'there'", so.render("Hi {{first_name}},", {}) == "Hi there,")
# the failure this guards: deriving from the local-part renders "Hi Jsmith"
check("name is NOT derived from the email local",
      so.render("Hi {{first_name}},", {"email": "jsmith@acme.io"}) == "Hi there,")
check("company is not a merge field", so.unknown_merge("{{company}}") == ["company"])
check("title is not a merge field", so.unknown_merge("{{title}}") == ["title"])
check("first_name is fillable", so.unknown_merge("{{first_name}}") == [])

# ---------- the live gate rejects a template with an unfillable field ----------
good = {"dry_run": False, "template_approved": True}
ok, why = so.live_ok(good, [("a", "s", "Hi {{first_name}}, " + "x" * 40)])
check("template using only first_name passes the gate", ok is True)
ok, why = so.live_ok(good, [("a", "s", "Hi {{first_name}} at {{company}}, " + "x" * 40)])
check("template using company is BLOCKED, not silently blanked", ok is False and "company" in why)

# ---------- Apollo sourcing filters ----------
check("US kept", "united states" in A.TARGET_COUNTRIES)
check("Germany kept", "germany" in A.TARGET_COUNTRIES)
check("India dropped", "india" not in A.TARGET_COUNTRIES)
check("Australia dropped (out of scope)", "australia" not in A.TARGET_COUNTRIES)

check("company name normalises for dedup", A._norm_company("Acme, Inc.") == A._norm_company("acme inc"))
check("different companies stay distinct", A._norm_company("Acme") != A._norm_company("Acmo"))

rec = {"city": "Austin", "state": "Texas", "country": "United States"}
check("location is city-first", A.person_location(rec) == "Austin, Texas, United States")
check("city resolves a real timezone, not the default",
      str(so.recipient_tz({"owner_location": A.person_location(rec)}, {})) == "America/Chicago")
check("London resolves correctly",
      str(so.recipient_tz({"owner_location": A.person_location(
          {"city": "London", "state": "", "country": "United Kingdom"})}, {})) == "Europe/London")
check("blank parts omitted", A.person_location({"city": "", "state": "", "country": "Ireland"}) == "Ireland")

V = ["REV_A_staging", "REV_B_bugreports", "REV_C_repro"]
check("variant deterministic", A.assign_variant("a@b.com", V) == A.assign_variant("a@b.com", V))
spread = {}
for i in range(600):
    v = A.assign_variant(f"u{i}@c{i}.dev", V); spread[v] = spread.get(v, 0) + 1
check("all three variants represented", set(spread) == set(V))
check("variants spread evenly (none starved)", all(120 < n < 280 for n in spread.values()))

# ---------- a sourced row renders and logs correctly end to end ----------
row = A.to_row({"first_name": "Jordan", "last_name": "Reyes", "title": "Backend Engineer",
                "email": "jordan@northwind.dev", "city": "Denver", "state": "Colorado",
                "country": "United States", "id": "x1",
                "organization": {"name": "Northwind", "primary_domain": "northwind.dev",
                                 "estimated_num_employees": 30}}, V, "2026-07-20")
check("row carries the domain for the throttle", row["company_domain"] == "northwind.dev")
check("row carries first_name for the merge", row["first_name"] == "Jordan")
check("row has empty tracking columns", row["status"] == "" and row["notes"] == "")
check("row renders cleanly", so.render("Hi {{first_name}},", row) == "Hi Jordan,")
check("row location drives tz", str(so.recipient_tz(row, {})) == "America/Denver")

# ---------- worklist writeback keys on email, adds absent tracking columns ----------
with tempfile.TemporaryDirectory() as td:
    wl = os.path.join(td, "wl.csv")
    with open(wl, "w", newline="") as f:                      # no status columns on purpose
        w = csv.DictWriter(f, fieldnames=["email", "first_name"]); w.writeheader()
        w.writerow({"email": "jordan@northwind.dev", "first_name": "Jordan"})
    so._mark_sent(wl, "jordan@northwind.dev", "2026-07-20")
    got = so.read_csv(wl)[0]
    check("status written despite absent column", got.get("status") == "sent")
    check("contacted_on written", got.get("contacted_on") == "2026-07-20")
    check("payload preserved", got.get("first_name") == "Jordan")


# ---------- timezone resolution from Apollo's structured location ----------
# Regression: only 11 state ABBREVIATIONS were mapped and no full names, so every North Carolina
# and Florida recipient fell through to the US country default (America/Chicago) and would have
# been mailed an hour early. Observed on a live pull, 2026-07-20.
def tz(loc):
    return str(so.recipient_tz({"owner_location": loc}, {}))

check("North Carolina resolves Eastern", tz("High Point, North Carolina, United States") == "America/New_York")
check("Florida resolves Eastern", tz("Tampa, Florida, United States") == "America/New_York")
check("Massachusetts resolves Eastern", tz("Boston, Massachusetts, United States") == "America/New_York")
check("Texas resolves Central", tz("Dallas, Texas, United States") == "America/Chicago")
check("Colorado resolves Mountain", tz("Denver, Colorado, United States") == "America/Denver")
check("Arizona resolves Phoenix (no DST)", tz("Tempe, Arizona, United States") == "America/Phoenix")
check("California resolves Pacific", tz("San Jose, California, United States") == "America/Los_Angeles")
check("Washington state resolves Pacific", tz("Tacoma, Washington, United States") == "America/Los_Angeles")
check("DC resolves Eastern, not Washington state",
      tz("Washington, District of Columbia, United States") == "America/New_York")
# the city list alone got this wrong: 'portland' matched Oregon's zone regardless of state
check("Portland OREGON is Pacific", tz("Portland, Oregon, United States") == "America/Los_Angeles")
check("Portland MAINE is Eastern", tz("Portland, Maine, United States") == "America/New_York")
check("West Virginia not confused with Virginia",
      tz("Morgantown, West Virginia, United States") == "America/New_York")

check("Ontario resolves Toronto", tz("Mississauga, Ontario, Canada") == "America/Toronto")
check("British Columbia resolves Vancouver", tz("Victoria, British Columbia, Canada") == "America/Vancouver")
check("Alberta resolves Edmonton", tz("Calgary, Alberta, Canada") == "America/Edmonton")
check("Saskatchewan resolves Regina", tz("Regina, Saskatchewan, Canada") == "America/Regina")
# Ontario, CALIFORNIA must not resolve to the Canadian province
check("Ontario California is Pacific", tz("Ontario, California, United States") == "America/Los_Angeles")

# non-US strings must be untouched by the state map
check("Italy still resolves Rome", tz("Florence, Tuscany, Italy") == "Europe/Rome")
check("Germany still resolves Berlin", tz("Munich, Bavaria, Germany") == "Europe/Berlin")
check("UK still resolves London", tz("Manchester, England, United Kingdom") == "Europe/London")
check("country-only US falls back to the default", tz("United States") in ("America/Chicago", "America/New_York"))
check("country-only Canada resolves Toronto", tz("Canada") == "America/Toronto")
check("empty location does not raise", tz("") != "")


# ---------- follow-the-sun: per-timezone quotas ----------
# even split when supply is plentiful: 2 EU zones + 4 NA zones, cap 100, eu_share .5
sup = {"UTC+01":200,"UTC+02":200,"UTC-05":200,"UTC-06":200,"UTC-07":200,"UTC-08":200}
a = so.allocate(100, sup, 0.5)
check("total equals cap", sum(a.values())==100)
eu = sum(v for k,v in a.items() if so._off(k)>=0); na = sum(v for k,v in a.items() if so._off(k)<0)
check("EU gets half", eu==50)
check("NA gets half", na==50)
check("2 EU zones split evenly (25 each)", a["UTC+01"]==25 and a["UTC+02"]==25)
check("4 NA zones split evenly (12-13 each)", all(12<=a[k]<=13 for k in ("UTC-05","UTC-06","UTC-07","UTC-08")))

# a zone short on supply must not lose the day volume
a = so.allocate(100, {"UTC+01":5,"UTC+02":200,"UTC-05":200,"UTC-06":200,"UTC-07":200,"UTC-08":200}, 0.5)
check("still sends the full cap when one zone is thin", sum(a.values())==100)
check("thin zone capped by its own supply", a["UTC+01"]==5)
check("thin zone's slack stays in EU first", sum(v for k,v in a.items() if so._off(k)>=0)==50)

# whole region missing -> the other takes the lot
a = so.allocate(100, {"UTC-05":200,"UTC-06":200}, 0.5)
check("no EU supply -> NA takes the full cap", sum(a.values())==100)
a = so.allocate(100, {"UTC+01":200,"UTC+02":200}, 0.5)
check("no NA supply -> EU takes the full cap", sum(a.values())==100)

# never exceed supply, never exceed cap
a = so.allocate(100, {"UTC+01":3,"UTC-05":4}, 0.5)
check("total supply below cap -> send everything", sum(a.values())==7)
check("no bucket exceeds its supply", a["UTC+01"]==3 and a["UTC-05"]==4)
check("empty supply is safe", so.allocate(100, {}, 0.5)=={})
check("zero cap is safe", so.allocate(0, sup, 0.5)=={})

# eu_share is honoured
a = so.allocate(100, sup, 0.7)
check("eu_share 0.7 shifts the split", sum(v for k,v in a.items() if so._off(k)>=0)==70)

# select() must respect the room it is given
rows=[{"email":f"u{i}@c{i}.dev","status":"","owner_location":"Boston, Massachusetts, United States"} for i in range(10)]
out = so.select(rows, {"segments":[]}, set(), set(), 100, None, bucket_room={"UTC-04":3,"UTC-05":3})
check("select stops at the zone's room", len(out)==3)
out = so.select(rows, {"segments":[]}, set(), set(), 100, None, bucket_room={})
check("no room for the zone -> nothing selected", out==[])
out = so.select(rows, {"segments":[]}, set(), set(), 100, None)
check("no quota passed -> unrestricted", len(out)==10)


# ---------- adaptive throttle (replaces the binary breaker) ----------
# The breaker fired on 3/28 = 11%, halted everything, and taught us nothing: that sample has a
# 95% CI of roughly 2-28%. Bounce rate should scale VOLUME, because reputation harm scales with
# volume. These tests pin the tiers and, more importantly, the refusal to act on a small sample.
import tempfile as _tf, csv as _csv, os as _os

def _mk(nsent, nbounce, cfg=None):
    """Build a temp send_log/bounces pair and return the throttle verdict."""
    td = _tf.mkdtemp()
    log = _os.path.join(td, "s.csv"); bnc = _os.path.join(td, "b.csv")
    with open(log, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=["ts", "to", "mode"]); w.writeheader()
        for i in range(nsent):
            w.writerow({"ts": "2026-07-21T09:00:00", "to": f"u{i}@d{i}.com", "mode": "live"})
    with open(bnc, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=["email", "type", "date"]); w.writeheader()
        for i in range(nbounce):
            w.writerow({"email": f"u{i}@d{i}.com", "type": "hard", "date": "2026-07-21"})
    old = so.BOUNCES; so.BOUNCES = bnc
    live = [r for r in so.read_csv(log) if r["mode"] == "live"]
    out = so.throttle_factor(cfg or {}, live)
    so.BOUNCES = old
    return out

check("clean list runs at full speed", _mk(100, 0)[0] == 1.0)
check("2% bounce is still full speed", _mk(100, 2)[0] == 1.0)
check("5% bounce -> three-quarter", _mk(100, 5)[0] == 0.75)
check("8% bounce -> half", _mk(100, 8)[0] == 0.5)
check("15% bounce -> quarter", _mk(100, 15)[0] == 0.25)
check("25% bounce -> stop", _mk(100, 25)[0] == 0.0)

# the whole point: a small denominator must NOT trigger action
f, note = _mk(28, 3)          # the exact sample that halted the campaign
check("the 3/28 that halted the campaign now runs at full speed", f == 1.0)
check("and says why", "too small" in note)
check("39 sends is still too small to act on", _mk(39, 6)[0] == 1.0)
check("40 sends is enough to act on", _mk(40, 6)[0] < 1.0)

# throttling scales volume rather than stopping it
check("a throttled campaign still sends", _mk(100, 8)[0] > 0)
check("only a dangerous rate reaches zero", _mk(100, 15)[0] > 0 and _mk(100, 25)[0] == 0.0)

# switch
check("adaptive_throttle:false disables it", _mk(100, 25, {"adaptive_throttle": False})[0] == 1.0)
check("zero bounces on record is full speed", _mk(100, 0)[0] == 1.0)

# ---------- learning signals recorded at sourcing ----------
check("esp is recorded on the row", "esp" in A.DATA_COLS)
check("refreshed is recorded on the row", "refreshed" in A.DATA_COLS)
check("esp and refreshed are stamped on each send", "esp" in so.LOG_COLS and "refreshed" in so.LOG_COLS)
check("microsoft is classified", A.mail_host("contoso.com") == "microsoft365")
check("google is classified", A.mail_host("gmail.com") == "google")
check("a domain with no MX is 'none'", A.mail_host("nonexistent-xyz-987654.com") == "none")
check("blank domain is safe", A.mail_host("") == "")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
