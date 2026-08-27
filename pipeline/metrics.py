#!/usr/bin/env python3
"""metrics.py - conversion read for the Review campaign.

Deliberately small. The source repo's version keys attribution on a GitHub repo and runs to 240
lines; there are no repos here, so the whole indirection is dead code. At a few hundred sends the
question is only: how many went out, how many replied, how many signed up for the product.

  python3 pipeline/metrics.py [--days 30] [--no-gmail]

Attribution, in order: exact address -> corporate domain. Freemail domains are never used as a
key: matching gmail.com would attribute the first unrelated signup to whoever we mailed there.
Deliberately NOT matched on email local-part - "john" would grab the first unrelated john@.
"""
import argparse, csv, json, os, re, sys
import html as html_mod
from collections import Counter, defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import send_outreach as so

SEND_LOG = so.SEND_LOG
OUT_JSON = os.path.join(ROOT, "outreach", "send", "metrics.json")
OUT_HTML = os.path.join(ROOT, "outreach", "metrics.html")
ADDR_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
ACCOUNT_SUBJECT = "New org created"
READONLY = "https://www.googleapis.com/auth/gmail.readonly"


def load_sends():
    """{email: {segment, domain, ts}} for live sends, plus sends-per-day."""
    sent, daily = {}, Counter()
    for r in so.read_csv(SEND_LOG):
        if r.get("mode") != "live":
            continue
        e = so.norm(r.get("to"))
        if not e:
            continue
        ts = r.get("ts", "")
        daily[ts[:10]] += 1
        if e not in sent or ts < sent[e]["ts"]:
            sent[e] = {"segment": r.get("segment") or "?", "ts": ts,
                       "domain": (r.get("company_domain") or e.split("@")[-1]).lower(),
                       "zone": r.get("zone") or "?", "esp": r.get("esp") or "?",
                       "refreshed": (r.get("refreshed") or "")[:10]}
    return sent, daily

def gmail_signals(sent, days):
    """(replied, converted, total_accounts, ok, note) - all sets of OUR sent addresses."""
    try:
        import gmail_auth
        try:
            scopes = json.load(open(gmail_auth.TOKEN)).get("scopes", [])
        except Exception:
            scopes = []
        if READONLY not in scopes:
            return set(), set(), 0, False, "gmail.readonly not granted (run pipeline/go.sh) — sends-only view."
        import warnings; warnings.filterwarnings("ignore")
        from googleapiclient.discovery import build
        svc = build("gmail", "v1", credentials=gmail_auth._creds(), cache_discovery=False)
        me = so.norm(json.load(open(os.path.join(ROOT, "outreach", "send", "config.json"))).get("from_address"))
        by_addr = dict(sent)
        by_domain = {}
        for e, v in sent.items():
            d = v["domain"]
            if d and d not in so.FREEMAIL:
                by_domain.setdefault(d, e)

        def _hdrs(m): return {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        DAEMON = ("mailer-daemon", "postmaster", "mail delivery")
        replied = set()
        for m in svc.users().messages().list(userId="me", q=f"newer_than:{days}d in:inbox",
                                             maxResults=500).execute().get("messages", []):
            full = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                              metadataHeaders=["From"]).execute()
            frm = _hdrs(full).get("from", "").lower()
            if any(h in frm for h in DAEMON):
                continue
            a = ADDR_RE.search(frm)
            addr = so.norm(a.group(0)) if a else ""
            if addr and addr != me and addr in by_addr:
                replied.add(addr)

        def _decode(payload):
            import base64
            out = []
            def walk(p):
                b = p.get("body", {}).get("data")
                if b:
                    try: out.append(base64.urlsafe_b64decode(b).decode("utf-8", "ignore"))
                    except Exception: pass
                for part in p.get("parts", []) or []: walk(part)
            walk(payload)
            return "\n".join(out)

        converted, total = set(), 0
        for m in svc.users().messages().list(userId="me", q=f'newer_than:{days}d subject:("{ACCOUNT_SUBJECT}")',
                                             maxResults=500).execute().get("messages", []):
            full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
            total += 1
            body = _decode(full.get("payload", {})) + " " + full.get("snippet", "")
            for c in [so.norm(x) for x in ADDR_RE.findall(body) if not x.lower().endswith(OWN_DOMAIN)]:
                dom = c.split("@")[-1]
                hit = by_addr.get(c) and c or (by_domain.get(dom) if dom not in so.FREEMAIL else None)
                if hit:
                    converted.add(hit); break
        return replied, converted, total, True, ""
    except Exception as e:
        return set(), set(), 0, False, f"gmail scan failed: {str(e)[:90]}"

