**New primitives introduced:** multi-signal trade-decision-event freshness watchdog coverage for `gainers_early`, `losers_contrarian`, and `trending_catch`; pre-engine dispatcher decision events for `losers_contrarian` and `trending_catch` filter/suppression outcomes. No new DB tables, no trading policy change, no Telegram alerting change.

# Plan - Pending Trader-Surface Closeout 2026-05-27

## Goal

Close all eight pending items that can be closed safely today. Items 1-5 are confirmation/memory hygiene and are already closed by PR #298 plus memory updates. Items 6-7 remain gated/re-scope-only. Item 8 is eligible for implementation: extend trade-decision-event instrumentation/watchdog beyond `gainers_early` so `losers_contrarian` and `trending_catch` decision-event drift cannot fail silently.

## Current Evidence

- Today's Focus action-language firewall already bans `act_now`, `act now`, `action_required`, `acting`, `now_tradeable`, and `tradeable_now` in copy and diagnostics.
- Prod `paper_trades.actionability_reason` audit returned only neutral machine labels.
- Today's Focus usage evidence is deployed as local-only sanitized JSON, with `notes_saved` counting first non-empty note creation per row.
- `feedback_anti_scope_as_runtime_contract.md` has both ratchet and meta-flag addenda.
- `feedback_closed_loop_smoke_against_motivating_cases.md` exists.
- PR #183 and #184 are merged docs/audit PRs, not stale open branches. Backlog marks their implementation as re-scope-eligible, not build-ready.
- `BL-NEW-TG-ALERT-QUALIFICATION-DESIGN` remains gated by tracker-promotion soak and measurement inputs.
- `scripts/check_trade_decision_events.py` currently compares recent `gainers_snapshots` to recent `gainers_early` decision events only.
- `scout/trading/signals.py` emits pre-engine decision events for `gainers_early` filters/suppression only; `losers_contrarian` and `trending_catch` can be filtered before `engine.open_trade()` without any decision event.

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| SQLite watchdog / freshness SLO | none applicable to this repo-local `trade_decision_events` table | Extend existing in-repo checker. |
| Trading dispatcher decision logging | none applicable to gecko-alpha signal dispatch internals | Reuse existing `emit_trade_decision`. |
| Telegram alert qualification | not in scope; still gated | No alert design or alert send changes. |

Awesome-hermes-agent ecosystem check: no drop-in skill owns gecko-alpha SQLite decision-event watchdogs. Verdict: use the existing local watchdog and fail-soft event emitter.

## Implementation Steps

1. Add tests showing `check_trade_decision_events.py` can check `gainers_early`, `losers_contrarian`, and `trending_catch` independently.
2. Extend `scripts/check_trade_decision_events.py` with a static source-table mapping:
   - `gainers_early` -> `gainers_snapshots.snapshot_at`
   - `losers_contrarian` -> `losers_snapshots.snapshot_at`
   - `trending_catch` -> `trending_snapshots.snapshot_at`
3. Add `--signals` CLI argument defaulting to all three signals, but make checks
   enablement-aware by reading only the relevant booleans from process
   environment plus repo `.env`, skipping signals whose paper dispatcher is
   intentionally disabled:
   - `gainers_early`: enabled; no separate paper-signal flag exists today.
   - `losers_contrarian`: skip when
     `PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED=False`.
   - `trending_catch`: skip when
     `PAPER_SIGNAL_TRENDING_CATCH_ENABLED=False`.
   The script returns `missing_recent_decisions` only for enabled checked
   signals with dispatch-eligible source rows but zero decision events. Source
   counts must exclude tokens already represented by an open paper trade for
   the same signal type, matching the dispatcher queries.
4. Generalize `_emit_dispatch_decision` to tolerate different row shapes via
   safe `row.get(...)` access or a dict-normalization helper. This is required
   because `trending_snapshots` rows do not have `price_change_24h` or
   `price_at_snapshot`; the emitter must remain fail-soft and must not raise
   before `emit_trade_decision`.
5. Emit fail-soft pre-engine decision events in `trade_losers()` for junk,
   missing/below/above mcap, and suppression.
6. Emit fail-soft pre-engine decision events in `trade_trending()` for junk,
   missing/low rank, missing/below/above mcap, and suppression.
7. Add dispatcher tests that assert rows are written for `losers_contrarian` and
   `trending_catch` filter/suppression paths. Existing open-count tests are not
   enough.
8. Keep trading behavior exactly unchanged. Event emission failure remains swallowed by `emit_trade_decision`.
9. Update cron docs to clarify the existing cron now checks enabled snapshot-backed signals.
10. Record items 6-7 as still gated/re-scope-only in `tasks/todo.md` rather than forcing policy work.

## Acceptance Checks

- Focused tests for decision events pass.
- Existing trading signal/engine tests pass.
- No new DB schema or cron line is introduced.
- The existing cron command remains valid; default checker coverage expands.
- No Telegram alert, signal policy, scoring, ranking, urgency tiers,
  cross-identifier resolver, source pruning, live execution, or sizing behavior
  changes.

## Plan Review Folds

- Product/scope reviewer: added explicit no-ranking, no-urgency-tier, and
  no-cross-id-resolver anti-scope.
- Code/runtime reviewer: made watchdog enablement-aware to avoid false
  positives when source snapshots are enabled but paper dispatch is disabled;
  required safe row-shape handling before using `_emit_dispatch_decision` for
  trending rows; added dispatcher-event tests to acceptance scope.
- PR runtime reviewer: made watchdog source counts mirror dispatcher open-trade
  exclusion and renamed the idle status to `idle_no_recent_source_rows`.
