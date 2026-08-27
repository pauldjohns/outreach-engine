# Auto-send outreach — method doc

_Implemented 2026-07-07. Canonical reference for the send stream: turns the ceiling worklist into automated, staggered cold email. Compliance controls - footer, opt-out mechanism, region gating - are yours to configure; see Suppression & opt-out. Ships in dry-run; three deliberate config flips required to go live._

## Decisions (a configuration choice)
- **Hand-rolled sender** (no Apollo/Smartlead). Gmail API + OAuth, sent from **`sender@example.com`** (primary domain), **config-swappable** to a dedicated domain later without a rewrite.
- **Fully autonomous** steady-state; one dry-run review before the first live send.
- **Full chain** (poll → bounce/opt-out scan → send) on **local `launchd`**, 3×/day.
- **Volume**: flat cap **100/day** (inbox already warmed — no ramp).
- **Volume ramp and region gating are configuration decisions** — see Suppression & opt-out below. Suppression gate + circuit breaker are always on.

## Update 2026-07-15 — segment expansion + dedup fix (a configuration choice)
- **Sending segments widened** from `[B_all_bot]` to `[B_all_bot, A_hybrid, C_mover, presumed_silent_graduate]`. B_all_bot alone had drained to ~4 sendable; the addressable pool is now ~145.
- **`presumed_silent_graduate` is a ONE-SHOT manual cohort, NOT a live-fed segment.** The scorer still routes PSG out (absent from the `qualified`/contact/worklist paths in `ceiling_poll.py`), so no new PSG flows automatically. This batch was enriched offline, filtered to the 63 with a real non-the sibling campaign edge-function API integration (Stripe/Resend/OpenAI/…), junk/placeholder-email filtered, email-deduped vs the live worklist+send_log, and **staged: 15 seeded, 46 held back** pending a bounce check on the first batch. To release the rest, upsert the remaining `psg_survivors.csv` rows.
- **Double-send bug fixed** (`select()` now dedups by email within a run). It had fired live 3× (one address 3×). At activation, 10 multi-repo founders in the enabled segments would otherwise have double-sent 12 times.
- **`daily_cap` kept at 100** (a deliberate choice). Note this is now a *sustained* ~50–60/day cold stream from `sender@example.com`, not a one-week backlog drain — the ceiling stream self-refills and the edge signal raises B_all_bot inflow. Steady-state reputation exposure accepted.
- `C_mover_fresh` deliberately left OUT (thinner/fresher signal).

## Load-bearing risk (stated, accepted, mitigated)
Cold-sending from the primary domain risks deliverability of **all** mail on that domain. Accepted by the operator. Mitigations: from-address is one config line (swap to a dedicated domain by editing config + DNS, no code change); flat conservative cap; circuit breaker; validation culls dead domains before they bounce.

## Architecture
```
launchd (3x/day) ── flock (whole-cycle) ── run_chain.sh
   ├─ ceiling_poll.py --scrape-sites   (discover + enrich + worklist upsert)
   ├─ bounce_scan.py                   (bounces + reply opt-outs → suppression; needs gmail.readonly)
   └─ send_outreach.py
        ├─ flock (send-level) · STOP · HALT · live-gate (dry_run/template_approved/copy)
        ├─ select  (status blank · valid email · in-segment · not suppressed · not already-sent · under cap)
        ├─ breaker check (bounce rate from bounces.csv)
        └─ per row: write-ahead pending → Gmail send → live log → mark status=sent  (+jitter, cap/window re-check)
```

### Components
| file | purpose |
|---|---|
| `pipeline/send_outreach.py` | the sender: select → render → send → record. Dry-run default; flock; write-ahead log. |
| `pipeline/bounce_scan.py` | scans mailbox for bounces (RFC-3464 DSN) + reply opt-outs → `bounces.csv` + suppression. Needs `gmail.readonly`. |
| `pipeline/gmail_auth.py` | Gmail auth (OAuth, creds in `~/.config/outreach-engine/`) + `send()` primitive. |
| `pipeline/validate_emails.py` | pre-send syntax + MX + disposable cull (the `email_valid` gate). |
| `pipeline/run_chain.sh` | one cycle; mkdir lock (PID-liveness + 8h backstop) so cycles can't overlap; honors STOP/HALT. |
| `pipeline/engine_loop.sh` | the "server": run_chain every 30 min, detached (nohup) from a granted shell. macOS TCC blocks a launchd timer from reading ~/Documents, so the chain is a loop, not a launchd job. |
| `outreach/send/config.json` | all knobs (identity, caps, jitter, windows, breaker thresholds, segments, dry_run, template_approved). |
| `outreach/send/templates/ceiling_b.md` | subject + body with `{{merge}}` fields. Placeholder until the operator writes copy. |
| `outreach/send/suppression.csv` | never-contact list: opt-outs, bounces, manual. **Never delete.** |
| `outreach/send/send_log.csv` | one row per send (pending/live/error). Dedup source of truth + breaker input. Gitignored (PII). |
| `outreach/send/bounces.csv` | breaker input from the scan. Gitignored (PII). |
| `outreach/send/{STOP,HALT}` | STOP = kill switch; HALT = breaker trip (manual delete to resume). Gitignored. |

