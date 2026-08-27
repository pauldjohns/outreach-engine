#!/usr/bin/env python3
"""Four programs write outreach/worklist_review.csv. None of them may delete another's columns.

apollo_pull.upsert() rewrites the whole file with csv.DictWriter(fieldnames=COLS,
extrasaction="ignore"), and COLS is a hardcoded list. Anything in the file that is not in COLS is
silently dropped. This is not theoretical: `email_check` is written by validate_emails and has been
stripped by every top-up since it was added, surviving only because run_chain.sh recomputes it on
the next line for free.

That is survivable for a column recomputed from DNS. It is not survivable for one bought from a
paid API -- a dropped verify_status sends the queue back to the verifier and bills for it again,
every hour. The fix is that COLS decides ORDER for columns it knows about and never decides
MEMBERSHIP: any column already in the file survives.

The writers, for reference:
  apollo_pull.upsert()        fieldnames=COLS                    (rewrites everything)
  validate_emails             appends email_valid/email_check when missing
  send_outreach._mark_sent    appends TRACK_COLS when missing
  queue_cleanup               fieldnames from the reader

No network. Run: python3 pipeline/test_worklist_columns.py
"""
import csv, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import apollo_pull as ap

PASS = 0; FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1
    else: FAIL += 1; print(f"FAIL: {name}{(' — ' + detail) if detail else ''}")


def _row(email, **extra):
    r = {c: "" for c in ap.COLS}
    r["email"] = email; r["first_name"] = "Test"; r["company_domain"] = email.split("@")[1]
    r.update(extra)
    return r

def _write(path, rows, header):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

def _read(path):
    with open(path, newline="") as f:
        rdr = csv.DictReader(f)
        return list(rdr.fieldnames or []), list(rdr)


# ---------- the paid columns survive a sourcing pass ----------
PAID = {"verify_status": "deliverable", "verify_date": "2026-07-21"}

with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "worklist.csv")
    hdr = ap.COLS + [c for c in ("email_check", *PAID) if c not in ap.COLS]
    _write(p, [_row("a@one.com", email_check="ok", **PAID),
               _row("b@two.com", email_check="ok", **PAID)], hdr)

    ap.upsert(p, [])                       # the no-op call run_chain makes every hour
    cols, rows = _read(p)
    for c in ("verify_status", "verify_date", "email_check"):
        check(f"{c} survives upsert([]) — the hourly no-op", c in cols)
    check("verify_status VALUE survives", all(r.get("verify_status") == "deliverable" for r in rows),
          str([r.get("verify_status") for r in rows]))

    # and again with an actual new row, which is the path that rewrites every row
    ap.upsert(p, [_row("c@three.com")])
    cols, rows = _read(p)
    check("paid columns survive upsert with new rows", {"verify_status", "verify_date"} <= set(cols))
    keep = [r for r in rows if r["email"] == "a@one.com"]
    check("an untouched row keeps its verdict", keep and keep[0].get("verify_status") == "deliverable")
    new = [r for r in rows if r["email"] == "c@three.com"]
    check("a NEW row has no verdict (it has not been verified)", new and not new[0].get("verify_status"))

# ---------- the general rule, not just the columns we happened to name ----------
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "worklist.csv")
    hdr = ap.COLS + ["some_future_column"]
    _write(p, [_row("a@one.com", some_future_column="keep me")], hdr)
    ap.upsert(p, [])
    cols, rows = _read(p)
    check("an UNKNOWN column survives — COLS sets order, never membership",
          "some_future_column" in cols, str(cols[-3:]))
    check("the unknown column keeps its value",
          rows and rows[0].get("some_future_column") == "keep me")

# ---------- upsert must not reorder what is already there ----------
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "worklist.csv")
    hdr = ap.COLS + ["email_check"]
    _write(p, [_row("a@one.com", email_check="ok")], hdr)
    ap.upsert(p, []); first, _ = _read(p)
    ap.upsert(p, []); second, _ = _read(p)
    check("header is stable across repeated upserts (no flip-flop)", first == second,
          f"{first[-3:]} vs {second[-3:]}")

# ---------- the shipped file agrees with the code ----------
shipped = os.path.join(ROOT, "outreach", "worklist_review.csv")
if os.path.exists(shipped):
    with open(shipped, newline="") as f:
        hdr = next(csv.reader(f), [])
    missing = [c for c in ap.COLS if c not in hdr]
    check("every COLS entry exists in the shipped worklist", not missing, str(missing))

# ---------- to_row reads catchall from the person record, not the org ----------
rec = {"email": "x@acme.com", "first_name": "X", "id": "1",
       "email_domain_catchall": True,
       "organization": {"name": "Acme", "primary_domain": "acme.com"}}
row = ap.to_row(rec, ["REV_A_staging"], "2026-07-21")
check("to_row captures email_domain_catchall", "email_domain_catchall" in row, str(sorted(row)[:6]))
check("email_domain_catchall normalised to a lowercase string, not Python True",
      row.get("email_domain_catchall") == "true", repr(row.get("email_domain_catchall")))
rec_f = {**rec, "email_domain_catchall": False}
check("False normalises to 'false', not '' (both would read truthy as a bare bool)",
      ap.to_row(rec_f, ["REV_A_staging"], "2026-07-21").get("email_domain_catchall") == "false")
# the field lives on the person record; org.email_domain_catchall is absent on all 206 raw records
rec_o = {"email": "y@acme.com", "first_name": "Y", "id": "2",
         "organization": {"name": "Acme", "primary_domain": "acme.com", "email_domain_catchall": True}}
check("org-level catchall is NOT read (it is absent in real Apollo payloads)",
      ap.to_row(rec_o, ["REV_A_staging"], "2026-07-21").get("email_domain_catchall") == "")

# ---------- verify columns must not be refreshable by a sourcing pass ----------
check("verify_status is not in DATA_COLS (upsert refreshes DATA_COLS from incoming rows)",
      "verify_status" not in ap.DATA_COLS)
check("verify_date is not in DATA_COLS", "verify_date" not in ap.DATA_COLS)
check("verify columns ARE in COLS", {"verify_status", "verify_date"} <= set(ap.COLS))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
