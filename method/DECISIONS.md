# Decisions

2026-07-20. a configuration choices, and the reasoning behind the non-obvious ones.

## Why a separate repo rather than a second campaign in the the sibling campaign repo

A first attempt added Review as a second campaign inside the sibling campaign's repo. It worked and
was fully tested, but it modified `send_outreach.py` and `build_metrics.py` — the code a live,
converting campaign runs on. the operator killed it. A separate repo has zero blast radius on the thing
that is currently working, at the cost of divergence between two copies of the sender. That trade
was accepted deliberately.

Copied by file, not `git clone`: the the sibling campaign history carries lead PII with no reason to be
duplicated here, and half the repo (the GitHub discovery pipeline) would have been deleted anyway.

## Why discovery could not be reused

Every gate in the the sibling campaign pipeline keys off a public GitHub repo carrying a the sibling campaign marker with a
harvestable commit email. This audience works in private company repos. There is no public
population to walk, so Apollo replaces the entire discovery half. The delivery half — sender,
validator, bounce scan, engine loop — transferred unchanged, and had zero imports from discovery.

## Why sourcing dedups on company NAME before enriching

Apollo's search endpoint is free but returns only id, first_name, title and organization **name**.
Email, location and domain require `bulk_match` at one credit each. Since the rule is one send per
domain, enriching five people at one company to keep one would waste four credits per company.
Company-name dedup happens on the free data; the domain check runs again on the enriched record.

## Why the domain throttle keys on the email domain

Real data caught this: `sam@brandco.com` has organization primary domain `retailbrandgroup.com`.
The mailbox host that judges the message is the **email** domain, so that is the throttle key.
Both are recorded and either being already-used blocks a lead.

## Why freemail is exempt from the domain throttle

Capping gmail.com at one recipient would end the campaign on its first send. The rule exists to
stop several near-identical messages piling into one corporate tenant's spam filter, which is not
what a shared consumer host is.

## Why the campaign copy carries no merge fields

a configuration choice. Fewer fields means fewer ways to render something broken, and the variants do not
need company or title to make their point. `first_name` was the only field the copy ever used, and
it came from the worklist column, never from the email local-part (which renders "Hi Jsmith" for
jsmith@acme.com).

On 2026-07-22 the `Hi {{first_name}},` greeting was cut from all three templates: the worklist name
was too often stale/wrong, or a full legal name ("Jonathan") when the inbox signs off "John", and a
wrong name on a cold note reads worse than no name. The templates now open on the pitch itself. The
merge machinery is untouched — `render` still fills `first_name` (blank -> "there") and `live_ok`
still blocks any template referencing an unfillable field — the campaign copy simply no longer
merges anything. `test_config_integrity` now asserts the greeting stays out.

## Why its own Gmail token directory

`go.sh` runs `rm -f "$TOKEN"` whenever the token fails a refresh check. Sharing
`~/.config/outreach-engine` would delete the sibling campaign's token; its next headless cycle would
block forever inside `run_local_server()`, holding the chain lock for the full 8-hour backstop
while sending nothing. Same OAuth client, same address, separate token.

## Why the breaker is 5% / [4,10] and what it cannot see

5% rather than the sibling campaign's 6% because Apollo corporate email bounces harder than commit-history
addresses. The burst rule stays at 4-in-10: 3-in-10 was considered and rejected because the
breaker is evaluated before every send across overlapping windows, so it false-trips and
reproduces the HALT-clear-retrip loop already documented in the source repo.

The signal is censored. `bounce_scan.py` reads returned NDRs; M365 and Workspace quarantine
silently. No threshold fixes a censored numerator — seed mailboxes on those hosts are the only
direct read on placement.

## Explicitly not built

No canary (a configuration choice), no cross-repo suppression or
bounce syncing (the systems are independent and not run concurrently), no LinkedIn, no second
sending domain, no HTML dashboard, no click tracking.

## Open

- **Attribution gap.** Engineers often sign up via GitHub OAuth or a personal address. `metrics.py`
  matches on exact address then corporate domain; a per-send token on the URL would close the rest
  but needs a redirect the operator controls.
- **Seed mailboxes.** Not yet added. Without them there is no read on inbox placement.

## ICP verification at sourcing (added 2026-07-20)

Two companies reached the list that should not have: a tactical-apparel retailer and a
fleet-leasing company. Both passed because the NAICS *exclusion* list did not happen to name
retail (458110) or vehicle leasing (532112), and because Apollo files both under the industry
label "information technology & services" -- the same label a real SaaS company gets.

Excluding sectors one at a time is whack-a-mole. The fix is a POSITIVE requirement, applied twice:

- **Server-side** `organization_naics_codes: 5112, 5132, 5415, 5182` at search time, so a
  non-software company is never enriched and never costs a credit.
- **Locally** `icp_reason()` after enrichment, where per-company NAICS/SIC codes are returned.
  Requires a software code, rejects a small industry blacklist outright, and falls back to the
  industry label only when a company has no codes at all (rather than dropping a real lead).