## Data flow & state
- **Sendable row** = `worklist_ceiling.csv` row where `status` is blank, `email` non-empty **and valid** (`email_valid`, or inline MX check), segment in config list, email not in `suppression.csv`, and not already in `send_log.csv`.
- **Ordering**: worklist is segment/trend-first, so the sender consumes best-first, top-down until the cap.
- **On send** (live): set `status=sent`, `contacted_on`, `channel=email`; matched by **email + owner_repo** (not owner_repo alone). Composes with `apply_tracking`/`build_worklist`/`ceiling_poll` upsert (all preserve a non-blank status). Dashboard renders `sent` as done.
- **No double-send, crash-safe**: a `pending` row is appended to `send_log` *before* the Gmail call and a `live` row after; dedup treats pending as sent, so a crash between call and log can't resend. **One exception** — a provably-not-delivered auth failure (`invalid_grant`, a dead/revoked token): the in-flight `pending` is rolled back (the message never left) and the run writes `HALT` and aborts, so one dead token can't burn the rest of the batch into permanent `pending` skips (2026-07-16). Transient/ambiguous send errors (timeout, 5xx, connection reset) still keep their `pending` and stay treated-as-sent. Startup warns on any un-reconciled `pending`. Whole-cycle `flock` + send-level `flock` stop overlapping launchd runs from both sending the same rows.

## Staggering & volume
- **Flat daily cap** 100/day (no ramp). Counted from `send_log` (machine-local day); re-checked before every send, so frequent fires can't exceed it even across overlap.
- **Per-send jitter** 90–240s.
- **Timezone scheduler** (`tz_scheduler:true`) — replaces the old single machine-local window. Each recipient is delivered inside **08:00–11:00 their local time** (`target_hour` 9, −`window_before_hours`/+`window_after_hours`). `recipient_tz()` resolves owner_location (state abbrev → city/country → country name) → email TLD → app-URL TLD → `default_timezone` (America/New_York). Stateless: each launchd fire, `select()` sends only rows whose recipient-local time is in-window now; a per-email hash offset spreads sends across the window (capped at `window − runner_cadence_minutes` so no one is skipped by the fire cadence); not-in-window rows defer to a later fire (implicitly next day). All window math is aware-UTC; DST owned by `zoneinfo` (IANA names, never fixed offsets). `send_windows`/`in_window` are retained but dead — flip `tz_scheduler:false` to roll back.
- **Coverage**: the machine runs in the operator's timezone. It reaches US mornings in local daytime and UK/Europe mornings at 00:00-04:00 local — which only works because the Mac is kept awake 24/7 (`ai.outreach.keepawake` runs `caffeinate -s`) and the launchd fires every 30 min round the clock. Without the keep-awake, a Mac in a US timezone could never serve Europe.

## Safety rails
- **Three-flip live gate**: sends live only if `dry_run:false` AND `template_approved:true` AND the template has real copy (no `[PLACEHOLDER]`, non-empty subject, body ≥ 20 chars). Any one missing → refuse. `dry_run` must be literally `false` (null/""/0 stay dry).
- **Circuit breaker** (any trips → write `HALT`, stop; manual delete to resume): hard-bounce ≥ **6%** over trailing **100** (`bounce_window`, raised from 50 on 2026-07-18) · ≥ **4 hard bounces in last 10**. Signal from `bounce_scan` (bounces lag sends → halts the *next* run; same-day at 3×/day). **There is no complaint trip** — this line claimed one until 2026-07-18, but `breaker_reason()` reads only hard bounces from `bounces.csv` and no complaint signal exists anywhere in the pipeline (Gmail gives non-bulk senders no feedback loop). **Open concern:** 6% is well above cold-outreach norms (<2% safe, >5% stop), and at a ~3% true rate a 6%/100 limit sits ~1.4σ above the process mean, so it false-trips ~12% of windows — that, not suppression accounting, is why clearing HALT tends to re-trip.
- **Role / machine-identity filter** (`skip_role_addresses:true` → `is_role()`): drops shared inboxes (`info@`, `sales@`, `seo@`) and machine git-author identities (`codex@openai.com`, `fix@claude.ai`, `ci@`, `dependabot@`). Matched on the **whole local** plus a small bot-domain set. A first-token rule lived here until 2026-07-18 and, over the project's entire 679-address history, caught 0 role inboxes and 2 real people — widen the sets, never the match. Machine identities are a growing class: the targeting thesis is builders who use AI coding tools, so their commit author is increasingly the tool.
- **Auth-failure guard**: a dead/revoked OAuth token (`invalid_grant`) is run-fatal, not per-recipient — the sender rolls back the in-flight `pending`, writes `HALT`, and aborts (at client build too, so a dead token HALTs loudly instead of crashing every cycle silently). Re-consent, then delete `HALT` to resume. **Root cause to fix (recurs ~weekly):** the OAuth app is in Google "Testing" status → 7-day refresh-token expiry; move to Internal user-type or a service account (Workspace domain) to end it.
- **Locks**: whole-cycle `flock` in `run_chain.sh` + send-level `flock` in `send_outreach.py`.
- **Kill switch**: `outreach/send/STOP` halts before/mid send. **HALT** re-checked mid-loop too.
- **Dry-run default**: renders to `outreach/send/dryrun/`, sends nothing, changes no status.

