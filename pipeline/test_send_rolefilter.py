#!/usr/bin/env python3
"""Unit tests for the tightened role-address filter and queue_cleanup.bad_reason (2026-07-17
breaker-trip fix). Network-free (MX paths not exercised) so CI runs it (unlike test_send.py)."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import send_outreach as so
import queue_cleanup as qc

PASS = 0; FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1
    else: FAIL += 1; print(f"FAIL: {name}")

# Role inboxes are matched on the WHOLE local only. The first-token rule that used to sit here was
# removed 2026-07-18: over the project's entire 661-address history it caught 0 role inboxes and 2
# real people (mail.to.sample@, contato.sampledev@), so it was pure lead loss.
ROLE = ["seo@medclinic.be", "agent@antigravity.ai", "marketing@x.com", "webmaster@x.io",
        "newsletter@x.com", "hr@x.com", "billing@x.com", "no-reply@x.com", "noreply@x.com",
        "postmaster@x.com", "abuse@x.net", "orders@shop.com", "booking@hotel.com",
        "info@x.com", "contact@x.com",
        # missed by the 07-17 tightening; community@ hard-bounced, ai@/dev@ were sent in error
        "community@teamsite.com", "ai@toolhub.com", "dev@campusapp.app", "dev@buildco.com",
        # department inboxes found in the 07-18 queue audit
        "devops@skillstack.com", "nurse@familyclinic.com", "desenvolvimento@exemplo.com.br"]
for e in ROLE:
    check(f"is_role({e})", so.is_role(e))

# Machine identities. These are git AUTHOR addresses belonging to coding agents and CI, not people.
# agent@antigravity.ai hard-bounced on 07-17; codex@openai.com sat in the queue twice and
# fix@claude.ai once, all sendable, until the 07-18 audit. This class grows as the target
# population adopts more agents, so it is matched by local AND by known agent domain.
BOTS = ["codex@openai.com", "commit@devbot.dev", "fix@claude.ai", "bot@x.com", "ci@x.com",
        "dependabot@x.com", "github-actions@x.com", "12345+u@users.noreply.github.com"]
for e in BOTS:
    check(f"is_role({e})", so.is_role(e))

# antigravity@google.com hard-bounced 2026-07-18 and helped trip the breaker at 7/100. Antigravity
# is Google's agentic IDE, so its commit identity sits on the EMPLOYER domain, not on the product
# domain already in BOT_DOMAINS. Caught by local, because google.com must stay sendable.
check("is_role(antigravity@google.com)", so.is_role("antigravity@google.com"))
for e in ["priya@google.com", "jane.doe@google.com", "someone@openai.com"]:
    check(f"not is_role({e})  [employer domain stays sendable]", not so.is_role(e))

# Personal / founder emails must NOT be filtered (guard against over-broadening losing real leads).
# The first three are the live leads the removed first-token rule culled or would have culled.
PERSONAL = ["mail.to.sample@gmail.com", "contato.sampledev@gmail.com", "dev.samplename@gmail.com",
            "ai.sample@gmail.com", "community.lead@gmail.com", "seo.team@x.com", "marketing-eu@x.com",
            "sender@example.com", "cofounder@example.com", "john.smith@gmail.com", "jsmith@startup.io",
            "maria.garcia@acme.co", "rmartin@hey.com", "m.example@example.nl", "first.last1@gmail.com",
            "first.last2@example.be", "first.last3@gmail.com"]
for e in PERSONAL:
    check(f"not is_role({e})", not so.is_role(e))

# queue_cleanup.bad_reason (no MX, deterministic) classifies role / noreply / empty / personal.
check("bad_reason seo -> role", qc.bad_reason("seo@x.com", do_mx=False) == "role")
check("bad_reason github-noreply", qc.bad_reason("12345+u@users.noreply.github.com", do_mx=False) == "noreply")
check("bad_reason personal -> None", qc.bad_reason("john.smith@gmail.com", do_mx=False) is None)
check("bad_reason empty -> None", qc.bad_reason("", do_mx=False) is None)
check("bad_reason blank-ish -> None", qc.bad_reason("   ", do_mx=False) is None)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