Sourcing now runs as a REFILL LOOP so rejects are backfilled instead of shrinking the batch.
The loop over-requests by the observed accept rate: asking for exactly the shortfall each round
converges to nothing (at a 40% accept rate the rounds shrink 10 -> 6 -> 4 -> 3 -> 2 and stall
short). The assumed rate is floored at 25% and the per-round ask is capped, so a bad round
recovers without running away with credits.

## A log's column ORDER belongs to the file, not the code (2026-07-21)

`98e6b1c` added `esp`/`refreshed` to `LOG_COLS` at positions 6-7 while the live `send_log.csv`
carried them appended at the end. `append_log()` writes a header only when the file is new, so
every later row would have gone in code order into a file whose header said otherwise, putting
`message_id` under `mode`. `run()` derives `live_log`, `sent_today`, the dedup set and the throttle
window from `mode == "live"`, so each cycle would have counted zero sends today and re-granted the
full daily cap every 30 minutes -- against an address shared with the sibling campaign.

Caught before it fired: the cap was already reached for the day, so nothing had been appended since
the commit. The rule now is that the existing file's header decides order and `LOG_COLS` decides
only the set; a set mismatch raises before any mail moves. Same fix applied to `bounce_scan._append`.
Adding a column to a live append-only log is the recurring shape here, not a one-off.

## Bounce detail: 'hard' was hiding two opposite emergencies (2026-07-21)

`bounces.csv` declared a `detail` column from the start that no caller ever passed, so the parsed
SMTP status was discarded and every bounce was recorded as just hard/soft. Reading the six bounces
from 2026-07-21 back out of Gmail showed why that mattered:

| address | code | what it actually is |
|---|---|---|
| alice@example-one.com | 5.1.10 | RecipientNotFound -- mailbox does not exist |
| dana@example-four.co | 5.2.1 | Google account inactive/suspended |
| bruno@example-two.de | 5.4.1 | M365 "Access denied" -- unknown recipient or tenant policy |
| carla@example-three.com | 5.4.1 | same |
| erik@example-five.com | 5.4.14 | hop count exceeded -- a mail loop on the RECIPIENT's side |
| farid@example-six.com | 5.7.23 | SPF violation on a forwarded hop |

Only the first four are list quality. The last two are not bad addresses and no verification
service would have culled them: 5.4.14 is the recipient's own routing loop, and 5.7.23 is what
`-all` plus forwarding without SRS produces at the final destination. your-domain.example's SPF
(`v=spf1 include:_spf.google.com -all`), DKIM and DMARC were checked and are correct.

So the 11.1% headline is at most ~7% addressable by pre-send verification. Worth doing, but it
does not take the rate to zero, and a bounce count is not by itself a list-quality measurement.

## Verification: buy it, do not build it (measured 2026-07-21)

the operator asked whether a local SMTP prober could replace a paid verifier, since four of the six
bounces looked like ordinary RCPT rejections. Tested rather than argued. Each domain probed twice
in one connection -- the real address and a gibberish one -- QUIT before DATA, no mail sent.

| domain | host | real | gibberish | signal |
|---|---|---|---|---|
| example-eight.com | google | `250 2.1.5 OK` | `550 5.1.1 does not exist` | **honest** |
| example-four.co | google | `550 5.2.1 inactive` | `550 5.1.1 does not exist` | **honest** |
| example-nine.com | m365 | `550 5.7.1 blocked using Spamhaus` | identical | none |
| example-ten.fr | m365 | `550 5.7.1 blocked using Spamhaus` | identical | none |
| example-two.de | m365 | `550 5.7.1 blocked using Spamhaus` | identical | none |
| example-three.com | m365 | `550 5.7.1 blocked using Spamhaus` | identical | none |
| example-eleven.com | barracuda | `250 OK` | `250 OK` | catch-all |

Microsoft rejects on connection reputation before it ever consults its directory: our egress IP
<redacted-egress-ip> is in Spamhaus, as residential IPs are by policy. So a local prober is blind on the
segment bouncing at 21.1% and sharp on the segment bouncing at 3.7% -- useful exactly where it is
not needed. Note example-four.co: Google volunteered `5.2.1 inactive`, the real reason, before we sent.

Port 25 itself is NOT blocked outbound, and Google answers fine, so the old blanket claim that
probing is "impossible from a residential IP" was too broad. The binding constraint is IP
reputation at Microsoft, not connectivity.

A cloud VM does not fix this cheaply: the major providers block outbound 25 by default, and a
fresh IP has no reputation to trade on. What a verifier sells is a pool of warmed IPs with correct
PTR and HELO -- Bouncer resolved example-two.de and example-three.com, which means Microsoft answers
*them*. That is the product.

Chose **Bouncer**. In a bake-off against our own six bounces (pipeline/verify_bakeoff.py) Bouncer
and ZeroBounce both caught 3 of 4 catchable; Bouncer over-condemned nothing and abstained honestly
on the one true catch-all (example-one.com), which ZeroBounce also could not resolve.