## Suppression & opt-out

- **Suppression gate** (`suppression.csv`, never-contact): already-sent addresses, hard bounces, and
  **reply-based opt-out** — `bounce_scan` flags inbound replies containing stop/remove/unsubscribe
  language and suppresses the replier permanently.
- **Honest headers**: real From address, non-deceptive subject.
- **Compliance is yours to configure, and it is not optional.** Before you send anything, decide how
  this stream satisfies the law where your recipients are: CAN-SPAM (US) requires accurate headers,
  a physical postal address and a working opt-out mechanism in the message itself; GDPR and PECR
  (EU/UK) require a lawful basis, disclosure of where the address came from, and erasure on request.
  The engine gives you the mechanics to honour all of that — a template footer is one line of copy,
  suppression is already wired, and a region gate is a worklist filter. It does not decide for you,
  and running it without those controls is a decision with legal consequences.

## Rollout (dry-run → live)
1. **Built in dry-run.** Verify selection, suppression, validation, cap, jitter, render against `outreach/send/dryrun/`. ✅ done — 50 B rows render, guards block.
2. **the operator writes copy** in `outreach/send/templates/ceiling_b.md` (delete the `[PLACEHOLDER]` block).
3. **One dry run reviewed** together on real selected rows.
4. **Enable the breaker**: add `gmail.readonly` to `GMAIL_SCOPES` in `gmail_auth.py`, `rm ~/.config/outreach-engine/token.json`, re-run `gmail_auth.py` to re-consent. Confirm `bounce_scan.py` runs. (Until this, the breaker has no signal — required before live.)
5. **Go live**: set `dry_run:false` + `template_approved:true`. First run with `--limit` small; watch `send_log` + bounces. Then install the launchd plist for hands-off cadence.

## Config surface (`outreach/send/config.json`)
```
from_address, from_name, reply_to           # identity (swap here to move domains)
dry_run: true                               # must be literally false to go live
template_approved: false                    # AND this true AND real copy
daily_cap: 100                              # flat, no ramp
jitter_seconds: [90, 240], send_windows: [["08:00","17:00"]]
bounce_rate_halt: 0.06, bounce_window: 50   # breaker: rate guard
bounce_burst: [4, 10]                       # breaker: >=4 hard bounces in last 10
segments: ["B_all_bot"], require_email_valid: true
worklist: outreach/worklist_ceiling.csv
```

## Auth setup (one-time, the operator) — done for gmail.send
Google Cloud project → enable Gmail API → OAuth consent (scopes `gmail.send` + `gmail.readonly`, add `sender@example.com` as test user) → Credentials → OAuth client ID → **Desktop app** → download → save as `~/.config/outreach-engine/client_secret.json`. First run opens a browser once → refresh token stored (out of repo, stable across branches). `pipeline/go.sh` grants `gmail.readonly` (bounce/opt-out scan) at arm time.

## Running as a service (24/7, autonomous) — see outreach/send/START-HERE.md
- **macOS TCC constraint (load-bearing):** a launchd agent cannot read `~/Documents` — it fails with "Operation not permitted" and the chain never runs. So only the *keep-awake* (which touches no files) is a launchd agent; the *chain* runs as a **detached nohup loop** (`engine_loop.sh`) started from a shell that has Documents access (Terminal). The loop survives closing Terminal but not logout/reboot — re-run `go.sh` after a reboot (idempotent; send_log is the memory, nothing double-sends).
- **`pipeline/install_engine.sh`** loads `ai.outreach.keepawake` (`caffeinate -dis` KeepAlive; `-i` holds idle sleep on battery too) and starts `engine_loop.sh` detached. After install the engine RUNS but HOLDS in dry-run.
- **`run_chain.sh`** per cycle: whole-cycle lock (atomic `mkdir`; take over only on a dead owner-PID, +8h backstop — never on a mtime shorter than a multi-hour send) → if `dry_run:true` heartbeat + exit → else discover + bounce-scan (≤hourly via `.last_poll`) + `send_outreach.py` (tz-windowed). STOP/HALT honored.
- **`pipeline/go.sh`** (run from Terminal) = arm: readonly consent → `dry_run:false`+`template_approved:true` → ensure loop running → fire once. **`stop.sh`** = STOP file. **`uninstall_engine.sh`** = stop loop + unload keep-awake, data kept.
- Committed config stays `dry_run:true`; going live is a local flip only, so a fresh checkout never sends. The keepawake plist hardcodes the main-checkout path.
- 6-day autonomy: keep on AC; `chain.log` grows only a few KB/day (loop owns the append fd — run_chain must not rotate it); daily discovery refills the queue; exhausted worklist idles cleanly ("nothing sendable").