def build(days, use_gmail):
    sent, daily = load_sends()
    replied = converted = set(); total_accounts = 0; ok = False
    note = "gmail scan skipped (--no-gmail)." if not use_gmail else "no live sends yet — nothing to scan."
    if use_gmail and sent:
        replied, converted, total_accounts, ok, note = gmail_signals(sent, days)

    def block(subset):
        n = len(subset)
        r = len(replied & subset); c = len(converted & subset)
        return {"sent": n, "replied": r, "accounts": c,
                "reply_rate": r / n if n else 0, "acct_rate": c / n if n else 0}

    per_variant = {}
    for seg in sorted({v["segment"] for v in sent.values()}):
        per_variant[seg] = block({e for e, v in sent.items() if v["segment"] == seg})
    # queue state: what is still waiting, and where. The campaign's failure mode is a silently
    # drained worklist, so this belongs on the same page as the conversion numbers.
    wl = os.path.join(ROOT, "outreach", "worklist_review.csv")
    queue, per_zone = {"unsent": 0, "sent": 0, "skipped": 0}, {}
    # verify_status is a censored signal until sends land: risky/unknown/accept-all are UNKNOWN
    # mailbox validity, not known-good and not known-bad, so they are counted apart from the plain
    # deliverable rows. A silent drift here (a bad key culling the queue) shows up as this moving.
    verify = Counter()
    for r in so.read_csv(wl):
        st = (r.get("status") or "").strip()
        queue["sent" if st == "sent" else "skipped" if st else "unsent"] += 1
        if not st:
            z = so.zone_bucket(r, {})
            per_zone[z] = per_zone.get(z, 0) + 1
            verify[(r.get("verify_status") or "unverified").strip() or "unverified"] += 1
    # Bounce rate per slice. No pre-send check can catch a dead mailbox from this machine
    # (Spamhaus blocks SMTP probing from a residential IP), so the only way to get smarter is to
    # measure which slices actually bounce and act on that. Every bounce so far is EU and none are
    # Google-hosted -- far too small to be a rule, which is exactly why it needs to be visible.
    hard = {so.norm(b.get("email")) for b in so.read_csv(so.BOUNCES)
            if (b.get("type") or "hard") == "hard"}

    def slice_bounce(keyfn):
        agg = {}
        for e, v in sent.items():
            a = agg.setdefault(keyfn(v), {"sent": 0, "bounced": 0})
            a["sent"] += 1
            if e in hard:
                a["bounced"] += 1
        for a in agg.values():
            a["rate"] = a["bounced"] / a["sent"] if a["sent"] else 0
        return dict(sorted(agg.items()))

    def _region(v):
        return "EU/UK" if so._off(v["zone"]) >= 0 else "Americas"

    def _age(v):
        r = v["refreshed"]
        if not r:
            return "unknown"
        try:
            d = (datetime.now() - datetime.strptime(r, "%Y-%m-%d")).days
        except ValueError:
            return "unknown"
        return "<90d" if d < 90 else "90-365d" if d < 365 else ">1y"

    bounce_slices = {"by_esp": slice_bounce(lambda v: v["esp"]),
                     "by_region": slice_bounce(_region),
                     "by_zone": slice_bounce(lambda v: v["zone"]),
                     "by_data_age": slice_bounce(_age)}
    total_bounced = len([e for e in sent if e in hard])

    sent_zone = {}
    for r in so.read_csv(SEND_LOG):
        if r.get("mode") == "live":
            z = r.get("zone") or "?"
            sent_zone[z] = sent_zone.get(z, 0) + 1

    return {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "window_days": days,
            "queue": queue, "queued_by_zone": dict(sorted(per_zone.items())),
            "queued_by_verify": dict(sorted(verify.items())),
            "bounce_slices": bounce_slices, "bounced": total_bounced,
            "bounce_rate": total_bounced / len(sent) if sent else 0,
            "sent_by_zone": dict(sorted(sent_zone.items())),
            "gmail_ok": ok, "note": note, "totals": {**block(set(sent)), "total_accounts": total_accounts},
            "per_variant": per_variant, "unique_domains": len({v["domain"] for v in sent.values()}),
            "daily": dict(sorted(daily.items()))}

