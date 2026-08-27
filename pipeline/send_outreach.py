#!/usr/bin/env python3
"""
send_outreach.py - staggered outreach sender for the ceiling worklist.

Selects sendable rows -> validates -> renders the template -> sends via Gmail ->
records. Ships in DRY-RUN (config dry_run:true): renders every message to
outreach/send/dryrun/ and sends NOTHING. Design + rationale: method/AUTOSEND.md.

Going live requires THREE deliberate config acts, not one: dry_run:false AND
template_approved:true AND real non-empty copy (no [PLACEHOLDER] block).

Safety (hardened after review):
- exclusive flock: overlapping runs (launchd fires while a prior run is mid-send)
  can't both send the same rows.
- write-ahead send_log: a 'pending' row is written BEFORE the Gmail call and a
  'live' row after, so a crash in between can never cause a resend (dedup treats
  pending as sent; startup warns on any un-reconciled pending).
- STOP kill-switch + HALT (breaker) block; daily cap AND send-window are
  re-checked before every send; per-send jitter.

  python3 pipeline/send_outreach.py                 # one pass (dry-run unless config live)
  python3 pipeline/send_outreach.py --limit 5       # cap this pass (small first live batch)
  python3 pipeline/send_outreach.py --config path
"""
import argparse, csv, fcntl, hashlib, json, os, random, re, sys, time
from collections import Counter
from datetime import datetime, timezone, time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
SEND = os.path.join(ROOT, "outreach", "send")
STOP = os.path.join(SEND, "STOP"); HALT = os.path.join(SEND, "HALT")
LOCK = os.path.join(SEND, ".send.lock")
SEND_LOG = os.path.join(SEND, "send_log.csv")
BOUNCES = os.path.join(SEND, "bounces.csv")
SUPPRESSION = os.path.join(SEND, "suppression.csv")
DRYRUN = os.path.join(SEND, "dryrun")
LOG_COLS = ["ts", "to", "company_domain", "segment", "zone", "esp", "refreshed",
            "subject", "message_id", "run_id", "mode"]
GENERIC_LOCALS = {"info", "sales", "support", "hello", "contact", "admin", "team", "office",
                  "mail", "help", "noreply", "service", "billing", "careers", "jobs", "press"}
ROLE_LOCALS = GENERIC_LOCALS | {"hi", "hey", "accounts", "enquiries", "inquiries", "hola",
                                "contacto", "kontakt", "founders", "general", "no-reply", "reception",
                                # PT/ES/DE/FR role locals (leads span BR/LatAm/EU)
                                "contato", "atendimento", "comercial", "vendas", "ventas", "soporte",
                                "suporte", "kontakt", "contact", "bonjour", "empresa", "geral"} | {
                                # scraped-contact-page role inboxes (bounce-prone). Added 2026-07-17
                                # after the hard-bounce breaker tripped on seo@/agent@ — --scrape-sites
                                # pulls these off footers/contact pages. is_role also matches on the
                                # first "._-"-delimited token, so "seo.team"/"marketing-eu" are caught.
                                "seo", "agent", "agents", "marketing", "webmaster", "postmaster",
                                "hostmaster", "abuse", "newsletter", "notifications", "notification",
                                "do-not-reply", "donotreply", "hr", "recruiting", "recruitment",
                                "legal", "privacy", "dpo", "compliance", "finance", "accounting",
                                "invoicing", "invoices", "invoice", "payments", "orders", "order",
                                "booking", "bookings", "reservations", "reservation", "partnerships",
                                "partners", "feedback", "returns", "refunds", "shop", "store",
                                "ecommerce", "security", "sysadmin", "mailer", "mailer-daemon",
                                "bounce", "bounces", "welcome", "kundenservice", "vertrieb",
                                "servicio", "informacion", "informazioni", "ufficio"} | {
                                # missed by the 07-17 pass: community@ hard-bounced 07-18, ai@ and
                                # dev@ were sent in error the same morning. Department inboxes below
                                # came out of the 07-18 queue audit.
                                "community", "ai", "dev", "devs", "developer", "developers",
                                "devops", "nurse", "desenvolvimento", "falecom", "itcenter"}
# Machine identities: git AUTHOR addresses belonging to coding agents and CI, not people. The
# target population is builders who use AI coding tools, so their commit history is increasingly
# authored by the tool rather than by them — agent@antigravity.ai hard-bounced on 07-17, and the
# 07-18 audit found codex@openai.com queued twice plus fix@claude.ai, all sendable.
BOT_LOCALS = {"bot", "bots", "ci", "cd", "codex", "commit", "commits", "devin", "dependabot",
              "renovate", "actions", "github-actions", "jenkins", "travis", "circleci",
              "automation", "automated", "robot", "daemon", "semantic-release",
              # antigravity@google.com hard-bounced 07-18 (helped trip the breaker at 7/100).
              # An agent whose vendor is a big company signs commits on the EMPLOYER domain, so
              # the product domain in BOT_DOMAINS does not catch it and must not be widened to
              # google.com. Match the agent's own name as a local instead.
              "antigravity"}
# Domains where ANY address is a tool identity rather than a person. Deliberately product/bot
# domains only — never an employer domain (a real OpenAI employee is @openai.com and is a valid
# lead; the Codex agent is caught by the "codex" local above, not by blocking the domain).
BOT_DOMAINS = {"claude.ai", "antigravity.ai", "cursor.sh", "devin.ai", "gpteng.co", "sibling-campaign.dev",
               "sibling-campaign.app", "users.noreply.github.com", "dependabot.com", "renovatebot.com"}
