**New primitives introduced:** enablement-aware multi-signal `trade_decision_events` watchdog coverage; row-shape-safe dispatcher decision event payloads for snapshot-backed paper dispatchers.

# Design - Pending Trader-Surface Closeout 2026-05-27

## Scope

Implement item 8 only, and record why items 1-7 are already closed or gated. The build extends existing observability for snapshot-backed paper dispatchers without changing paper-trade admission, Telegram alerts, rankings, source selection, signal thresholds, live execution, sizing, or cross-identifier resolution.

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| SQLite freshness watchdog | none for gecko-alpha tables | Extend existing `scripts/check_trade_decision_events.py`. |
| Dispatcher decision event logging | none for repo-local trading dispatchers | Reuse `scout.trading.decision_events.emit_trade_decision`. |
| Alert qualification / ranking | out of scope and still gated | Do not design alerts or urgency tiers. |

Awesome-hermes-agent ecosystem check: no public Hermes skill replaces the repo-local SQLite and dispatcher instrumentation. Verdict: custom local patch is appropriate.

## Watchdog Design

`check_trade_decision_events.py` currently checks one pair: recent `gainers_snapshots` rows and recent `gainers_early` decision events. Extend it with a closed static mapping:

```python
SIGNAL_SOURCES = {
    "gainers_early": SignalSource(table="gainers_snapshots", timestamp_col="snapshot_at", enabled_setting=None),
    "losers_contrarian": SignalSource(table="losers_snapshots", timestamp_col="snapshot_at", enabled_setting="PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED"),
    "trending_catch": SignalSource(table="trending_snapshots", timestamp_col="snapshot_at", enabled_setting="PAPER_SIGNAL_TRENDING_CATCH_ENABLED"),
}
```

CLI:

- `--signals`: comma-separated signal list; default `gainers_early,losers_contrarian,trending_catch`.
- Disabled signals are reported under `skipped_disabled_signals` and do not cause failure.
- If `TRADING_ENABLED=False`, all signals are skipped with
  `status=trading_disabled`. Source rows are expected in this state because
  trackers can still run while paper dispatch is off.
- Unknown signal names exit with code 5 and JSON `status=unknown_signal`.
- The checker must not construct full `Settings()`: unrelated required fields
  such as Telegram/API keys should not turn this cron into a traceback. Read
  only the relevant boolean flags from process environment plus repo `.env`,
  applying the `Settings` defaults for absent keys.
- Missing DB remains code 4; SQLite error remains code 3; missing decisions remains code 2.
- Output JSON includes per-signal source and decision counts so a cron log can explain which signal failed.
- Source row counts mirror dispatcher eligibility by excluding tokens that
  already have an open `paper_trades` row for the same signal type. Otherwise
  the watchdog can false-positive when fresh snapshots are correctly skipped
  before the dispatcher loop because the trade is already open.

False-positive guard: if prod has tracker snapshots but `TRADING_ENABLED=False`,
or if `losers_contrarian` / `trending_catch` paper dispatch flags are false, or
if all fresh source rows are already represented by open paper trades, the
checker must return OK with skipped/idle detail. Source rows alone are not
enough to expect decision rows.

## Dispatcher Event Design

Generalize `_emit_dispatch_decision` so it accepts row-like `sqlite3.Row` objects with missing optional fields. The payload should use safe lookup for:

- `symbol`
- `name`
- `market_cap`
- `price_change_24h`
- `price_at_snapshot`
- `current_price`
- `market_cap_rank`

This prevents a `trending_catch` row from raising `KeyError` before reaching the fail-soft emitter.

Add `trade_losers()` event writes before existing `continue` branches:

- `junk_candidate`
- `missing_market_cap`
- `below_min_market_cap`
- `above_max_market_cap`
- `suppressed`

Add `trade_trending()` event writes before existing `continue` branches:

- `junk_candidate`
- `missing_market_cap_rank`
- `below_rank_threshold`
- `missing_market_cap`
- `below_min_market_cap`
- `above_max_market_cap`
- `suppressed`

Do not emit a `blocked/missing_price` event in this PR. Current
`trade_losers()` and `trade_trending()` can still call `engine.open_trade(...)`
with a missing entry price and let the engine make the existing decision. A
blocked dispatcher event would be misleading, and changing the branch to
`continue` would violate the non-behavior-change scope.

Repeated blocked rows may emit once per dispatcher cycle while their source
snapshot remains inside the five-minute query window. That is acceptable for
this table because the watchdog only requires recent row presence, not a
deduplicated audit ledger. Do not add dedupe in this PR; dedupe would require a
new identity/window policy and could hide a disconnected dispatcher.

## Tests

- Checker: enabled source rows with no decision rows fail for each of the three signals.
- Checker: disabled `losers_contrarian` / `trending_catch` source rows are skipped and return OK.
- Checker: unknown signal is a hard config error.
- Dispatcher: `trade_losers` writes blocked rows for mcap filters and suppression without changing open behavior.
- Dispatcher: `trade_trending` writes blocked rows for rank/mcap filters and suppression without changing open behavior.
- Regression: `trade_trending` event emission does not raise on missing `price_change_24h` / `price_at_snapshot` fields.
- Regression: missing entry-price paths are left to the existing engine path and
  do not emit false `blocked/missing_price` dispatcher rows.

## Non-Scope

No new DB table, migration, cron line, Telegram alert, alert qualification, ranking, urgency tier, cross-id resolver, signal-policy change, paper-trade policy change, source pruning, live execution, sizing, or paid/vendor call.

## Design Review Folds

- Dispatcher/instrumentation reviewer: removed ambiguous `missing_price`
  dispatcher events to avoid either false blocked rows or behavior changes;
  documented repeated blocked events as acceptable for freshness-watchdog
  semantics rather than adding dedupe.
- Watchdog/runtime reviewer: added global `TRADING_ENABLED=False` skip semantics
  and avoided full `Settings()` construction in the cron checker so unrelated
  config validation cannot crash the watchdog.
- PR docs/tests reviewer: added explicit `trending_catch` missing-decision
  checker coverage, loser/trending mcap decision-event assertions, a
  no-`missing_price` regression assertion, and cleaned stale cron README entry
  count wording.