# ---------------- dashboard ----------------
CSS = """
:root{--bg:#fbfbfa;--fg:#1c1c1a;--muted:#75756e;--card:#fff;--line:#e7e6e1;--accent:#3b6ea5;--good:#3f7d4e;--warn:#8a6d1a;--warnbg:#fdf6e3}
@media(prefers-color-scheme:dark){:root{--bg:#17171a;--fg:#e9e9e6;--muted:#9a9a92;--card:#212127;--line:#2f2f36;--accent:#6fa8dc;--good:#7fc08f;--warn:#d8c27a;--warnbg:#2a2410}}
:root[data-theme=light]{--bg:#fbfbfa;--fg:#1c1c1a;--muted:#75756e;--card:#fff;--line:#e7e6e1;--accent:#3b6ea5;--good:#3f7d4e;--warn:#8a6d1a;--warnbg:#fdf6e3}
:root[data-theme=dark]{--bg:#17171a;--fg:#e9e9e6;--muted:#9a9a92;--card:#212127;--line:#2f2f36;--accent:#6fa8dc;--good:#7fc08f;--warn:#d8c27a;--warnbg:#2a2410}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:32px 20px}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
header h1{margin:0 0 4px;font-size:24px}.meta{color:var(--muted);font-size:13px}
.hd-actions{text-align:right}
.refresh{cursor:pointer;font:600 13px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#fff;background:var(--accent);border:none;border-radius:8px;padding:9px 14px}
.refresh:disabled{opacity:.55;cursor:default}
#rmsg{display:block;color:var(--warn);font-size:12px;margin-top:6px;max-width:240px}
.warn{background:var(--warnbg);color:var(--warn);border:1px solid var(--line);padding:10px 12px;border-radius:8px;margin:16px 0;font-size:13px}
.tiles{display:flex;gap:14px;margin:22px 0;flex-wrap:wrap}
.tile{flex:1 1 150px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.tk{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.tv{font-size:30px;font-weight:650;margin:4px 0;font-variant-numeric:tabular-nums}.ts{color:var(--muted);font-size:12px}
h2{font-size:15px;margin:26px 0 10px;color:var(--muted);font-weight:600}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:10px 12px;text-align:right;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}th{font-size:12px;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:none}
td:first-child{font-family:ui-monospace,monospace;font-size:13px}
.num{font-variant-numeric:tabular-nums}.rate{color:var(--accent);font-variant-numeric:tabular-nums}
.bars{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.row{display:flex;align-items:center;gap:10px;padding:3px 0}
.lab{width:92px;color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.wrapb{flex:1;background:transparent}.bar{height:12px;border-radius:3px;background:var(--accent);min-width:2px}
.bar.q{background:var(--muted);opacity:.55}.n{width:44px;text-align:right;font-size:13px;font-variant-numeric:tabular-nums}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
"""

# The Refresh button POSTs to /refresh, served by metrics_server.py, which re-runs metrics.py and
# writes a fresh page. Opened as a bare file:// there is no server, so the fetch throws and the
# button explains how to start one instead of silently doing nothing.
JS = """
(function(){
  var b=document.getElementById('refresh'), m=document.getElementById('rmsg');
  if(!b) return;
  b.addEventListener('click', function(){
    var old=b.textContent;
    b.disabled=true; b.textContent='Refreshing\\u2026'; m.textContent='';
    fetch('/refresh',{method:'POST'})
      .then(function(r){return r.json().then(function(j){return {ok:r.ok, j:j};});})
      .then(function(x){
        if(x.ok && x.j && x.j.ok){ location.reload(); return; }
        b.disabled=false; b.textContent=old;
        m.textContent=(x.j && x.j.message) ? ('refresh failed: '+x.j.message) : 'refresh failed';
      })
      .catch(function(){
        b.disabled=false; b.textContent=old;
        m.textContent='no server \\u2014 run: python3 pipeline/metrics_server.py';
      });
  });
})();
"""

def _pct(x): return f"{x*100:.1f}%"

def _slices(sl):
    """One small table per slice. A row under 40 sends is marked with * -- the denominator is too
    small to act on, and an unmarked 33% off three sends is how people talk themselves into
    deleting a good segment."""
    out = []
    for name, agg in sl.items():
        if not agg:
            continue
        rows = "".join(
            '<tr><td>{}</td><td class="num">{}</td><td class="num">{}</td>'
            '<td class="rate">{}{}</td></tr>'.format(
                html_mod.escape(str(k)), v["sent"], v["bounced"], _pct(v["rate"]),
                "" if v["sent"] >= 40 else " *")
            for k, v in agg.items())
        out.append(
            '<h2 style="font-size:13px;margin:14px 0 6px">{}</h2>'
            '<table><thead><tr><th>Slice</th><th>Sent</th><th>Bounced</th><th>Rate</th></tr>'
            '</thead><tbody>{}</tbody></table>'.format(
                html_mod.escape(name.replace("by_", "by ")), rows))
    return "".join(out) or '<div class="empty">no sends yet</div>'


def _bars(d, cls=""):
    if not d: return '<div class="empty">nothing yet</div>'
    mx = max(d.values()) or 1
    return "".join(
        f'<div class="row"><span class="lab">{html_mod.escape(str(k))}</span>'
        f'<span class="wrapb"><div class="bar {cls}" style="width:{max(2,round(v/mx*100))}%"></div></span>'
        f'<span class="n">{v}</span></div>' for k, v in d.items())