MERGE_RE = re.compile(r"\{\{(\w+)\}\}")
MIN_BODY = 20  # a live body shorter than this is almost certainly unfinished


def norm(email):
    return (email or "").strip().lower()

def load_json(p):
    with open(p) as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}

def read_csv(p):
    return list(csv.DictReader(open(p))) if os.path.exists(p) else []

def load_suppression():
    return {norm(r.get("email")) for r in read_csv(SUPPRESSION) if r.get("email")}

def log_fieldnames():
    """Column ORDER comes from the file that already exists; LOG_COLS only fixes the SET.

    append_log writes a header only when the file is new, so a log created under an older column
    list keeps that order forever. Adding esp/refreshed to LOG_COLS at positions 6-7 on 2026-07-21
    while the live file carried them appended at the end put message_id under 'mode' on read-back --
    and run() derives live_log, sent_today and the dedup set from mode == 'live'. Every cycle would
    have counted 0 sent today and re-granted the full daily cap against a shared sending address.
    A set mismatch is a deploy error, not something to write around: raise before any mail moves.
    """
    if not os.path.exists(SEND_LOG):
        return LOG_COLS
    with open(SEND_LOG, newline="") as f:
        hdr = next(csv.reader(f), [])
    if not hdr:
        return LOG_COLS
    if set(hdr) != set(LOG_COLS):
        raise SystemExit(
            f"[send] send_log.csv schema mismatch — refusing to write.\n"
            f"        only in file: {sorted(set(hdr) - set(LOG_COLS))}\n"
            f"        only in code: {sorted(set(LOG_COLS) - set(hdr))}\n"
            f"        Reconcile {SEND_LOG} with LOG_COLS before sending.")
    return hdr

def append_log(row):
    new = not os.path.exists(SEND_LOG)
    cols = log_fieldnames()
    with open(SEND_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new: w.writeheader()
        w.writerow(row); f.flush(); os.fsync(f.fileno())

# ---------- auth-failure handling ----------
# A dead/revoked OAuth token (invalid_grant) is RUN-FATAL, not a per-recipient error: the send is
# rejected at auth BEFORE Gmail accepts anything (provably not delivered), and every remaining send
# this run fails identically. So on the first one we roll back the in-flight write-ahead pending
# (the message never left) and abort the run — instead of burning the rest of the batch into
# permanent 'pending' skips (the 2026-07-16 outage: one dead token parked 27 leads as sent).
def _auth_fatal(e):
    """True for a non-transient OAuth failure (token expired/revoked). Transient/ambiguous errors
    (timeout, 5xx, connection reset) return False and stay treated-as-sent per the write-ahead log."""
    try:
        from google.auth.exceptions import RefreshError
        if isinstance(e, RefreshError):
            return True
    except Exception:
        pass
    return "invalid_grant" in str(e)

def _write_halt(run_id, msg):
    with open(HALT, "w") as f:
        f.write(f"{run_id} auth failure: {msg}")

def _rollback_pending(to, run_id):
    """Delete the write-ahead 'pending' row just written for (to, run_id). Safe ONLY on a
    provably-not-delivered auth failure (the message never left). Caller holds the send flock."""
    rows = read_csv(SEND_LOG)
    keep = [r for r in rows if not (norm(r.get("to")) == norm(to)
                                    and r.get("run_id") == run_id and r.get("mode") == "pending")]
    if len(keep) == len(rows):
        return
    tmp = SEND_LOG + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLS, extrasaction="ignore")
        w.writeheader(); w.writerows(keep); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, SEND_LOG)

# ---------- template ----------
def load_templates(cfg):
    """(by_segment, default). `templates` maps segment -> path so each row renders its own variant
    inside a single pass; `template` is the single-message fallback."""
    by_seg = {seg: load_template(pth) for seg, pth in (cfg.get("templates") or {}).items()}
    default = load_template(cfg["template"]) if cfg.get("template") else None
    if not by_seg and not default:
        raise SystemExit("[send] config has neither 'template' nor 'templates'")
    return by_seg, default

def template_for(row, by_seg, default):
    t = by_seg.get((row.get("segment") or "").strip())
    if t is None: t = default
    if t is None: raise KeyError(f"no template for segment {row.get('segment')!r} and no default")
    return t

def load_template(path):
    raw = open(os.path.join(ROOT, path) if not os.path.isabs(path) else path).read().splitlines()
    subject, body_start = "", 0
    for i, line in enumerate(raw):
        if line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip(); body_start = i + 1; break
    body = "\n".join(raw[body_start:]).lstrip("\n")
    return subject, body

def render(tmpl, row):
    """first_name is the ONLY merge field (the operator, 2026-07-20 — no company, no title).

    It comes from the worklist column, never from the email local-part: deriving it gives
    "Hi Jsmith" for jsmith@acme.com. A blank name renders "there", which is a weaker greeting
    but always a grammatical one."""
    fields = {"first_name": (row.get("first_name") or "").strip() or "there"}
    # single pass: a field value containing {{x}} is NOT re-expanded
    return MERGE_RE.sub(lambda m: (fields.get(m.group(1)) or ""), tmpl)

def unknown_merge(tmpl):
    """Merge fields the renderer cannot fill. first_name always resolves, so anything else in a
    template is a typo or a leftover from the source repo -- caught at the live gate, not in flight."""
    return sorted(set(MERGE_RE.findall(tmpl)) - {"first_name"})

