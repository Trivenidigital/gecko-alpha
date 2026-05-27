**New primitives introduced:** NONE

# Eligible Backlog Finish Design - 2026-05-27

## Goal

Produce a no-runtime-change PR that makes backlog/todo state match current repo and prod state. The PR closes stale active-work tails for already-merged work, corrects stale PR status, and pins blocked/re-scope gates so future sessions do not rebuild or prematurely implement parked items.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| backlog triage / scheduled reminders | yes - Hermes automation templates include backlog triage and cron delivery | repo-local backlog remains source of truth; no Hermes job added in this docs-only PR |
| tracker-promotion soak reminders | yes - Hermes cron can run script-only reminders | record next-check/backstop now; add scheduler only if the gate rots after this PR |
| narrative CA re-resolution | none found for local CA-to-CoinGecko reconciliation over gecko-alpha SQLite tables | keep custom blocked until a canonical resolver exists |
| held-position stale alert | generic Hermes notifications exist, no project-specific stale-price evaluator | do not alert while 7d baseline stays below threshold |

Awesome-Hermes-agent ecosystem check: generic automation exists, but no ecosystem primitive can authoritatively mutate gecko-alpha backlog state or infer canonical coin IDs from local CA rows. Build from repo/runtime evidence.

## Files

- Modify `backlog.md`
  - Correct PR #33 from stale "open/design-review-required" to closed/superseded.
  - Correct PR #280 from "close rather than merge" to closed/superseded.
  - Mark `BL-NEW-SIGNAL-TRUST-ROADMAP` scorecards as shipped via PR #289, not open/rebase-needed.
  - Mark Trade Inbox counter-risk context as shipped via PR #290 and PR #278 closed/superseded.
  - Update the snapshot header from "through PR #287" to "through PR #294".
  - Update `BL-NEW-TG-ALERT-QUALIFICATION-DESIGN` with current soak rows, 2026-06-09 backstop, and the finding that the current SQL cannot by itself prove a three-mature-day unlock because it scans only the last 36h and suppresses against current open paper rows.
  - Update `BL-NEW-HERMES-NARRATIVE-DEFERRED-RESOLUTION-SWEEP` to `BLOCKED-CANONICAL-ID / SOURCE-CALL-IDENTITY-RESOLUTION` with runtime evidence.
  - Update `BL-NEW-HELD-POSITION-STALE-COUNT-ALERT` to `BASELINE-MEASURED / BELOW-SUGGESTED-THRESHOLD / OPERATOR-THRESHOLD-PENDING` with 7d evidence and no alert implementation.
  - Keep Track 3 items as `RE-SCOPE-ELIGIBLE`, not build-ready.
- Modify `tasks/todo.md`
  - Mark this cycle's checklist as completed through docs/status build.
  - Close stale active work entries for Signal Trust and Trade Inbox counter-risk by replacing unchecked PR/deploy tails with shipped evidence.
- Add `tasks/findings_eligible_backlog_finish_2026_05_27.md`
  - Record all runtime commands/evidence.
  - Capture branch decisions and anti-scope.

## Status Semantics

Use these exact meanings:

- `SHIPPED`: code/runtime behavior landed on `origin/master`; deploy evidence exists or no deploy needed.
- `CLOSED-SUPERSEDED`: PR/backlog path is no longer actionable because a replacement merged or the operator closed it.
- `GATED`: a pre-pinned data/operator condition has not fired.
- `GATED / SOAK-METRIC-NOT-YET-AUDITABLE`: the feature remains gated and the current measurement query cannot prove the unlock condition by itself.
- `RE-SCOPE-ELIGIBLE`: the old gate cleared, but implementation still requires current-base drift/runtime triage.
- `BLOCKED-CANONICAL-ID / SOURCE-CALL-IDENTITY-RESOLUTION`: a plausible feature exists, but the data model lacks a safe canonical write target; fold into source-call identity work.
- `BASELINE-MEASURED / BELOW-SUGGESTED-THRESHOLD / OPERATOR-THRESHOLD-PENDING`: baseline was measured and did not breach the suggested threshold, but the final alert threshold remains an operator policy decision.

## Branch Decisions

### Signal Trust Scorecards

Current base includes PR #289:
- `/api/signal_trust/scorecards`
- scorecard models and DB helper
- frontend `SignalTrustTab.jsx` scorecard fetch/render
- focused tests
- prod smoke: srilu `/api/signal_trust/scorecards` returned HTTP 200 with `not_for_alerting=true` metadata while prod HEAD was at least PR #290 (`2e8cf69`).

Decision: mark the stale `BUILD VERIFYING` todo tail complete. No implementation remains.

### Trade Inbox Counter-Risk