def render_html(mx):
    t, q = mx["totals"], mx["queue"]
    rows = "".join(
        f'<tr><td>{html_mod.escape(s)}</td><td class="num">{v["sent"]}</td>'
        f'<td class="num">{v["replied"]}</td><td class="rate">{_pct(v["reply_rate"])}</td>'
        f'<td class="num">{v["accounts"]}</td><td class="rate">{_pct(v["acct_rate"])}</td></tr>'
        for s, v in sorted(mx["per_variant"].items())) or \
        '<tr><td colspan="6" class="empty">no sends yet</td></tr>'
    warn = "" if mx["gmail_ok"] else f'<div class="warn">{html_mod.escape(mx["note"])} Reply and signup figures are unavailable or partial.</div>'
    days_left = (q["unsent"] / 100) if q["unsent"] else 0
    return f"""<div class="wrap">
<header>
<div><h1>Review campaign</h1>
<div class="meta">generated {html_mod.escape(mx["generated"])} · {mx["window_days"]}-day window · driving to review.your-domain.example</div></div>
<div class="hd-actions"><button id="refresh" class="refresh" type="button">Refresh</button><span id="rmsg"></span></div></header>
{warn}
<section class="tiles">
  <div class="tile"><div class="tk">Sent</div><div class="tv">{t["sent"]}</div><div class="ts">{mx["unique_domains"]} distinct domains</div></div>
  <div class="tile"><div class="tk">Replies</div><div class="tv">{t["replied"]}</div><div class="ts">{_pct(t["reply_rate"])} of sent</div></div>
  <div class="tile"><div class="tk">Signups</div><div class="tv">{t["accounts"]}</div><div class="ts">{_pct(t["acct_rate"])} of sent</div></div>
  <div class="tile"><div class="tk">Queued</div><div class="tv">{q["unsent"]}</div><div class="ts">~{days_left:.1f} days at 100/day</div></div>
  <div class="tile"><div class="tk">Hard bounces</div><div class="tv">{mx["bounced"]}</div><div class="ts">{_pct(mx["bounce_rate"])} of sent</div></div>
</section>
<h2>Bounce rate by slice</h2>
<div class="meta" style="margin-bottom:8px">No pre-send check can catch a dead mailbox from this machine, so these slices are how the list gets smarter. A rate marked * is under 40 sends and is noise.</div>
{_slices(mx["bounce_slices"])}
<h2>By message variant</h2>
<table><thead><tr><th>Variant</th><th>Sent</th><th>Replies</th><th>Reply&nbsp;%</th><th>Signups</th><th>Signup&nbsp;%</th></tr></thead>
<tbody>{rows}</tbody></table>
<h2>Sent by timezone</h2>
<div class="bars">{_bars(mx["sent_by_zone"])}</div>
<h2>Still queued by timezone</h2>
<div class="bars">{_bars(mx["queued_by_zone"], "q")}</div>
<h2>Sends per day</h2>
<div class="bars">{_bars(dict(list(mx["daily"].items())[-21:]))}</div>
<div class="meta" style="margin-top:22px">worklist: {q["unsent"]} unsent · {q["sent"]} sent · {q["skipped"]} skipped</div>
<div class="meta">unsent by verify: {" · ".join(f"{k} {v}" for k, v in mx.get("queued_by_verify", {}).items()) or "—"}</div>
</div>"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--no-gmail", action="store_true")
    a = ap.parse_args()
    mx = build(a.days, use_gmail=not a.no_gmail)
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    json.dump(mx, open(OUT_JSON, "w"), indent=1)
    doc = ("<!doctype html><html><head><meta charset=utf-8>"
           "<meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>Review campaign</title><style>{CSS}</style></head><body>{render_html(mx)}"
           f"<script>{JS}</script></body></html>")
    with open(OUT_HTML, "w") as f: f.write(doc)
    t = mx["totals"]
    pct = lambda x: f"{x*100:.0f}%"
    print(f"[metrics] {t['sent']} sent to {mx['unique_domains']} domains · "
          f"{t['replied']} replies ({pct(t['reply_rate'])}) · "
          f"{t['accounts']} accounts ({pct(t['acct_rate'])}) · gmail_ok={mx['gmail_ok']}")
    print(f"[metrics] queue: {mx['queue']['unsent']} unsent -> {os.path.relpath(OUT_HTML, ROOT)}")
    for seg, v in mx["per_variant"].items():
        print(f"  {seg:<20} {v['sent']:>4} sent · {v['replied']:>3} replies · {v['accounts']:>3} accounts")
    if not mx["gmail_ok"]:
        print(f"  note: {mx['note']}")

if __name__ == "__main__":
    main()
