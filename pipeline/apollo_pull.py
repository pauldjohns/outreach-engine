#!/usr/bin/env python3
"""apollo_pull.py - headless lead sourcing for the Review campaign.

Search -> dedup by company -> enrich -> filter -> upsert outreach/worklist_review.csv.
Runs from cron with no Claude session: authenticates with the Apollo REST key.

  export APOLLO_API_KEY=...            # or ~/.config/review-outreach/apollo.env
  python3 pipeline/apollo_pull.py --target 60
  python3 pipeline/apollo_pull.py --target 60 --dry-run     # spends NO credits

Two-step by necessity. /mixed_people/api_search is free but returns only id, first_name, title
and organization NAME - no email, no location, no domain. /people/bulk_match returns those and
costs one credit per record.

That shapes the whole design: because one send per company domain is the rule, and because the
org NAME arrives free while the domain costs a credit, dedup by company happens BEFORE enrichment.
Enriching five people at one company to keep one would burn four credits per company.
"""
import argparse, csv, hashlib, json, os, subprocess, sys, time, urllib.error, urllib.request
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import send_outreach as so

OUT = os.path.join(ROOT, "outreach", "worklist_review.csv")
RAW = os.path.join(ROOT, "data")
ENV = os.path.expanduser("~/.config/review-outreach/apollo.env")
BASE = "https://api.apollo.io/api/v1"
SEARCH = f"{BASE}/mixed_people/api_search"
ENRICH = f"{BASE}/people/bulk_match"
ENRICH_CHUNK = 10          # bulk_match accepts up to 10 per call
MAX_ENRICH_PER_ROUND = 250 # credit guard: one round can never run away

TRACK_COLS = ["status", "contacted_on", "channel", "replied", "notes"]
DATA_COLS = ["first_seen", "segment", "first_name", "last_name", "title", "company",
             "company_domain", "email", "email_valid", "owner_location", "country",
             "linkedin_url", "apollo_id", "employees", "industry", "company_url", "naics", "icp",
             # Recorded for LEARNING, not filtering. Every bounce so far has been EU and none were
             # Google-hosted, but n=3 cannot support a rule. Capturing these means that in a few
             # hundred sends the question is answerable from data instead of guessed.
             "refreshed", "esp",
             # Apollo's own accept-all read, free on every enriched record. Recorded for LEARNING,
             # not filtering: on the queued rows it says 46.8% of Google and 7.5% of M365, while
             # Bouncer's abstention ran the other way (0% Google, 10% M365). Two independent
             # signals that disagree systematically; capturing both is how that gets settled.
             "email_domain_catchall"]
# Bought from a paid API, so deliberately OUT of DATA_COLS: upsert() refreshes DATA_COLS from
# incoming rows, and a sourcing pass must not be able to overwrite a verdict it did not produce.
# verify_reason keeps Bouncer's sub-reason (low_quality = role/disposable, a real mailbox fact, vs
# low_deliverability = a gateway shrug) and the accept-all flag. Captured once, because the
# idempotence guard means a reason not stored on a row costs a whole second paid pass to recover.
VERIFY_COLS = ["verify_status", "verify_reason", "verify_date"]
# email_check is written by validate_emails, not here, but it must be in COLS or upsert() deletes
# it on every run -- which it has been doing since the column was added.
COLS = TRACK_COLS + DATA_COLS + VERIFY_COLS + ["email_check"]

# Person-level geography. The constraint is where the HUMAN is, not the company HQ, so this is
# also passed to Apollo as person_locations and re-checked on the enriched record.
TARGET_COUNTRIES = {
    "united states", "usa", "canada", "united kingdom", "england", "scotland", "wales",
    "northern ireland", "ireland", "germany", "france", "spain", "italy", "netherlands",
    "belgium", "luxembourg", "austria", "switzerland", "sweden", "norway", "denmark",
    "finland", "iceland", "portugal", "greece", "malta",
}
# Geography is searched as TWO pools so the US can be held at half the list (the operator, 2026-07-20).
# Weighting all countries in one query returned 3 US out of 12, because Apollo does not rank by
# the order locations are listed.
US_LOCATIONS = ["United States"]
EU_LOCATIONS = ["Canada", "United Kingdom", "Ireland", "Germany", "France", "Spain", "Italy",
                "Netherlands", "Belgium", "Austria", "Switzerland", "Sweden", "Norway",
                "Denmark", "Finland", "Portugal"]
US_SHARE = 0.5