Current base includes PR #290:
- counter-risk fields in Trade Inbox models
- paper-row latest-prediction enrichment
- tracker null/empty invariants
- UI context rendering
- contract and endpoint tests
- prod smoke: srilu `/api/trade_inbox?limit_per_group=2` returned HTTP 200; sampled rows include `counter_risk_score`, `counter_flags`, and `counter_risk_predicted_at`.

Decision: mark stale active-work tail complete and PR #278 closed/superseded. No implementation remains.

### TG Alert Qualification

Prod soak rows from `scripts/trade_inbox_tracker_promotion_soak.sql`:
- `2026-05-26|17`
- `2026-05-25|50`

Decision: keep `GATED / SOAK-METRIC-NOT-YET-AUDITABLE`. The pre-pinned gate requires three mature UTC days, but the current SQL scans only `datetime('now', '-36 hours')`, so a single run cannot prove three mature days. It also suppresses against current open paper rows, so historical counts can drift as paper trades open/close. Unlock requires either a widened/fixed SQL query that covers at least four UTC days and uses point-in-time paper state, or three recorded daily artifacts with `run_at`, SQL hash, result rows, and the current-open-paper caveat. The 14-day backstop remains `2026-06-09`.

### Held-Position Stale Alert

7d prod journal baseline:
- 3,878 `held_position_refresh_summary` rows
- time range `2026-05-20T01:42:52Z` to `2026-05-27T01:39:12Z`
- `stale_open_count` min/p50/max `2/4/5`
- `held_total` min/p50/max `125/139/150`
- threshold-exceed cycles `0`
- max consecutive threshold-exceed cycles `0`

Parser command: `journalctl -u gecko-pipeline --since '7 days ago' --no-pager`, JSON lines filtered to `event == "held_position_refresh_summary"`. Threshold semantics are strict `stale_open_count > max(5, 0.05 * held_total)`, not `>=`. The 3,878 rows across seven days prove the gauge was continuously observable enough for baseline measurement, though the service did experience CoinGecko backoff windows.

Decision: do not implement alert now. Keep item open as `BASELINE-MEASURED / BELOW-SUGGESTED-THRESHOLD / OPERATOR-THRESHOLD-PENDING`; implementation reopens if the operator chooses a lower threshold or future baseline breaches the chosen threshold. Preserve the existing decision-by/backstop status instead of treating "no alert now" as permanent closure.

### Hermes Narrative Deferred Resolution

Prod evidence:
- unresolved CA rows last 7d: 24
- unresolved CA rows all-time: 39
- resolved rows all-time: 0
- candidate matches last 7d: 3 rows, 1 distinct CA
- matching candidate has ticker/name, but no canonical `coin_id`
- `scout/api/narrative_resolver.py` returns `"coin_id": None` for `candidates` hits by contract.
- `PRAGMA table_info(candidates)` shows no `coingecko_id`; table audit found `coingecko_id` only on `second_wave_candidates`.
- `/api/coin/lookup` returns `found=true`, `coin_id=null`, `source=candidates` for a candidate hit under the current resolver contract.
- Hermes `~/.hermes/cron/jobs.json` shows `gecko-x-narrative-scanner`: `enabled=true`, `last_status=ok`, `last_error=null`, `last_run_at=2026-05-27T01:00:52.525883+00:00`.

Decision: do not write `resolved_coin_id` with contract address or ticker. Keep blocked as `BLOCKED-CANONICAL-ID / SOURCE-CALL-IDENTITY-RESOLUTION` until a canonical CA-to-CoinGecko-id resolver or source-call identity-resolution design exists.

### Track 3

`BL-NEW-X-OUTCOME-LINKAGE`, `BL-NEW-TG-OUTCOME-LINKAGE`, and `BL-NEW-NO-PEAK-RISK-HANDLING` are no longer blocked by actionability row-count. They are not build-ready in this PR because each needs stale-PR/current-base triage and runtime assumption verification.

Per-item re-scope gates:
- X outcome linkage: current-base drift check plus current unresolved/priced X counts.
- TG outcome linkage: current direct-FK/linkage-state counts and source-call overlap.
- No-peak risk handling: current-regime replay, peak/giveback coverage, and explicit `pre_entry_giveback_ratio IS NOT NULL` guard.

The actionability row-count finding only clears the old wait gate and authorizes triage, not implementation.

Decision: leave as `RE-SCOPE-ELIGIBLE`.

## Verification

Run:

```powershell
git diff --check
uv run pytest -q tests/test_signal_trust_scorecards_endpoint.py tests/test_signal_trust_registry_endpoint.py tests/test_trade_inbox_endpoint.py tests/test_check_trade_inbox_contract.py tests/test_dashboard_frontend_layout.py
```

Expected:

- Diff check clean.
- Focused shipped-surface tests pass.

## Deploy

No deploy is needed if the final diff remains docs/status only. If code changes are introduced during review folds, reclassify deploy requirements before merge.