# ---------- circuit breaker ----------
def breaker_reason(cfg, live_log):
    # the operator disabled the auto-halt 2026-07-21 after it tripped at 3/28 = 11%. Bounces are still
    # SCANNED and hard-bounced addresses are still suppressed, so nobody is re-mailed and the rate
    # stays visible in bounces.csv and on the dashboard -- only the automatic stop is off.
    # With this false, STOP is the only brake: nothing halts sending on any bounce rate.
    if cfg.get("breaker_enabled", True) is False:
        return None
    hard = {norm(b.get("email")) for b in read_csv(BOUNCES) if (b.get("type") or "hard") == "hard"}
    if not hard or not live_log:
        return None
    win = live_log[-int(cfg["bounce_window"]):]
    wb = sum(1 for r in win if norm(r.get("to")) in hard)
    if len(win) >= 10 and wb / len(win) >= float(cfg["bounce_rate_halt"]):
        return f"hard-bounce {wb}/{len(win)} = {wb/len(win):.0%} >= {float(cfg['bounce_rate_halt']):.0%}"
    burst_n, burst_win = cfg["bounce_burst"]
    recent = live_log[-int(burst_win):]
    rb = sum(1 for r in recent if norm(r.get("to")) in hard)
    if rb >= int(burst_n):
        return f"{rb} hard bounces in last {len(recent)} sends (>= {burst_n})"
    return None

# ---------- selection ----------
def in_window(cfg, now):
    hm = now.strftime("%H:%M")
    return any(a <= hm <= b for a, b in cfg["send_windows"])

def is_role(email):
    """Shared inbox (sales@/info@/support@ ...) or a machine identity (codex@/ci@/fix@claude.ai) —
    either way not a person who wants a personal note. Matched on the WHOLE local: a first-token
    rule lived here until 2026-07-18 and, across the project's entire 661-address history, caught
    0 role inboxes and 2 real people (mail.to.sample@, contato.sampledev@). Widen the sets
    below, never the match."""
    e = norm(email)
    local, _, domain = e.partition("@")
    return (local in ROLE_LOCALS or local in BOT_LOCALS or domain in BOT_DOMAINS
            or "noreply" in local or "no-reply" in local)

# NOTE: the source repo's region_class heuristic (email TLD + location string + app-URL TLD)
# is deleted here. Apollo filters person-level country at SOURCING time, so every row in the
# worklist is already in-region and a second guess adds only false negatives.

# ---------- timezone scheduler (deliver in each recipient's local morning) ----------
# Resolve recipient tz from coarse signals, deliver inside [target_hour-before, +after] local
# (default 08:00-11:00). Stateless: each run re-checks "is it their window now?"; a recipient not
# in-window is simply deferred to a future run (implicitly next day). All gating math is aware-UTC;
# DST is owned by zoneinfo (IANA names, never fixed offsets). See method/AUTOSEND.md.
CC_TZ = {"us": "America/Chicago", "ca": "America/Toronto", "uk": "Europe/London", "gb": "Europe/London",
         "ie": "Europe/Dublin", "fr": "Europe/Paris", "de": "Europe/Berlin", "es": "Europe/Madrid",
         "it": "Europe/Rome", "nl": "Europe/Amsterdam", "be": "Europe/Brussels", "lu": "Europe/Luxembourg",
         "at": "Europe/Vienna", "ch": "Europe/Zurich", "se": "Europe/Stockholm", "no": "Europe/Oslo",
         "dk": "Europe/Copenhagen", "fi": "Europe/Helsinki", "is": "Atlantic/Reykjavik", "pt": "Europe/Lisbon",
         "pl": "Europe/Warsaw", "cz": "Europe/Prague", "sk": "Europe/Bratislava", "hu": "Europe/Budapest",
         "ro": "Europe/Bucharest", "bg": "Europe/Sofia", "hr": "Europe/Zagreb", "si": "Europe/Ljubljana",
         "ee": "Europe/Tallinn", "lv": "Europe/Riga", "lt": "Europe/Vilnius", "gr": "Europe/Athens",
         "cy": "Asia/Nicosia", "mt": "Europe/Malta"}
LOC_TZ = {"los angeles": "America/Los_Angeles", "san francisco": "America/Los_Angeles",
          "california": "America/Los_Angeles", "seattle": "America/Los_Angeles", "portland": "America/Los_Angeles",
          "new york": "America/New_York", "brooklyn": "America/New_York", "boston": "America/New_York",
          "miami": "America/New_York", "atlanta": "America/New_York", "chicago": "America/Chicago",
          "austin": "America/Chicago", "texas": "America/Chicago", "denver": "America/Denver",
          "colorado": "America/Denver", "phoenix": "America/Phoenix", "arizona": "America/Phoenix",
          "london": "Europe/London", "england": "Europe/London", "manchester": "Europe/London",
          "berlin": "Europe/Berlin", "munich": "Europe/Berlin", "paris": "Europe/Paris",
          "stockholm": "Europe/Stockholm", "sweden": "Europe/Stockholm", "amsterdam": "Europe/Amsterdam",
          "madrid": "Europe/Madrid", "dublin": "Europe/Dublin"}
# US state abbrevs — dropped the ones that collide with common words (" or "=conjunction,
# " co "="& Co", " ma "=word) since they'd mis-resolve non-US strings. Checked AFTER explicit
# country/city names (see recipient_tz order) so an abbrev never overrides a real country.
STATE_ABBR_TZ = {" va ": "America/New_York", " ny ": "America/New_York", " fl ": "America/New_York",
                 " ga ": "America/New_York", " nc ": "America/New_York", " tx ": "America/Chicago",
                 " il ": "America/Chicago", " mn ": "America/Chicago", " az ": "America/Phoenix",
                 " ca ": "America/Los_Angeles", " wa ": "America/Los_Angeles"}
