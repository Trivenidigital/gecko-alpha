**New primitives introduced:** `trade_decision_events` SQLite table; `scout.trading.decision_events.emit_trade_decision`; `scripts/check_trade_decision_events.py` freshness watchdog; managed cron entry for trade-decision freshness logging.

# Trade Decision Event Log Plan

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Decision-support event logging | None found in Hermes Skills Hub that writes gecko-alpha trade gate decisions into the local SQLite audit path. | Build custom because the payload must bind to `paper_trades`, `signal_params`, and tracker snapshots. |
| Table freshness watchdog | No gecko-alpha-specific Hermes watchdog found. Hermes has DevOps/webhook and dashboard-adjacent skills, but not this DB SLO. | Build a small local checker paired with the new table. |
| Trader urgency/ranking | Not applicable to this PR. | Defer; this PR only instruments decisions. |

Awesome-hermes-agent ecosystem check: dashboard/process-monitoring projects exist, but they do not replace gecko-alpha's source-of-truth DB decision audit.

Verdict: use a narrow in-repo implementation; no Hermes skill owns this audit trail.

## Runtime / Drift Findings

- `signal_events` already exists, but cannot be reused because gainers/trending comparison code treats any `signal_events` row as chain detection evidence.
- `/api/trade_inbox` and `/api/live_candidates` remain downstream of open `paper_trades`; disabled paper signals have no row to inspect.
- Prod attribution for `gainers_early` supports the current disabled state: after the 2026-05-13 KEEP_ON audit and before the 2026-05-19 hard-loss suspend, prod had `123` closed `gainers_early` rows, `-$2,262.70` net, with stop-loss alone at `32` rows / `-$2,632.15`.

## Scope

1. Add an append-only `trade_decision_events` table with indexes for token, signal, decision/reason, and created time.
2. Add a fail-soft emitter used by trading dispatch code.
3. Instrument `trade_gainers` pre-engine filters and `TradingEngine.open_trade` admission decisions.
4. Add a freshness checker that fails only when recent `gainers_snapshots` exist but no recent `gainers_early` decision events exist.
5. Add a managed cron entry so the checker is observed on the same schedule class as other repo-tracked cron watchdogs.
6. Record this task and the attribution result in `tasks/todo.md`.

## Non-Scope

- Do not re-enable `gainers_early`.
- Do not promote tracker rows into Trade Inbox yet.
- Do not add Telegram alerting; the initial watchdog is scheduled log output.
- Do not implement urgency classification.

## Verification

- New unit tests for migration, emitter fail-soft behavior, engine decisions, and gainers pre-engine filters.
- Red/green TDD on the new tests.
- Targeted pytest for trading decision events and existing trading engine/signal coverage.