**ZeroBounce: the send path is a FALLBACK, not the mechanism.** Corrected after testing, 2026-07-21.
Its Verify+ tier can validate by sending a real email ("we send an email to check for bounces" --
their docs), and a public forum report describes seeing that behaviour, which we did not independently reproduce. The report describes mail from unfamiliar sending infrastructure -- one commenter reported
"Adventure-Meter Department" at bugbusterbrigade.com -- never the customer's address or domain.

the operator enabled Verify+ for all validations and we tested against your-domain.example, which is itself a
catch-all (SMTP returns 250 for every local part, gibberish included):

| address | verdict | latency | mail delivered |
|---|---|---|---|
| sender@example.com | valid | ~2s | none |
| nosuchbox@example.com | invalid / mailbox_not_found | ~2s | none |
| q7v2xk9mrt4bnz8w@example.com | invalid / mailbox_not_found, catchall_domain:true | **0.6s** | none |

Zero mail arrived across ~6 minutes, inbox plus spam plus trash. 0.6s on an address that cannot be
in any database is far too fast for send-and-observe, so ZeroBounce resolved a catch-all mailbox by
some non-SMTP means. Caveat: the gibberish local part may have been pattern-flagged rather than
looked up, so treat that one as suggestive; the second mailbox is the stronger datapoint.

So Verify+ only falls back to sending for what it cannot resolve otherwise -- which on our list is
exactly the residue that matters. example-one.com, the self-hosted catch-all both vendors missed, is
precisely the shape that would trigger it.

**Settled on Bouncer.** The open question was abstention: ZeroBounce resolves catch-alls and Bouncer
does not, so if Bouncer shrugged at a large share of the queue its clean mechanism would not be
worth the lost ICP. Measured on a stratified sample of 40 unsent rows, 20 per mail host
(`verify_bakeoff.py --queue-sample 20`):

| stratum | n | good | risky/unknown | undeliverable | abstain % |
|---|---|---|---|---|---|
| microsoft365 | 20 | 17 | 2 | 1 | 10.0% |
| google | 20 | 17 | 0 | 3 | 0.0% |
| **total** | 40 | 34 | 2 | 4 | **5.0%** (95% CI 1.4-16.5%) |

5%, not the 36-38% accept-all share the vendor literature predicts -- that range sits outside our
upper bound. Extrapolated over the 340 M365+Google queue rows: ~34 culled as undeliverable, ~17
unresolvable. Dropping every unresolved row costs 17 leads, so ZeroBounce's catch-all advantage is
worth about 17 rows and is not worth carrying a tier that can email prospects.

The 10% undeliverable rate also coheres with the 11.1% we actually bounced at, which is the check
that matters: Bouncer is finding roughly the right number of bad addresses, not inventing them.

Caveats: n=20 per stratum, so the CIs are wide and the Google-worse-than-M365 split (15% vs 5%
undeliverable) inverts the observed bounce pattern and is probably noise. These 40 verdicts have no
ground truth behind them -- the bake-off validated Bouncer against known bounces, this only measures
what it says.

**Head to head on the identical 40 rows** (Verify+ confirmed off, latency canary run first to check
no send path engaged -- all calls 0.9-1.7s, far too fast for send-and-observe):

| | good | risky/unknown | undeliverable |
|---|---|---|---|
| Bouncer | 34 | 2 | 4 |
| ZeroBounce | 35 | 0 | 5 |

**They disagree on 1 of 40 rows -- 97.5% agreement on send/don't-send.** Every address Bouncer
called undeliverable, ZeroBounce did too; ZeroBounce found one more (gina@example-seven.com),
which is one of the two Bouncer abstained on. It resolved the other as valid. So ZeroBounce's
catch-all resolution is real and does exactly what it claims -- it just has very little left to do
on this list. Practitioner reports put cross-tool disagreement at 6-40%; ours is 2%, because this
list is mostly resolvable rather than heavily accept-all.

**That settles the choice on mechanism rather than accuracy, which is the comfortable way to settle
it.** Over the 340 M365+Google queue rows the two differ by roughly 8 addresses. Bouncer has no
send path; ZeroBounce carries a tier that emails what it cannot resolve. Take Bouncer.

**Gate:** drop `undeliverable`, quarantine `risky`/`unknown` rather than deleting (holding preserves
the ever-dedup in `existing_state()`; deleting frees the domain to be re-bought at a credit).
`verify_bakeoff.py` carries an interlock refusing to send prospect addresses to ZeroBounce unless
Verify+ is confirmed off.

*Note on the artifact:* a needless re-run of the Bouncer sample exhausted the free credits (HTTP
402) partway through and overwrote the good entry in verify_bakeoff.json with a partial one. The
file now carries `_WARNING` and `_authoritative_score` on that entry. The table above is from the
complete first run. Paid credits are needed for any further Bouncer work.

## Seed mailboxes: dropped (a configuration choice)

Repeatedly suggested as the only direct read on inbox placement, since bounce_scan sees returned
NDRs and M365/Workspace quarantine silently. the operator does not have mailboxes on those hosts. The
suggestion is closed -- do not raise it again. The consequence stands and is accepted: over a
week of automated sending there is no signal distinguishing "delivered and ignored" from
"quarantined", and the breaker only catches hard bounces.