LOC_CC = {"united states": "us", "usa": "us", "america": "us", "canada": "ca", "united kingdom": "uk",
          "england": "uk", "scotland": "uk", "wales": "uk", "ireland": "ie", "germany": "de",
          "deutschland": "de", "france": "fr", "spain": "es", "españa": "es", "italy": "it", "italia": "it",
          "netherlands": "nl", "belgium": "be", "sweden": "se", "norway": "no", "denmark": "dk",
          "finland": "fi", "poland": "pl", "portugal": "pt", "switzerland": "ch", "austria": "at"}

def _cc(host):
    """Two-letter ccTLD of a hostname, or "". Used as the last-resort timezone signal when a
    location string yields nothing. (Restored 2026-07-20: this was removed alongside the region
    gate, leaving recipient_tz to raise NameError on any location it could not otherwise resolve.)"""
    last = (host or "").rstrip(".").split(".")[-1].lower()
    return last if len(last) == 2 else ""

# US states and Canadian provinces -> IANA zone. Apollo returns structured "City, State, Country",
# so the region token fixes the zone exactly; the older substring heuristics below only listed 11
# state ABBREVIATIONS and no full names, which sent every North Carolina and Florida recipient to
# America/Chicago via the US country default (observed 2026-07-20).
# States spanning zones are mapped to the zone holding most of the population.
STATE_TZ = {
    "alabama": "America/Chicago", "alaska": "America/Anchorage", "arizona": "America/Phoenix",
    "arkansas": "America/Chicago", "california": "America/Los_Angeles", "colorado": "America/Denver",
    "connecticut": "America/New_York", "delaware": "America/New_York",
    "district of columbia": "America/New_York", "washington dc": "America/New_York",
    "florida": "America/New_York", "georgia": "America/New_York", "hawaii": "Pacific/Honolulu",
    "idaho": "America/Boise", "illinois": "America/Chicago",
    "indiana": "America/Indiana/Indianapolis", "iowa": "America/Chicago", "kansas": "America/Chicago",
    "kentucky": "America/New_York", "louisiana": "America/Chicago", "maine": "America/New_York",
    "maryland": "America/New_York", "massachusetts": "America/New_York", "michigan": "America/Detroit",
    "minnesota": "America/Chicago", "mississippi": "America/Chicago", "missouri": "America/Chicago",
    "montana": "America/Denver", "nebraska": "America/Chicago", "nevada": "America/Los_Angeles",
    "new hampshire": "America/New_York", "new jersey": "America/New_York",
    "new mexico": "America/Denver", "new york": "America/New_York",
    "north carolina": "America/New_York", "north dakota": "America/Chicago",
    "ohio": "America/New_York", "oklahoma": "America/Chicago", "oregon": "America/Los_Angeles",
    "pennsylvania": "America/New_York", "rhode island": "America/New_York",
    "south carolina": "America/New_York", "south dakota": "America/Chicago",
    "tennessee": "America/Chicago", "texas": "America/Chicago", "utah": "America/Denver",
    "vermont": "America/New_York", "virginia": "America/New_York", "washington": "America/Los_Angeles",
    "west virginia": "America/New_York", "wisconsin": "America/Chicago", "wyoming": "America/Denver",
    # Canadian provinces and territories
    "alberta": "America/Edmonton", "british columbia": "America/Vancouver",
    "manitoba": "America/Winnipeg", "new brunswick": "America/Moncton",
    "newfoundland and labrador": "America/St_Johns", "newfoundland": "America/St_Johns",
    "northwest territories": "America/Yellowknife", "nova scotia": "America/Halifax",
    "nunavut": "America/Iqaluit", "ontario": "America/Toronto",
    "prince edward island": "America/Halifax", "quebec": "America/Toronto", "qu\u00e9bec": "America/Toronto",
    "saskatchewan": "America/Regina", "yukon": "America/Whitehorse",
}
NA_COUNTRIES = {"united states", "usa", "us", "u.s.", "u.s.a.", "america", "canada"}

def _structured_tz(loc):
    """Zone from a structured 'City, State, Country' string, or None.

    Preferred over the substring heuristics because it cannot mis-fire: 'Portland, Maine' resolves
    on the STATE (Eastern) rather than matching the city 'portland' and returning Pacific."""
    parts = [p.strip().lower() for p in loc.split(",") if p.strip()]
    if len(parts) < 2:
        return None
    if parts[-1] not in NA_COUNTRIES:      # only US/Canada have an unambiguous region token here
        return None
    tz = STATE_TZ.get(parts[-2])           # 'City, State, Country'
    if tz:
        return tz
    return LOC_TZ.get(parts[0])            # no usable region -> fall back to the city

def _zone(name, default_name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, TypeError):   # TypeError: non-string cfg value
        try:
            return ZoneInfo(default_name)
        except Exception:
            return ZoneInfo("America/New_York")

