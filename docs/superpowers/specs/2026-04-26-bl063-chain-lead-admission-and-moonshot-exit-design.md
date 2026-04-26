# BL-063 — Chain-Lead Admission + Moonshot Exit

**Status:** Design (pending review)
**Date:** 2026-04-26
**Author:** Claude (with srilu)

## Problem statement

The "Early Catches" dashboard shows a 91.3% hit rate and 184h average lead — chain detection is working. Yet 7d net paper-trade P&L is roughly **-$32** despite the underlying tokens posting massive moves (BSB +191%, MAGA +53%, KAT +201%, SPK +164%, CHIP +146%).

Audit of prod VPS data identified two structural leaks:

1. **Late entry.** For 11 chain-detected winners, the average paper-trade entry price is **+30–80% above the chain-detection price**. Paper trading fires off CoinGecko-derived signals (`gainers_early`, `first_signal`, `trending_catch`) that activate hours/days after chain sees on-chain accumulation. Chain detection is only logged to `trending_comparisons` — it does not trigger trades.
2. **Early exit.** Paper trades' `peak_pct` averages 20–55% on these tokens vs the underlying token peak gain of 53–200%. The BL-061 ladder's TP (+20) and trailing (~15-21pp drawdown) cap the upside.

The single MAGA case where entry was at detection (-0.3% premium) captured 37 of 53 points (70%). The thesis is that closing both leaks recovers a large fraction of the move on the upper-decile runners.

## Goals

- **Primary:** raise the **tail-capture ratio** (`paper_peak_pct / token_peak_gain_pct`) on chain-detected runners from ~25% baseline to ≥60% on the test set.
- **Secondary:** maintain or improve net 7d paper P&L vs status quo.
- **Non-goal in this PR:** changes to existing signal-types' exit ladders; live-trading wire-up beyond `would_be_live` accounting.

## Approach

Two coordinated changes shipped behind independent flags.

### A. Chain-lead admission

Wire `scout/chains/tracker.py` to emit a structured signal that `scout/trading/paper.py` consumes as a new `signal_type='chain_lead'`. Today the tracker only writes to `chain_matches` / `trending_comparisons.chains_detected_at`. After this change, when a chain pattern fires the token is admitted to paper trading subject to the same gates other signal types use.

### B. Moonshot exit upgrade

When an open paper trade's `peak_pct` crosses `PAPER_MOONSHOT_THRESHOLD_PCT` (default +40), upgrade its exit ladder in place: cancel the fixed TP, widen the trailing-stop drawdown to `PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT` (default 30pp from peak), and optionally take a partial leg. This is per-trade and applies to ALL signal types — but we expect chain_lead trades to benefit most because they enter early enough to actually peak past the threshold.

## Architecture

```
chains/tracker.py.check_chains()
   └─ on pattern fire (existing) → NEW: emit_chain_lead_signal(token, pattern)
        └─ admission gates:
             - safety check (existing scout.safety)
             - junk filter (existing in paper.py)
             - mcap caps (existing PAPER_MIN/MAX_MCAP_USD)
             - BL-062 signal-stacking gate (existing)
             - 7d re-entry suppression (NEW guardrail)
             - PAPER_CHAIN_LEAD_ENABLED flag
        └─ trading/paper.py.execute_buy(signal_type='chain_lead', ...)
              └─ writes paper_trades row
              └─ would_be_live computed by existing BL-060 logic
              └─ signal_combo = 'chain_lead' or 'chain_lead+<pattern_name>'

paper price-tick loop (existing in paper.py)
   └─ on each price update for an open trade:
        if PAPER_MOONSHOT_ENABLED
          and peak_pct >= PAPER_MOONSHOT_THRESHOLD_PCT
          and moonshot_armed_at IS NULL:
            moonshot_upgrade(trade):
              moonshot_armed_at = now()
              original_tp_pct, original_trail_drawdown_pct ← preserve
              tp_price = +inf  (fixed TP disabled)
              trail_drawdown_pct = PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT
              if PAPER_MOONSHOT_PARTIAL_FRACTION > 0:
                close leg_1 at (PAPER_MOONSHOT_PARTIAL_FRACTION × quantity)
```

## Schema migration

In `scout/db.py`, new migration adding to `paper_trades`:
- `moonshot_armed_at TEXT NULL`
- `original_tp_pct REAL NULL`
- `original_trail_drawdown_pct REAL NULL`
- index `idx_paper_trades_moonshot ON paper_trades(moonshot_armed_at) WHERE moonshot_armed_at IS NOT NULL`

