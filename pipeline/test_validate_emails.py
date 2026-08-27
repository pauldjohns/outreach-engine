#!/usr/bin/env python3
"""Unit tests for validate_emails MX parsing (2026-07-19 null-MX fix).

user12345@gmail.cm hard-bounced on 07-18 and helped trip the breaker at 7/100.
gmail.cm publishes an RFC 7505 null MX ("0 .") — an explicit "this domain accepts no
mail" — plus an A record. has_mx() tested the dig output for truthiness, so "0 ." read
as a valid MX and the address passed validation as ok.

Network-free (dig is never called; the pure parser and the cache are exercised) so CI
runs it, matching test_send_rolefilter.py."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import validate_emails as v

PASS = 0; FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1
    else: FAIL += 1; print(f"FAIL: {name}")

GOOGLE_MX = "10 alt1.aspmx.l.google.com.\n20 alt2.aspmx.l.google.com.\n1 aspmx.l.google.com."

# (has_real_mx, is_null_mx)
check("null MX '0 .' -> no real MX, null flagged", v._parse_mx("0 .") == (False, True))
check("null MX with trailing blank line", v._parse_mx("0 .\n") == (False, True))
check("real MX -> real, not null", v._parse_mx("10 mail.example.com.") == (True, False))
check("several real MX", v._parse_mx(GOOGLE_MX) == (True, False))
check("empty output -> neither", v._parse_mx("") == (False, False))
check("None -> neither", v._parse_mx(None) == (False, False))
check("whitespace -> neither", v._parse_mx("   \n  ") == (False, False))
# A real MX alongside a null one is malformed per RFC 7505, but a deliverable host wins.
check("mixed real + null -> real wins", v._parse_mx("0 .\n10 mail.example.com.") == (True, False))

# A null MX is authoritative: the A-record fallback must NOT rescue it. gmail.cm resolves
# (142.251.45.133), which is exactly how the typo domain slipped through before.
v._mx_cache.clear()
v._mx_cache["gmail.cm"] = False          # what has_mx must now conclude
check("check() rejects null-MX domain", v.check("user12345@gmail.cm") == (False, "no_mx"))
v._mx_cache["gmail.com"] = True
check("check() accepts real-MX domain", v.check("someone@gmail.com") == (True, "ok"))
v._mx_cache.clear()

# Cheap local checks stay network-free and unchanged.
check("bad syntax", v.check("not-an-email") == (False, "bad_syntax"))
check("empty", v.check("") == (False, "no_email"))
check("disposable", v.check("x@mailinator.com") == (False, "disposable"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