def recipient_tz(row, cfg):
    """Best-effort IANA zone. Explicit city, then explicit COUNTRY, then US-state abbrev, then TLD,
    then default. Country/city before the 2-letter abbrev so 'Germany or remote' resolves to Berlin,
    not Oregon (the abbrev map would otherwise catch the word 'or' first)."""
    default = cfg.get("default_timezone", "America/New_York")
    raw = (row.get("owner_location") or "").strip()
    st = _structured_tz(raw)                               # 'City, State, Country' -> exact
    if st:
        return _zone(st, default)
    loc = " " + raw.lower() + " "
    for kw, tz in LOC_TZ.items():                          # explicit city / region (most specific)
        if kw in loc: return _zone(tz, default)
    for kw, cc in LOC_CC.items():                          # explicit country name beats an abbrev collision
        if kw in loc and cc in CC_TZ: return _zone(CC_TZ[cc], default)
    for tok, tz in STATE_ABBR_TZ.items():                  # US-state abbrev fallback ("Wytheville VA")
        if tok in loc: return _zone(tz, default)
    dom = norm(row.get("email")).split("@")[-1]
    url = re.sub(r"^https?://(?:www\.)?", "", (row.get("live_url") or "").lower()).split("/")[0]
    for host_cc in (_cc(dom), _cc(url)):
        if host_cc in CC_TZ: return _zone(CC_TZ[host_cc], default)
    return _zone(default, default)

def _slot_offset_minutes(email, window_minutes):
    """Deterministic per-recipient offset into the window (spreads sends, stateless)."""
    h = hashlib.sha256(norm(email).encode()).digest()
    return int.from_bytes(h[:4], "big") % window_minutes

def due_now(row, cfg, now_utc):
    """True iff recipient's LOCAL time is in [start,end) AND past their hash offset into the window.
    Once past the offset a recipient stays due until window end, so the due-span is
    (window - offset). We cap the offset at (window - cadence) so EVERY recipient has >= one
    cadence-interval of due-span — otherwise a high-offset recipient (due only in the last minute)
    would be skipped by the coarse fire cadence every single day and never send."""
    local = now_utc.astimezone(recipient_tz(row, cfg))
    ch, bfr, aft = int(cfg.get("target_hour", 9)), int(cfg.get("window_before_hours", 1)), int(cfg.get("window_after_hours", 2))
    start_h, end_h = ch - bfr, ch + aft
    if not (dtime(start_h, 0) <= local.time() < dtime(end_h, 0)):
        return False
    window_minutes = (bfr + aft) * 60
    spread = max(1, window_minutes - int(cfg.get("runner_cadence_minutes", 30)))
    minutes_into = (local.hour - start_h) * 60 + local.minute
    return minutes_into >= _slot_offset_minutes(row.get("email"), spread)

# Freemail is exempt from the one-per-domain rule: capping gmail.com at a single recipient would
# end the campaign on its first send. The rule exists to stop identical copy piling into one
# CORPORATE tenant's spam filter, which is not what a shared consumer host is.
FREEMAIL = {"gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com",
            "icloud.com", "me.com", "proton.me", "protonmail.com", "aol.com", "gmx.com", "gmx.de",
            "web.de", "mail.com", "yandex.com", "zoho.com", "msn.com", "hotmail.co.uk", "yahoo.co.uk"}

# ---------- adaptive throttle ----------
# The binary breaker was the wrong instrument: it fired on 3/28 = 11% (95% CI roughly 2-28%),
# stopped everything, and taught us nothing. Bounce rate should modulate VOLUME, not flip a switch.
# A clean run earns its way up to the full cap; a dirty one is throttled back while still producing
# data; only a genuinely dangerous rate stops sending. Reputation damage scales with volume, so
# halving the cap halves the harm without ending the experiment.
#
# Tiers are (min_bounce_rate, cap_multiplier). Evaluated against the trailing window, and only
# once the window holds THROTTLE_MIN_N sends -- below that the rate is noise and the cap is full.
THROTTLE_TIERS = [(0.20, 0.0),    # >=20%: stop. the list is bad, not unlucky.
                  (0.12, 0.25),   # >=12%: quarter speed
                  (0.07, 0.50),   # >=7%:  half speed
                  (0.04, 0.75)]   # >=4%:  three-quarter speed
THROTTLE_MIN_N = 40               # below this the denominator cannot support a decision

def throttle_factor(cfg, live_log):
    """(multiplier, note). Multiplier scales today's cap by the trailing hard-bounce rate."""
    if cfg.get("adaptive_throttle", True) is not True:
        return 1.0, ""
    hard = {norm(b.get("email")) for b in read_csv(BOUNCES) if (b.get("type") or "hard") == "hard"}
    win = live_log[-int(cfg.get("throttle_window", 100)):]
    n = len(win)
    if not hard or n < int(cfg.get("throttle_min_n", THROTTLE_MIN_N)):
        return 1.0, f"bounce sample too small to act on ({n} sends)"
    rate = sum(1 for r in win if norm(r.get("to")) in hard) / n
    for threshold, mult in THROTTLE_TIERS:
        if rate >= threshold:
            return mult, f"bounce {rate:.0%} over last {n} -> cap x{mult}"
    return 1.0, f"bounce {rate:.0%} over last {n} -> full speed"