Migration follows the **mid-flight flag migration** pattern: nullable, pre-cutover rows = NULL, A/B scoped to post-cutover only. The `CREATE INDEX` lives in the migration step, NOT in `_create_tables` (per BL-060 DDL lesson).

## Configuration

```python
# scout/config.py additions
PAPER_CHAIN_LEAD_ENABLED: bool = False
PAPER_CHAIN_LEAD_REENTRY_SUPPRESS_HOURS: int = 168  # 7 days
PAPER_MIN_CHAIN_PATTERN_STRENGTH: float = 0.0  # picked from fit set during rollout
PAPER_MOONSHOT_ENABLED: bool = False
PAPER_MOONSHOT_THRESHOLD_PCT: float = 40.0
PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT: float = 30.0
PAPER_MOONSHOT_PARTIAL_FRACTION: float = 0.0
```

Validators reject invalid combinations (e.g., trail ≥ 100, partial ∉ [0, 1]).

## Backtest harness

`scripts/backtest_chain_moonshot.py` — standalone, no live network calls.

- Reads 14d cohort from `trending_comparisons` joined with `price_cache` and existing `paper_trades`
- Simulates each token under four policies: `status_quo`, `chain_lead_only`, `moonshot_only`, `both`
- Splits **fit set** Apr 12-19 / **test set** Apr 20-26
- Slippage-aware: `entry_px = detection_price * (1 + 30bps)`
- Output JSON: total P&L, win rate, **tail capture ratio** (`paper_peak / token_peak`), max drawdown, per-token rows
- Used to pick `PAPER_MIN_CHAIN_PATTERN_STRENGTH`, `PAPER_MOONSHOT_THRESHOLD_PCT`, `PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT`

## Testing

- `tests/test_chain_lead_admission.py` — admission gates: safety pass/fail, junk filter, re-entry suppression, mcap caps, BL-062 stacking, flag off
- `tests/test_moonshot_upgrade.py` — upgrade fires once at threshold, preserves originals, modifies trail correctly, idempotent, doesn't fire below threshold, doesn't fire when disabled
- `tests/test_paper_chain_lead_integration.py` — end-to-end: chain pattern fires → `execute_buy` called → moonshot arms → trailing exit
- `tests/test_backtest_harness.py` — deterministic output on fixture cohort
- `tests/test_db_migration_chain_lead_moonshot.py` — migration adds columns idempotently, pre-cutover rows = NULL
- All existing `paper_trade` tests must pass unchanged

## Rollout

1. Merge with `PAPER_CHAIN_LEAD_ENABLED=False`, `PAPER_MOONSHOT_ENABLED=False`, schema migrated
2. Run backtest on prod 14d cohort, pick params, document in PR comment
3. `PAPER_CHAIN_LEAD_ENABLED=True` on prod (paper-only impact)
4. Soak 7d, monitor `combo_performance` row for `chain_lead`
5. `PAPER_MOONSHOT_ENABLED=True`
6. Soak another 7d, review tail capture + net P&L
7. Decision: if test-set tail capture ≥2× baseline AND net P&L ≥ status quo → keep on; else iterate or revert

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Chain signals noisy → many losers from `chain_lead` | Strict `PAPER_MIN_CHAIN_PATTERN_STRENGTH` from fit set; 7d re-entry suppression; default-OFF flag |
| Moonshot round-trips a +40% peak that dumps -50% | Backtest the (threshold, trail) combo on full cohort; optional partial-exit leg; flag-gated |
| Backtest optimistic vs live (slippage, latency) | Apply 30bps slippage; treat 7d prod soak as ground truth, not backtest |
| `would_be_live` capital constraint excludes good chain_lead trades | FCFS-20 already ranks by score; chain_lead competes fairly; track separately in digest |
| Mid-flight flag migration breaks A/B | Nullable column, A/B scoped to post-cutover only (per BL-060 lesson) |
| Re-entry suppression too aggressive (skip legit follow-ons) | Configurable hours; backtest shows whether 7d / 24h / disabled is best |

## Open questions

- Is fixed `peak_pct` threshold the right moonshot trigger, or should it be momentum-aware (e.g., `vol_acceleration` × `peak_pct`)?
- Should chain_lead admission also feed `live_trades` shadow-mode, or paper-only as drafted?
- Is 7d re-entry suppression too aggressive vs 24h or 48h?
- Schema: separate `moonshot_*` columns or pack into existing `signal_data` JSON?
