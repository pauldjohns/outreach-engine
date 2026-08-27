#!/usr/bin/env python3
"""Integrity checks on the shipped config, templates and worklist.

These guard the failure modes that are invisible until real mail goes out:
a segment with no template (KeyError mid-send, or a silent fallback to the wrong
copy), a template referencing a merge field that cannot be filled (mails a blank),
and a credential leaking into the repo (which is backed up to GitHub and holds PII).

No network. Run: python3 pipeline/test_config_integrity.py
"""
import glob, json, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import send_outreach as so

PASS = 0; FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1
    else: FAIL += 1; print(f"FAIL: {name}{(' — ' + detail) if detail else ''}")

CFG_PATH = os.path.join(ROOT, "outreach", "send", "config.json")
cfg = {k: v for k, v in json.load(open(CFG_PATH)).items() if not k.startswith("_")}

# ---------- config <-> templates ----------
templates = cfg.get("templates") or {}
check("config declares templates", bool(templates))
for seg, rel in templates.items():
    check(f"template file exists for {seg}", os.path.exists(os.path.join(ROOT, rel)), rel)

check("every configured segment has a template",
      set(cfg.get("segments") or []) <= set(templates),
      f"missing: {sorted(set(cfg.get('segments') or []) - set(templates))}")

# every template on disk should be wired up, or it is dead copy nobody reviews
on_disk = {os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, "outreach/send/templates/*.md"))}
check("no orphaned template files", on_disk == set(templates.values()),
      f"unwired: {sorted(on_disk - set(templates.values()))}")

# ---------- each template parses and renders ----------
for seg, rel in templates.items():
    subject, body = so.load_template(rel)
    check(f"{seg}: has a subject line", bool(subject.strip()))
    check(f"{seg}: body is not a stub", len(body.strip()) >= so.MIN_BODY)
    check(f"{seg}: no [PLACEHOLDER] left", "[PLACEHOLDER" not in subject + body)
    bad = so.unknown_merge(subject + "\n" + body)
    check(f"{seg}: every merge field is fillable", not bad, f"unfillable: {bad}")
    # first_name is the ONLY merge field for this campaign (the operator, 2026-07-20)
    used = set(so.MERGE_RE.findall(subject + "\n" + body))
    check(f"{seg}: uses only first_name", used <= {"first_name"}, f"also uses: {sorted(used - {'first_name'})}")
    # No name greeting in this campaign (the operator, 2026-07-22). The worklist first_name was too often
    # stale/wrong, or a full legal name ("Jonathan") when the inbox signs off "John", so the
    # "Hi {{first_name}}," opener was cut from every template. Guard that it stays out: the copy
    # carries no first_name merge and opens with no "Hi/Hey/Hello <name>" line (which, rendered
    # against a blank name, is exactly the empty greeting this check used to catch).
    rendered = so.render(body, {"first_name": ""})
    check(f"{seg}: no name greeting", "{{first_name}}" not in body and "Hi ," not in rendered
          and not rendered.lstrip().startswith(("Hi ", "Hey ", "Hello ")))
    # Mail is sent as text/plain (gmail_auth.send -> MIMEText(..., "plain")), so any markup would
    # arrive as literal characters: "**staging envs**" rather than bold. Copy pasted out of a doc
    # or a chat window is the likely way this gets in.
    markup = re.findall(r"\*\*|__|<\s*/?\s*(?:b|strong|em|i|p|br|div|span|a)\b|\[[^\]]+\]\([^)]+\)",
                        subject + "\n" + body)
    check(f"{seg}: copy is plain text, no markup", not markup, f"found: {sorted(set(markup))}")

# ---------- worklist rows all map to a live template ----------
wl = os.path.join(ROOT, "outreach", "worklist_review.csv")
if os.path.exists(wl):
    rows = so.read_csv(wl)
    segs = {(r.get("segment") or "").strip() for r in rows if not (r.get("status") or "").strip()}
    orphans = segs - set(templates)
    check("no sendable row points at a missing template", not orphans, f"orphaned: {sorted(orphans)}")
    # the one-per-domain rule must hold in the data, not just in the code
    doms = [ (r.get("email") or "").split("@")[-1].lower() for r in rows
             if (r.get("email") or "") and not (r.get("status") or "").strip() ]
    corp = [d for d in doms if d and d not in so.FREEMAIL]
    check("no duplicate corporate domain queued", len(corp) == len(set(corp)),
          f"repeats: {sorted({d for d in corp if corp.count(d) > 1})}")

# ---------- breaker sanity ----------
check("bounce_rate_halt is a fraction, not a percent", 0 < float(cfg["bounce_rate_halt"]) < 1)
burst_n, burst_win = cfg["bounce_burst"]
check("bounce burst threshold below its window", int(burst_n) < int(burst_win))
check("daily cap is positive", int(cfg["daily_cap"]) > 0)
lo, hi = cfg["jitter_seconds"]
check("jitter range is ordered", 0 < int(lo) <= int(hi))

# ---------- no credential may ever be committed ----------
BAD_PATH = re.compile(r"(token\.json|client_secret|\.env$|\.pem$|credentials\.json)")
tracked = [p for p in glob.glob(os.path.join(ROOT, "**", "*"), recursive=True) if os.path.isfile(p)]
leaks = [os.path.relpath(p, ROOT) for p in tracked
         if BAD_PATH.search(os.path.basename(p)) and ".git/" not in p]
check("no credential-shaped file in the tree", not leaks, f"found: {leaks}")

SECRET = re.compile(r"(APOLLO_API_KEY\s*=\s*['\"]?[A-Za-z0-9_\-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY)")
hits = []
for p in tracked:
    if any(x in p for x in (".git/", "/data/", "__pycache__")): continue
    if os.path.splitext(p)[1] not in (".py", ".sh", ".json", ".md", ".yml", ".yaml", ".csv", ""): continue
    try: txt = open(p, encoding="utf-8", errors="ignore").read()
    except Exception: continue
    if SECRET.search(txt): hits.append(os.path.relpath(p, ROOT))
check("no hardcoded secret in any source file", not hits, f"found in: {hits}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
