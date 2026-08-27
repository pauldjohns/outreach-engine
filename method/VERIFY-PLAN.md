# Plan v3: Bouncer verification as its own chain step

Supersedes v1 (withdrawn: fail-closed gate over a column `apollo_pull` strips hourly) and v2
(withdrawn: see below). 2026-07-21.

Answers the question directly: **yes, Bouncer runs on every lead before we email it, as a standing
step in the chain.** v3 changes only where that step lives and what it is allowed to do.

## Why v2 was withdrawn

1. **A vendor error would have permanently burnt domains.** v2's rule was "unrecognised statuses
   default to the non-sending branch." The adapter returns `error` as a verdict on HTTP 402/401
   (`verify_bakeoff.py:83-92`, `:107-108`), so a credit exhaustion mid-run would have written
   `status="skipped"` across the batch. `existing_state()` (`apollo_pull.py:239-254`) reads every
   row regardless of status, so those companies leave the addressable universe forever, and
   `verify_at_sourcing: false` does not undo it. Worse than the v1 defect.
2. **The accept-all rule was priced on nothing.** v2 demoted `deliverable`+`acceptAll=yes` to risky
   and costed it at 5%. `verify_bakeoff.py:252` persists only `{esp, verdict, raw}` — `acceptAll`
   was never recorded. Measured against Apollo's free `email_domain_catchall` on the 127 queued
   rows covered by raw JSON: **28.3% overall, 46.8% of Google, 7.5% of M365.** Bouncer's abstention
   ran the other way (0% Google, 10% M365). The two disagree systematically and the rule would land
   on Google, which is 172 of the 400 queued rows.
3. **Verification after the round loop under-delivers every run.** The loop exits at
   `len(rows) >= a.target` (`apollo_pull.py:324`, `:391`); culling afterwards means `--target 60`
   writes ~51, and `rate` at `:331` never learns, so the over-request maths stays wrong.
4. **Terminal skip on `risky` contradicts the decision log.** `DECISIONS.md:240-241` already settled
   on quarantine, not deletion. Reversing it on n=2 repeats the exact error `config.json:56`
   records — a binary action on a small-sample signal.

## The v3 shape

Verification is **its own step in `run_chain.sh`**, between `validate_emails` (`:63`) and
`bounce_scan` (`:65`). Same cycle, so nothing reaches `send_outreach.py` (`:73`) unverified.

This deletes v2's two hardest problems outright:

- **No Apollo credits at risk.** Verification no longer sits inside a transaction that has already
  spent them, so there is nothing to preflight and no all-or-nothing abort.
- **A 402 costs nothing.** Rows simply stay unverified and get picked up next cycle. Worst case on
  a Bouncer outage is one cycle of today's behaviour — degradation to status quo, not a new
  failure mode.

Still true from v2: zero changes to `send_outreach.py` (`DECISIONS.md:5-11`), no gate that can fail
closed, `upsert()` still owns `COLS`, and blank status still means genuinely sendable so
`run_chain.sh:52` stays honest.

## `pipeline/verify_queue.py`

Modelled on `queue_cleanup.py`, which already has the right shape: blank-status rows only (`:52-54`),
dated notes (`:62`), atomic `.tmp` + `os.replace` (`:69-72`), dry-run by default (`:75`).

- Imports `v_bouncer` from `verify_bakeoff` — no `verify_api.py` extraction. That file's contents
  are load-bearing evidence; refactor when a third caller exists.
- Skips rows that already carry a `verify_status`. Idempotent and resumable, so an interrupted run
  costs nothing and never double-bills.
- `--limit N`, `--esp <list>`, `--dry-run` default.
- Re-reads the worklist and merges by `norm(email)` on every write, never rewriting from a stale
  snapshot.

### Partition

| verdict | `verify_status` written | `status` | rationale |
|---|---|---|---|
| `undeliverable` | `undeliverable` | `skipped`, note `verify_undeliverable` | well evidenced: Bouncer caught 3 of 4 known bounces, over-condemned 0 of 2 known-good |
| `deliverable`, not accept-all | `deliverable` | blank (sendable) | |
| `deliverable`, accept-all | `deliverable_acceptall` | **blank (sendable)** | recorded, not acted on — the rule is unmeasured and would hit ~47% of Google |
| `risky` / `unknown` | `risky` / `unknown` | **blank (sendable)** | reverses v2; matches `DECISIONS.md:240-241`; n=2 with 95% CI 1.4-16.5%, and ZeroBounce called one of those two valid |
| `error` / transport failure | **nothing written** | unchanged | retried next cycle; never terminal |

Only `undeliverable` is terminal. Everything else is recorded and measured. That is the whole
difference between v3 and v2, and it is the difference between a reversible and an irreversible bet.

