# outreach-engine

A small, headless cold-email engine: source leads from Apollo, verify the addresses, send each
message in the recipient’s local morning through the Gmail API, scan for bounces and opt-outs, and
throttle itself when the bounce rate rises. Roughly 4,000 lines of Python and shell, no SaaS
sending platform, no queue server, no database – CSVs on disk and a cron-style loop.

It is the engine extracted from a live campaign that mailed ~950
engineers across the US, Canada, UK and Western Europe. **The contact data is not here and never
will be** – see [What is deliberately missing](#what-is-deliberately-missing).

## The motion

```
apollo_pull.py    search (free) → dedup by company → enrich (1 credit each) → filter → worklist
validate_emails   MX-check every new address, cull before it can bounce
verify_queue.py   mailbox verification (Bouncer) on the queue, cull the undeliverable
send_outreach.py  select → render the row’s variant → send via Gmail → log
bounce_scan.py    scan for NDRs and opt-outs → bounces.csv / suppression.csv
metrics.py        sends, replies, signups, per variant
```

`engine_loop.sh` runs that chain every ~30 minutes. `pipeline/go.sh` arms it, `pipeline/stop.sh`
stops it, and `outreach/send/STOP` is a hard brake honored mid-run.

## What is worth stealing

- **Timezone scheduling that is not a schedule.** `tz_scheduler` mails everyone at 08:00–11:00
  *their* local time, from the owner’s city on the row. Follow-the-sun falls out of it. `zone_quota`
  then caps each UTC-offset bucket’s share of the daily cap, and a zone that cannot fill its budget
  donates the slack, so fairness never costs volume.
- **A throttle instead of a breaker.** A binary bounce-rate halt fired on 3 bounces in 28 sends
  (95% CI ~2–28%) and stopped a healthy campaign for nothing. Bounce rate now scales the daily cap
  instead: ≥4% → 75%, ≥7% → 50%, ≥12% → 25%, ≥20% → stop. Below a minimum sample the rate is noise
  and the cap stays full.
- **One send per company domain, ever.** Several near-identical messages landing in one corporate
  tenant is what trips a shared spam filter. Enforced at sourcing and again at selection, keyed on
  the *recipient’s* mailbox domain rather than the company’s primary domain, because those differ
  more often than you would guess. Freemail is exempt.
- **Address verification that is measured, not assumed.** `method/VERIFY-PLAN.md` and
  `method/DECISIONS.md` record a head-to-head of two verification vendors scored against this
  campaign’s own bounces rather than a vendor benchmark, plus why local RCPT probing is blind
  exactly where it would need to work.
- **A role and machine-identity filter.** Shared inboxes (`info@`, `seo@`) and coding-agent commit
  identities (`codex@`, `fix@claude.ai`, `dependabot@`) are matched on the whole local part plus a
  small bot-domain set. Matching on the first token of the local part instead caught 0 role inboxes
  and 2 real people over the campaign’s full history.

## Run it

```bash
pip install -r pipeline/requirements-send.txt
cp .env.example .env      # then fill it in - it is gitignored
```

1. Google Cloud project → enable the Gmail API → OAuth consent with scopes `gmail.send` and
   `gmail.readonly` → Desktop-app client ID → save the download outside the repo. First run opens a
   browser once and stores a refresh token.
2. Put your Apollo REST key in `~/.config/<campaign>/apollo.env` as `APOLLO_API_KEY=…`.
3. Copy `examples/worklist.example.csv` to the path named in `outreach/send/config.json` and fill it
   with your own leads, or run `pipeline/apollo_pull.py` to build it.
4. Edit `outreach/send/config.json`: set `from_address` and `reply_to` to the mailbox the OAuth
   token belongs to, write real copy into the templates, then flip `dry_run` to `false` and
   `template_approved` to `true`. **Both ship deliberately disabled.** Nothing sends until you
   change them.
5. `python3 pipeline/send_outreach.py` for a single pass, or `pipeline/go.sh` to arm the loop.

Tests are offline – no Gmail auth, no Apollo key, no network:

```bash
for t in pipeline/test_*.py; do python3 "$t" || break; done
```

## What is deliberately missing

This repo is the engine only. Removed before publishing, and gitignored so they cannot come back:

- the worklists, send log, bounce log and suppression list – ~950 real people’s names, employers,
  titles, locations, LinkedIn URLs and work email addresses
- the verification bake-off artifacts, which were keyed by real address
- the campaign’s sending identity and OAuth credentials

Every address you find in the tests and docs is synthetic or belongs to a vendor or a bot. The
bounce tables in `method/DECISIONS.md` keep their SMTP codes and lose their recipients.

## Before you mail anyone

Cold email to named individuals is regulated, and most of the useful lead supply is in Europe.
CAN-SPAM (US) wants accurate headers, a physical postal address and a working opt-out. GDPR (EU/UK)
treats a work address as personal data: you need a lawful basis, a real disclosure of where the data
came from, and an honored right to erasure. The engine gives you the mechanics – suppression on
opt-out, no re-sends, a hard STOP – and none of the judgment. That part is yours.

## Layout

```
pipeline/      the engine and its offline tests
method/        decisions, source reference, verification plan – why it works the way it does
outreach/      config and templates (data files are gitignored)
examples/      synthetic worklist with the real column contract
```