# ---------- per-timezone quotas ----------
# Follow-the-sun already falls out of the tz scheduler: everyone is mailed 08:00-11:00 THEIR time,
# so EU fires while the operator sleeps and the Americas follow. What that does NOT guarantee is a fair
# split -- EU windows open ~6h earlier, so on an EU-heavy list Europe can eat the whole daily cap
# before New York wakes up. These quotas cap each zone's share of the day.
#
# A bucket is the recipient's CURRENT UTC offset, so it tracks DST on its own and needs no
# hand-maintained zone list. Western Europe collapses to UTC+00/+01/+02, North America to
# UTC-04..-08, which is the 2-vs-4 split the operator described.
def zone_bucket(row, cfg, now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    off = now_utc.astimezone(recipient_tz(row, cfg)).utcoffset()
    return "UTC%+03d" % int((off.total_seconds() if off else 0) // 3600)

def _off(bucket):
    try: return int(bucket.replace("UTC", ""))
    except ValueError: return 0

def _fill(budget, supply):
    """Even split of `budget` across buckets, capped by each bucket's supply, leftovers
    redistributed until either runs out. Deterministic (sorted) so runs are reproducible."""
    out = {k: 0 for k in supply}
    remaining = int(budget)
    active = {k for k, v in supply.items() if v > 0}
    while remaining > 0 and active:
        share = remaining // len(active)
        if share == 0:                                   # hand out the remainder one at a time
            for k in sorted(active):
                if remaining == 0: break
                if out[k] < supply[k]: out[k] += 1; remaining -= 1
            break
        moved = False
        for k in sorted(active):
            take = min(share, supply[k] - out[k])
            if take > 0: out[k] += take; remaining -= take; moved = True
        active = {k for k in active if out[k] < supply[k]}
        if not moved: break
    return out

def allocate(cap, pending, eu_share=0.5):
    """{bucket: target sends today} for a day's cap, given {bucket: rows waiting}.

    Split the cap between Europe (offset >= 0) and the Americas (offset < 0), then evenly across
    the zones that actually have supply inside each. A region that cannot use its budget donates
    the slack to the other, so an even split never costs throughput."""
    pending = {b: n for b, n in pending.items() if n > 0}
    if not pending or cap <= 0:
        return {}
    eu = {b: n for b, n in pending.items() if _off(b) >= 0}
    na = {b: n for b, n in pending.items() if _off(b) < 0}
    if not eu:   eu_budget, na_budget = 0, cap
    elif not na: eu_budget, na_budget = cap, 0
    else:        eu_budget = round(cap * eu_share); na_budget = cap - eu_budget
    out = _fill(eu_budget, eu); out.update(_fill(na_budget, na))
    slack = cap - sum(out.values())                      # one region short -> give it to the other
    if slack > 0:
        left = {b: pending[b] - out.get(b, 0) for b in pending if pending[b] - out.get(b, 0) > 0}
        for b, v in _fill(slack, left).items(): out[b] = out.get(b, 0) + v
    return out

def select(rows, cfg, suppressed, already, cap, now_utc, sent_domains=None, bucket_room=None):
    import validate_emails
    out = []
    picked = set()          # emails already selected THIS run
    # One send per company domain, ever (the operator, 2026-07-20). sent_domains carries every domain in
    # the send log; picked_domains additionally guards within this run.
    picked_domains = set(sent_domains or ())
    one_per_domain = cfg.get("one_per_domain", True)
    for r in rows:
        if (r.get("status") or "").strip():
            continue
        email = norm(r.get("email"))
        if not email or email in suppressed or email in already:
            continue
        if email in picked:   # same email on another worklist row: dedup within the run (else double-send)
            continue
        dom = email.split("@")[-1]
        if one_per_domain and dom not in FREEMAIL and dom in picked_domains:
            continue
        if cfg.get("skip_role_addresses") and is_role(email):
            continue
        if cfg["segments"] and r.get("segment") not in cfg["segments"]:
            continue
        if cfg.get("require_email_valid"):
            ev = (r.get("email_valid") or "").strip().lower()
            if ev == "":
                ok, _ = validate_emails.check(email); ev = "true" if ok else "false"
            if ev != "true":
                continue
        if cfg.get("tz_scheduler") and now_utc is not None and not due_now(r, cfg, now_utc):   # not their local morning yet -> defer
            continue
        if bucket_room is not None:                      # this zone has used its share of the day
            b = zone_bucket(r, cfg, now_utc)
            if bucket_room.get(b, 0) <= 0:
                continue
            bucket_room[b] -= 1
        out.append(r)
        picked.add(email)
        if dom not in FREEMAIL:
            picked_domains.add(dom)
        if len(out) >= cap:
            break
    return out

def live_ok(cfg, templates):
    """All three gates must pass to send live. Returns (ok, reason)."""
    if cfg.get("dry_run", True) is not False:
        return False, "dry_run is not literally false"
    if cfg.get("template_approved", False) is not True:
        return False, "template_approved is not true (flip it deliberately when copy is final)"
    for name, subject_t, body_t in templates:
        if "[PLACEHOLDER" in (subject_t + body_t):
            return False, f"template {name} still contains the [PLACEHOLDER] block"
        if not subject_t.strip() or len(body_t.strip()) < MIN_BODY:
            return False, f"template {name}: subject empty or body under {MIN_BODY} chars"
        bad = unknown_merge(subject_t + "\n" + body_t)
        if bad:
            return False, f"template {name} has unfillable merge field(s): {bad}"
    return True, ""

# ---------- run ----------
def run(config_path, limit, ignore_window=False):
    cfg = load_json(config_path)
    now = datetime.now()                               # machine-local: run_id, today, ts, cap-bucket
    now_utc = datetime.now(timezone.utc)               # aware-UTC: ALL window/tz math
    run_id = now.strftime("%Y%m%dT%H%M%S")
    dry = cfg.get("dry_run", True) is not False        # live ONLY if literally false
    os.makedirs(SEND, exist_ok=True)
    if cfg.get("tz_scheduler"):                        # fail fast on a default_timezone typo
        _zone(cfg.get("default_timezone", "America/New_York"), "America/New_York")

    if os.path.exists(STOP):
        print("[send] STOP file present — halting, no send."); return
    if os.path.exists(HALT):
        print(f"[send] HALT present ({open(HALT).read().strip()}) — clear it to resume."); return

    by_seg, default_t = load_templates(cfg)
    all_t = [(seg, a, b) for seg, (a, b) in by_seg.items()]
    if default_t: all_t.append((cfg.get("template", "default"), default_t[0], default_t[1]))
    ok, why = live_ok(cfg, all_t)

    if not dry:
        # exclusive lock so an overlapping launchd cycle can't double-send
        lock_fh = open(LOCK, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[send] another live send holds the lock — exiting (no double-send)."); return
        if not ok:
            print(f"[send] REFUSING live send: {why}."); return

    log_fieldnames()          # preflight: a log schema we cannot write correctly aborts before any send
    log_rows = read_csv(SEND_LOG)
    live_log = [r for r in log_rows if r.get("mode") == "live"]
    # write-ahead: 'pending' means the Gmail call may have happened; treat as sent, warn to reconcile
    pending = [r for r in log_rows if r.get("mode") == "pending"]
    live_to = {norm(r.get("to")) for r in live_log}
    orphan_pending = {norm(r.get("to")) for r in pending} - live_to
    if orphan_pending:
        print(f"[send] WARNING: {len(orphan_pending)} pending send(s) never confirmed live "
              f"(prior crash?). Treating as sent (won't resend). Check: {sorted(orphan_pending)[:5]}")
    already = live_to | {norm(r.get("to")) for r in pending}

    reason = breaker_reason(cfg, live_log)
    if reason and not dry:
        with open(HALT, "w") as f: f.write(f"{run_id} circuit breaker: {reason}")
        print(f"[send] CIRCUIT BREAKER TRIPPED — {reason}. Wrote HALT, no send."); return
    if reason:
        print(f"[send] (dry-run) breaker WOULD trip: {reason}")
    if cfg.get("breaker_enabled", True) is False:
        nb = len([b for b in read_csv(BOUNCES) if (b.get("type") or "hard") == "hard"])
        print(f"[send] BOUNCE BREAKER DISABLED — no automatic halt. {nb} hard bounce(s) on record. "
              f"STOP is the only brake.")

    if not dry and not cfg.get("tz_scheduler") and not in_window(cfg, now):   # tz_scheduler replaces the global window
        print(f"[send] outside send window {cfg['send_windows']} (now {now:%H:%M}) — skipping."); return

    today = now.strftime("%Y-%m-%d")
    camp_today = [r for r in live_log if (r.get("ts") or "").startswith(today)]
    sent_today = len(camp_today)
    mult, tnote = throttle_factor(cfg, live_log)
    effective_cap = int(round(int(cfg["daily_cap"]) * mult))
    if tnote:
        print(f"[send] throttle: {tnote} (cap {cfg['daily_cap']} -> {effective_cap})")
    if mult == 0.0 and not dry:
        print("[send] throttle at zero — bounce rate is dangerous, not sending. "
              "Verify the list or lower the tier in THROTTLE_TIERS."); return
    cap = max(0, effective_cap - sent_today)
    if limit is not None:
        cap = min(cap, limit)
    if cap <= 0 and not dry:
        print(f"[send] cap {effective_cap} reached ({sent_today} today) — done."); return
    if dry:
        cap = limit if limit is not None else 10 ** 9

    rows = read_csv(os.path.join(ROOT, cfg["worklist"]))
    # ignore_window (dry-run review only) passes now_utc=None -> select skips the tz-due gate so you
    # can see every render regardless of the current hour.
    # Both keys: the throttle compares the RECIPIENT's email domain (that is the mailbox a spam
    # filter judges), but a logged company_domain can differ from it (sam@brandco.com works at
    # retailbrandgroup.com), so carry both or the history misses one of them.
    sent_domains = set()
    for r in live_log:
        t = norm(r.get("to"))
        if not t: continue
        sent_domains.add(t.split("@")[-1])
        cd = (r.get("company_domain") or "").strip().lower()
        if cd: sent_domains.add(cd)
    # Per-zone quotas: how much of today's cap each timezone may still use. Supply is every row
    # not yet contacted; spend is today's log grouped by the zone stamped at send time.
    bucket_room = None
    if cfg.get("zone_quota", True) and cfg.get("tz_scheduler"):
        supply = Counter()
        sup_suppressed = load_suppression()
        for r in rows:
            if (r.get("status") or "").strip(): continue
            e = norm(r.get("email"))
            if not e or e in sup_suppressed or e in already: continue
            supply[zone_bucket(r, cfg, now_utc)] += 1
        spent = Counter(r.get("zone") or "?" for r in camp_today)
        targets = allocate(int(cfg["daily_cap"]), dict(supply), float(cfg.get("eu_share", 0.5)))
        bucket_room = {b: max(0, t - spent.get(b, 0)) for b, t in targets.items()}
        print(f"[send] zone plan (cap {cfg['daily_cap']}): "
              + " · ".join(f"{b} {spent.get(b,0)}/{t}" for b, t in sorted(targets.items())))

    selected = select(rows, cfg, load_suppression(), already, cap,
                      None if (dry and ignore_window) else now_utc,
                      sent_domains=sent_domains, bucket_room=bucket_room)
    if not selected:
        due = " (none in their local send window right now)" if cfg.get("tz_scheduler") and not ignore_window else ""
        print(f"[send] nothing sendable{due}."); return

    if dry:
        for f in (os.listdir(DRYRUN) if os.path.isdir(DRYRUN) else []):
            os.remove(os.path.join(DRYRUN, f))
        os.makedirs(DRYRUN, exist_ok=True)
        # (Counter is imported at module level; a local import here would shadow it and
        #  leave Counter unbound in the zone-quota code above.)
        tzc = Counter()
        for i, r in enumerate(selected):
            tz = str(recipient_tz(r, cfg)) if cfg.get("tz_scheduler") else ""
            tzc[tz] += 1
            sub_t, bod_t = template_for(r, by_seg, default_t)
            slug = norm(r.get("email")).replace("@", "_at_")
            with open(os.path.join(DRYRUN, f"{i:03d}_{slug}.txt"), "w") as fh:
                fh.write(f"To: {r['email']}\nFrom: {cfg['from_name']} <{cfg['from_address']}>\n"
                         f"Segment: {r.get('segment')}  TZ: {tz}\n"
                         f"Subject: {render(sub_t, r)}\n\n{render(bod_t, r)}\n")
        scope = "all selectable (window ignored)" if ignore_window else "in-window now"
        print(f"[send] DRY-RUN: {len(selected)} rendered ({scope}) to {os.path.relpath(DRYRUN, ROOT)}/ — nothing sent.")
        print(f"[send] live gate: {'READY' if ok else 'BLOCKED — ' + why}")
        if cfg.get("tz_scheduler"):
            print(f"[send] recipient timezones: {dict(tzc.most_common())}")
        print(f"[send] would send from {cfg['from_address']} · cap {cfg['daily_cap']}/day · segments {cfg['segments']}")
        return

    # ---- LIVE ----
    import gmail_auth
    try:
        svc = gmail_auth.service()
    except Exception as e:
        if _auth_fatal(e):   # dead token at client build (the silent-outage case) -> HALT loudly, don't crash
            _write_halt(run_id, f"building Gmail client: {e}")
            print(f"[send] AUTH FAILURE building client: {e} — wrote HALT, no send (re-consent, then clear HALT)."); return
        raise
    frm = f"{cfg['from_name']} <{cfg['from_address']}>"
    worklist_path = os.path.join(ROOT, cfg["worklist"])
    sent = 0
    for r in selected:
        if os.path.exists(STOP):
            print("[send] STOP appeared mid-run — stopping."); break
        if os.path.exists(HALT):
            print("[send] HALT appeared mid-run — stopping."); break
        now = datetime.now()
        if cfg.get("tz_scheduler"):
            if not due_now(r, cfg, datetime.now(timezone.utc)):   # window closed during a long run -> skip THIS one, others may still be due
                continue
        elif not in_window(cfg, now):
            print(f"[send] left send window ({now:%H:%M}) — stopping."); break
        if sent_today + sent >= effective_cap:
            print(f"[send] cap {effective_cap} reached mid-run — stopping."); break
        to = norm(r.get("email"))
        base = {"ts": datetime.now().isoformat(timespec="seconds"), "to": to,
                "company_domain": (r.get("company_domain") or to.split("@")[-1]),
                "segment": r.get("segment"), "zone": zone_bucket(r, cfg, now_utc),
                "esp": r.get("esp") or "", "refreshed": r.get("refreshed") or "",
                "subject": render(template_for(r, by_seg, default_t)[0], r), "run_id": run_id}
        append_log({**base, "message_id": "", "mode": "pending"})   # write-ahead BEFORE send
        try:
            mid = gmail_auth.send(svc, to, base["subject"],
                                  render(template_for(r, by_seg, default_t)[1], r), from_addr=frm)
        except Exception as e:
            if _auth_fatal(e):   # dead/revoked token: provably not delivered AND global — roll back this
                _rollback_pending(to, run_id)   # lead's pending (don't park as sent) and abort the batch
                _write_halt(run_id, str(e))
                print(f"[send] AUTH FAILURE ({e}) — rolled back pending for {to}, wrote HALT, aborting run "
                      f"(re-consent the token, then clear HALT)."); break
            append_log({**base, "message_id": "", "mode": "error"})   # transient/ambiguous: keep pending+error
            print(f"[send] ERROR to {to}: {e} — logged, continuing."); continue
        append_log({**base, "message_id": mid, "mode": "live"})
        _mark_sent(worklist_path, to, today)
        sent += 1
        print(f"[send] sent {sent}/{len(selected)} -> {to} ({to.split('@')[-1]})")
        if sent < len(selected):
            time.sleep(random.uniform(*cfg["jitter_seconds"]))
    print(f"[send] done — {sent} sent this run, {sent_today + sent}/{cfg['daily_cap']} today.")

TRACK_COLS = ("status", "contacted_on", "channel")

def _mark_sent(worklist_path, email, today):
    rows = read_csv(worklist_path)
    if not rows: return
    cols = list(rows[0].keys())
    # Missing tracking columns would be eaten by extrasaction="ignore": every row would read as
    # never-contacted, with no error raised. Add them instead.
    for c in TRACK_COLS:
        if c not in cols:
            cols.append(c)
            for r in rows: r.setdefault(c, "")
    for r in rows:
        if norm(r.get("email")) == email and not (r.get("status") or "").strip():
            r["status"] = "sent"; r["contacted_on"] = today; r["channel"] = "email"
    tmp = worklist_path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    os.replace(tmp, worklist_path)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(SEND, "config.json"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--ignore-window", action="store_true",
                    help="dry-run only: render every selectable row regardless of tz window (copy review)")
    a = ap.parse_args()
    run(a.config, a.limit, ignore_window=a.ignore_window)