## Columns

`VERIFY_COLS = ["verify_status", "verify_date"]`, appended to `COLS` but **kept out of
`DATA_COLS`** — `upsert()` refreshes `DATA_COLS` from incoming rows (`apollo_pull.py:288-289`), so
excluding them means a sourcing pass structurally cannot overwrite a verdict it did not produce.

Also add `email_check` to `COLS`: it is written by `validate_emails.py:121-125` and stripped hourly
by `DictWriter(fieldnames=COLS, extrasaction="ignore")` (`apollo_pull.py:294`), surviving only
because the next chain line recomputes it. Live bug, independent of this work.

Also add `email_domain_catchall` to `DATA_COLS` and `to_row()`, read from `rec` not `org` (present
on 206/206 records; absent from the organization sub-object on all 400). Free second read on the
catch-all question the partition will eventually turn on.

Do **not** attempt a `refreshed` backfill: Apollo returns `last_refreshed_at` on 79/206 records.

## Cap: unchanged at 100 (a configuration choice)

An earlier draft proposed pinning `daily_cap` to 50 during the verification period. a configuration choice:
leave it at 100 and let the adaptive throttle scale it as designed.

The consequence still holds and is accepted, not forgotten: verification removes exactly the
failures `throttle_factor()` (`send_outreach.py:420-433`) can observe, so the multiplier climbs
toward 1.0 and the effective cap returns to 100 within roughly three days -- driven by the removal
of the measurable failure mode, not by evidence that inbox placement improved. `DECISIONS.md:63-65`
and `:250-256` already record that quarantining is invisible here. Watch reply rate and the Google
vs M365 slices rather than the bounce rate as volume rises.

## Sequence

**1. Today, no verification yet.** `COLS` fix (`verify_status`, `verify_date`, `email_check`) +
`email_domain_catchall` capture. Multi-writer round-trip test: `upsert` → `validate_emails --write`
→ `_mark_sent` → `queue_cleanup --apply` → `upsert`, asserting a stable column set and no lost
values. Commit `outreach/send/verify_bakeoff.json` into `method/` — it is currently **untracked**
and is the only machine-readable calibration record.

**2. Build `verify_queue.py`.** Tests offline with a mocked client.

**3. The 60 unsampled rows first** (`other` 41, proofpoint 8, barracuda 5, mimecast 5, none 1).
~$0.50. This is where the evidence is worst: `other` holds `example-one.com`, the one address neither
vendor resolved, and `example-eleven.com` (barracuda) answered `250 OK` to gibberish in the local probe.
If those strata abstain at 30-40% while M365/Google abstain at 5%, a uniform rule would quietly
delete a stratum — worth fifty cents to find out.

**4. The 340.** Dry-run, hand-review the cull list, apply. ~$2.70. Hold `STOP` across verify →
review → apply, so the engine does not send ~50 unculled rows during the review window.

**5. Wire into `run_chain.sh`** behind `verify_enabled`, between `:63` and `:65`.

**6. A week of sends, then set the partition from real outcomes.** With `verify_status` on every row
and `bounce_scan` running, bounce rate splits by verdict class: deliverable, deliverable_acceptall,
risky, unknown. That answers "should risky be terminal" and "should accept-all be demoted" from ~400
of this campaign's own rows instead of two addresses and a vendor benchmark. Record in
`DECISIONS.md`; add terminal rules then, if the data says so.

## Instrumentation (with step 5)

- `metrics.py`: split `queue["skipped"]` by reason (`:133-134` currently collapses ICP culls,
  cleanup culls and verification culls into one integer), add a verification tile, add
  `verify_status` as a slice in `bounce_slices` (`:169-172`).
- Fix `metrics.py:260`, which hardcodes `days_left = unsent/100` and is 2x optimistic under throttle.
- Cull-rate tripwire in `verify_queue.py`: if n>=20 and the undeliverable rate exceeds a ceiling,
  abort without writing and print loudly. Catches a bad key, a changed status vocabulary, or a
  vendor incident before it culls the queue.

## Known costs, accepted

1. Verification blinds the throttle's only observable signal; the cap pin is the compensating control.
2. ~10% of leads become `skipped`, so sustaining volume needs ~15% more Apollo sourcing. Apollo is
   the dominant unit cost (~2.9 credits/lead), not Bouncer (~$0.007).
3. Marking a bad address `skipped` locks its **company** out permanently via `seen_companies`
   (`apollo_pull.py:248`, consumed free at `:171-173`). ~40 companies over the current queue. Real,
   and the reason only `undeliverable` is terminal.

## Not doing

`verify_api.py` extraction; the credit preflight; `verify_unresolved_action` config; the batch
endpoint (every calibration number came from the single endpoint — measure batch before switching).