# IC engineers only. "lead engineer" and "engineering manager" were dropped 2026-07-20: they pull
# in non-software firms that employ engineers rather than software teams that ship a product.
TITLES = ["backend engineer", "backend developer", "full stack engineer", "full stack developer",
          "software engineer", "senior software engineer"]
EMPLOYEE_RANGES = ["50,100", "101,200", "201,500"]

# Include software/product companies...
KEYWORD_TAGS = ["SaaS", "B2B", "B2C", "Software"]
# ...and exclude regulated or non-product sectors, which do not have the branch-review problem.
#   62 health care · 61 educational services · 92 public administration
#   52 finance and insurance · 5416 management/scientific/technical consulting · 22 utilities
# NOTE: edtech is deliberately still reachable - those companies classify as software (5112),
# not as educational services (61). IT-services firms sit under 5415 ALONGSIDE genuine custom
# software shops, so they cannot be excluded by NAICS without collateral damage; the keyword
# tags carry that filtering instead.
EXCLUDE_NAICS = ["62", "61", "92", "52", "5416", "22"]

# Positive requirement: the company must actually BE a software company. Excluding sectors one at
# a time is whack-a-mole -- a tactical-apparel retailer (NAICS 458110) and a fleet-leasing firm
# (532112) both reached the list on 2026-07-20 because neither code was on the exclusion list, and
# Apollo files both under the industry label "information technology & services".
#   5112  software publishers      5132  (51321) software publishers, current coding
#   5415  computer systems design  5182  data processing / hosting
INCLUDE_NAICS = ["5112", "5132", "5415", "5182"]
# Same test applied again after enrichment, where per-company codes are returned. Prefix match.
SOFTWARE_NAICS = ("5112", "5132", "51321", "5415", "54151", "5182", "51821", "518")
SOFTWARE_SIC = {"7371", "7372", "7373", "7374", "7375", "7376", "7379", "7389"}
# Industry labels that are never a fit regardless of codes.
REJECT_INDUSTRY = {"retail", "automotive", "apparel & fashion", "consumer goods", "wholesale",
                   "restaurants", "food & beverages", "real estate", "construction", "mining & metals",
                   "oil & energy", "transportation/trucking/railroad", "airlines/aviation",
                   "hospital & health care", "insurance", "banking", "government administration"}


def icp_reason(org):
    """"" if the company looks like a software company, else why it was rejected.

    Runs on the ENRICHED record, which is the first point Apollo returns per-company NAICS/SIC
    codes. Requires positive evidence of software rather than trusting the coarse `industry`
    label, which reads "information technology & services" for a fleet-leasing firm."""
    ind = (org.get("industry") or "").strip().lower()
    if ind in REJECT_INDUSTRY:
        return f"industry={ind}"
    naics = [str(c) for c in (org.get("naics_codes") or [])]
    sic = {str(c) for c in (org.get("sic_codes") or [])}
    if any(c.startswith(SOFTWARE_NAICS) for c in naics):
        return ""
    if sic & SOFTWARE_SIC:
        return ""
    if not naics and not sic:
        # no codes at all: fall back to the industry label rather than dropping a real lead
        return "" if "information technology" in ind or "software" in ind or "internet" in ind \
               else f"no software codes (industry={ind or 'unknown'})"
    return f"not software (naics={naics or '-'} sic={sorted(sic) or '-'})"


def load_key():
    k = os.environ.get("APOLLO_API_KEY", "").strip()
    if k:
        return k
    if os.path.exists(ENV):
        for line in open(ENV):
            line = line.strip()
            if line.startswith("APOLLO_API_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit(f"[apollo] no API key. Set APOLLO_API_KEY or put it in {ENV}")

def post(url, payload, key, tries=4):
    """POST with backoff. Apollo rate-limits per minute/hour; a 429 here must not lose the run."""
    body = json.dumps(payload).encode()
    for attempt in range(tries):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Cache-Control": "no-cache", "x-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                wait = 2 ** attempt * 15
                print(f"[apollo] HTTP {e.code}, retrying in {wait}s ({detail[:80]})"); time.sleep(wait); continue
            sys.exit(f"[apollo] HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            if attempt < tries - 1:
                time.sleep(2 ** attempt * 15); continue
            sys.exit(f"[apollo] network error: {e}")
    return {}

def search_page(key, page, locations, per_page=100):
    return post(SEARCH, {"person_titles": TITLES,
                         "person_locations": locations,
                         "organization_num_employees_ranges": EMPLOYEE_RANGES,
                         "q_organization_keyword_tags": KEYWORD_TAGS,
                         "organization_naics_codes": INCLUDE_NAICS,
                         "not_organization_naics_codes": EXCLUDE_NAICS,
                         # server-side, so we never spend a credit enriching a record we would
                         # then drop for an unverified address
                         "contact_email_status": ["verified"],
                         "per_page": per_page, "page": page}, key)

def gather(key, locations, want, seen_companies, taken_companies, max_pages, label):
    """Walk search pages until `want` distinct NEW companies are collected. Dedup happens on the
    free search payload; nothing here spends a credit."""
    got, page = [], 1
    while len(got) < want and page <= max_pages:
        people = (search_page(key, page, locations) or {}).get("people") or []
        if not people:
            print(f"[apollo] {label}: search exhausted at page {page}"); break
        for p in people:
            cname = _norm_company((p.get("organization") or {}).get("name"))
            if not cname or cname in seen_companies or cname in taken_companies:
                continue
            taken_companies.add(cname); got.append(p)
            if len(got) >= want: break
        print(f"[apollo] {label} page {page}: {len(people)} results -> {len(got)}/{want} distinct companies")
        page += 1; time.sleep(1)
    return got

def enrich(key, ids):
    out = []
    for i in range(0, len(ids), ENRICH_CHUNK):
        chunk = ids[i:i + ENRICH_CHUNK]
        res = post(ENRICH, {"details": [{"id": x} for x in chunk]}, key)
        out.extend(res.get("matches") or [])
        time.sleep(1)
    return out

MX_CACHE = {}

def mail_host(domain):
    """Which provider actually runs the recipient's mail. Cheap DNS, cached per domain, and the
    only pre-send attribute we can observe for free.

    Measured 2026-07-21, not assumed: RCPT probing from this machine is blind exactly where it
    would help. All four Microsoft 365 domains tested returned the same answer for a real mailbox
    and a gibberish one -- `550 5.7.1 Service unavailable, Client host [<redacted-egress-ip>] blocked
    using Spamhaus` -- so M365, which carries a 21.1% bounce rate here, yields no signal at all.
    Google answered honestly (250 for real, 550 5.1.1 for gibberish, and 550 5.2.1 'inactive' for
    the one that later bounced), but Google is the 3.7% segment we do not need help with. See
    method/DECISIONS.md."""
    d = (domain or "").lower()
    if not d:
        return ""
    if d in MX_CACHE:
        return MX_CACHE[d]
    try:
        out = subprocess.run(["dig", "+short", "MX", d], capture_output=True, text=True, timeout=8).stdout
        hosts = sorted((int(l.split()[0]), l.split()[1].lower()) for l in out.strip().splitlines()
                       if len(l.split()) == 2)
        top = hosts[0][1] if hosts else ""
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        top = ""
    # Patterns verified against live MX on the queue, 2026-07-21: without office365.us, .smtp.goog
    # and ppe-hosted, 5 of 55 rows in the small strata mislabelled as 'other' -- broadcom.com is
    # Google, canvas-inc.com is M365, protechsolutions.com is Proofpoint. A wrong ESP label taints
    # every by-host bounce number, so the classifier has to know the government-cloud and
    # relay-brand hostnames, not just the commercial ones.
    esp = ("microsoft365" if "outlook" in top or "microsoft" in top or "office365.us" in top else
           "google" if "google" in top or ".smtp.goog" in top else
           "proofpoint" if "pphosted" in top or "proofpoint" in top or "ppe-hosted" in top else
           "mimecast" if "mimecast" in top else
           "barracuda" if "barracuda" in top else
           "none" if not top else "other")
    MX_CACHE[d] = esp
    return esp

def _norm_company(name):
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())

def person_location(rec):
    """City first: the timezone scheduler resolves a city precisely, a country not at all
    (every US row would collapse into one send window)."""
    return ", ".join(p for p in [(rec.get("city") or "").strip(),
                                 (rec.get("state") or "").strip(),
                                 (rec.get("country") or "").strip()] if p)

def assign_variant(email, variants):
    """Deterministic per person, so variants interleave through the worklist and one pass of the
    sender ships a balanced mix rather than draining the cap from a single variant."""
    h = hashlib.sha256(so.norm(email).encode()).digest()
    return variants[int.from_bytes(h[:4], "big") % len(variants)]

def existing_state():
    """(emails, domains, companies) already used - from the worklist AND the send log, so a
    domain is touched once EVER, not once per run."""
    emails, domains, companies = set(), set(), set()
    for r in so.read_csv(OUT):
        e = so.norm(r.get("email"))
        if e: emails.add(e)
        for d in ((r.get("company_domain") or "").strip().lower(), e.split("@")[-1] if e else ""):
            if d and d not in so.FREEMAIL: domains.add(d)
        if r.get("company"): companies.add(_norm_company(r["company"]))
    for r in so.read_csv(so.SEND_LOG):
        e = so.norm(r.get("to"))
        if e: emails.add(e)
        for d in ((r.get("company_domain") or "").strip().lower(), e.split("@")[-1] if e else ""):
            if d and d not in so.FREEMAIL: domains.add(d)
    return emails, domains, companies | so.load_suppression()

def to_row(rec, variants, today):
    email = so.norm(rec.get("email"))
    org = rec.get("organization") or {}
    return {**{c: "" for c in TRACK_COLS},
            "first_seen": today,
            "segment": assign_variant(email, variants),
            "first_name": (rec.get("first_name") or "").strip(),
            "last_name": (rec.get("last_name") or "").strip(),
            "title": (rec.get("title") or "").strip(),
            "company": (org.get("name") or "").strip(),
            "company_domain": (org.get("primary_domain") or "").strip().lower(),
            "email": email,
            "email_valid": "",
            "owner_location": person_location(rec),
            "country": (rec.get("country") or "").strip(),
            "linkedin_url": (rec.get("linkedin_url") or "").strip(),
            "apollo_id": (rec.get("id") or "").strip(),
            "employees": str(org.get("estimated_num_employees") or ""),
            "industry": (org.get("industry") or "").strip(),
            "company_url": (org.get("website_url") or org.get("primary_domain") or "").strip(),
            "naics": " ".join(str(c) for c in (org.get("naics_codes") or [])),
            "icp": "ok",
            "refreshed": (rec.get("last_refreshed_at") or "")[:10],
            "esp": mail_host(email.split("@")[-1]),
            # On the PERSON record. org.email_domain_catchall is absent on all 206 raw records.
            # Normalised to a string: written raw, False round-trips as "False", which reads
            # truthy from the CSV and inverts the signal.
            "email_domain_catchall": ("true" if rec.get("email_domain_catchall") is True else
                                      "false" if rec.get("email_domain_catchall") is False else "")}

def upsert(path, rows):
    existing = {r.get("email", ""): r for r in so.read_csv(path)}
    added = 0
    for nr in rows:
        k = nr.get("email", "")
        if not k: continue
        if k in existing:
            for c in DATA_COLS:                     # refresh data, never touch the operator's tracking edits
                if nr.get(c): existing[k][c] = nr[c]
        else:
            existing[k] = nr; added += 1
    # COLS decides ORDER for the columns this program knows about. It must never decide
    # MEMBERSHIP: four programs write this file, and rewriting it with a hardcoded fieldnames list
    # silently deletes whatever the other three added. That is how email_check has been vanishing
    # on every top-up, and it is why a paid verify_status column would have been re-billed hourly.
    # Anything already in the file survives, appended after the known columns.
    prior = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            prior = next(csv.reader(f), [])
    fieldnames = COLS + [c for c in prior if c not in COLS]
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader(); w.writerows(existing.values())
    os.replace(tmp, path)
    return added, len(existing)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=60, help="net-new sendable leads wanted")
    ap.add_argument("--variants", default="REV_A_staging,REV_B_bugreports,REV_C_repro")
    ap.add_argument("--max-pages", type=int, default=25, help="search pages per round before giving up")
    ap.add_argument("--max-rounds", type=int, default=6, help="search/enrich refill rounds")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--dry-run", action="store_true", help="search + report only; spends NO credits")
    a = ap.parse_args()

    key = load_key()
    variants = [v.strip() for v in a.variants.split(",") if v.strip()]
    today = date.today().isoformat()
    seen_emails, seen_domains, seen_companies = existing_state()
    print(f"[apollo] already on file: {len(seen_emails)} people, {len(seen_domains)} domains")

    # REFILL LOOP: search -> enrich -> filter, repeating until the target is met. A record rejected
    # for sector, role inbox or repeat domain is BACKFILLED rather than silently shrinking the
    # batch, so `--target 100` delivers 100 sendable leads and not 100-minus-the-rejects.
    from collections import Counter
    drop = Counter()
    rows, taken, spent, rounds = [], set(), 0, 0

    enriched_total = 0
    while len(rows) < a.target and rounds < a.max_rounds:
        rounds += 1
        need = a.target - len(rows)
        # Over-request by the observed accept rate. Asking for exactly `need` makes a low accept
        # rate converge to nothing: at 40% accept the rounds shrink 10 -> 6 -> 4 -> 3 -> 2 and
        # stall short of target. Floor the assumed rate at 0.25 so a bad first round still
        # recovers, and cap the ask so one round cannot burn the credit budget.
        rate = (len(rows) / enriched_total) if enriched_total else 1.0
        ask = min(int(need / max(rate, 0.25)) + 1, need * 4, MAX_ENRICH_PER_ROUND)
        want_us = round(ask * US_SHARE)
        us = gather(key, US_LOCATIONS, want_us, seen_companies, taken, a.max_pages, f"US r{rounds}")
        eu = gather(key, EU_LOCATIONS, ask - want_us, seen_companies, taken, a.max_pages, f"EU/CA r{rounds}")
        short = ask - len(us) - len(eu)
        if short > 0:      # one pool dry -> take the remainder from the other rather than under-deliver
            locs, lbl = (EU_LOCATIONS, "EU/CA") if len(us) >= want_us else (US_LOCATIONS, "US")
            eu.extend(gather(key, locs, short, seen_companies, taken, a.max_pages, f"{lbl} r{rounds} top-up"))
        cand = us + eu
        if not cand:
            print(f"[apollo] round {rounds}: search exhausted"); break
        if a.dry_run:
            print(f"[apollo] --dry-run: would enrich {len(cand)} records ({len(cand)} credits). Nothing spent.")
            return

        print(f"[apollo] round {rounds}: enriching {len(cand)} ({len(cand)} credits)")
        matches = enrich(key, [x["id"] for x in cand]); spent += len(cand); enriched_total += len(cand)
        # last_refreshed_at only exists on the SEARCH record, so graft it onto the enriched one
        # before filtering. It is the closest thing Apollo gives us to a staleness signal.
        refreshed_by_id = {x.get("id"): x.get("last_refreshed_at") for x in cand}
        for m in matches:
            if not m.get("last_refreshed_at"):
                m["last_refreshed_at"] = refreshed_by_id.get(m.get("id")) or ""
        os.makedirs(RAW, exist_ok=True)
        with open(os.path.join(RAW, f"apollo_raw_{today}_r{rounds}.json"), "w") as f:
            json.dump({"matches": matches}, f, indent=1)

        for rec in matches:
            email = so.norm(rec.get("email")); org = rec.get("organization") or {}
            if not email or "@" not in email or "email_not_unlocked" in email:
                drop["no_email"] += 1; continue
            if (rec.get("email_status") or "").lower() not in ("verified", ""):
                drop["unverified"] += 1; continue
            if (rec.get("country") or "").strip().lower() not in TARGET_COUNTRIES:
                drop["geo"] += 1; continue
            if so.is_role(email):
                drop["role_or_bot"] += 1; continue
            # A missing first_name no longer drops the lead: the campaign copy carries no greeting
            # (the operator, 2026-07-22), so a name-less but otherwise-valid address is fully sendable.
            why = icp_reason(org)                            # sector gate
            if why:
                drop["not_icp"] += 1
                print(f"[apollo]   dropped {org.get('name') or email}: {why}")
                continue
            if email in seen_emails:
                drop["dupe_email"] += 1; continue
            # Throttle on the EMAIL domain: that is the mailbox host whose spam filter sees the
            # copy. The org's primary_domain can differ (sam@brandco.com works at
            # retailbrandgroup.com), so block on either being used, and record both.
            dom = email.split("@")[-1].lower()
            org_dom = (org.get("primary_domain") or "").strip().lower()
            if any(d and d not in so.FREEMAIL and d in seen_domains for d in (dom, org_dom)):
                drop["dupe_domain"] += 1; continue
            seen_emails.add(email)
            for d in (dom, org_dom):
                if d and d not in so.FREEMAIL: seen_domains.add(d)
            cname = _norm_company(org.get("name"))
            if cname: seen_companies.add(cname)
            rows.append(to_row(rec, variants, today))
            if len(rows) >= a.target: break
        print(f"[apollo] round {rounds}: {len(rows)}/{a.target} sendable "
              f"(accept rate {len(rows)/enriched_total:.0%})")

    print(f"[apollo] {spent} credits over {rounds} round(s) -> {len(rows)} sendable")
    print(f"[apollo] dropped: {dict(drop)}")
    print(f"[apollo] variant mix: {dict(sorted(Counter(r['segment'] for r in rows).items()))}")
    if len(rows) < a.target:
        print(f"[apollo] NOTE: short of target ({len(rows)}/{a.target}) - raise --max-rounds or widen filters")
    added, total = upsert(a.out, rows)
    print(f"[apollo] {added} net-new -> {os.path.relpath(a.out, ROOT)} ({total} rows total)")

if __name__ == "__main__":
    main()
