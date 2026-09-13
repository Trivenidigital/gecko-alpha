# Historical recorded stop-shortfall baseline — 2026-09-13

**FINDINGS ONLY — EXPERIMENTAL, not for pruning, sizing, execution or dispatch changes.**

Existing data supports a narrow descriptive V1 baseline: **23 paper stop exits
across 22 tokens**, July 10 through August 9. All retained entry stops were 25%.
Mean recorded exit-price shortfall beyond that entry stop is **1.347 percentage
points**, median **1.131 pp**, range **0.394–3.765 pp**. This is not measured
execution slippage, an expectation for future trades, or evidence to revive a
signal. The cohort contains 21 gainers_early and two volume_spike trades; it is
small, selected, historical, and includes one repeated token.

## Meaning and exclusions

- Recorded exit-price return = 100 × (exit_price / entry_price − 1).
- Entry-stop shortfall = max(0, −recorded exit-price return − entry stop pct).
- These compare stored paper prices and frozen entry-stop settings, not actual
  venue fills or realized portfolio loss. Stored exits already include modeled
  paper slippage (`scout/trading/paper.py:676`). No fee or latency attribution.
- Required: closed_sl; explicit market exit; non-legacy entry price source;
  close after price-provenance cutover; v1 frozen entry stop; positive entry,
  nonnegative exit; no conviction lock or ladder-leg timestamps. Verified for
  all retained rows: remaining_qty equals quantity, realized_pnl_usd is zero,
  and quantity × entry_price equals amount_usd within 1e-9 relative tolerance.
- 355 historical closed_sl records existed. Only 139 had frozen entry stop
  values (135 market + four stop_gap_model). Four modeled exits are excluded.
  The stricter screen below retains 23; the other 332 are not assessed by this
  metric, not declared bad trades.
- The migration backfilled otherwise-unclassified old exits to market
  (`scout/db.py:9811–9829`), so market alone is insufficient provenance.
  Price-provenance cutover was 2026-07-03T00:32:14.634856+00:00; legacy sources
  are excluded even when they closed later. Stop-gap-model repricing is at
  `scout/trading/evaluator.py:1266–1320` and must not be presented as observed loss.

## Read-only method and repeatable selection

Observed 2026-09-13T19:54:10.560207+00:00 on production baseline 6c56186e.
SQLite URI mode=ro plus PRAGMA query_only=ON. No vendor calls or DB writes.

```sql
SELECT p.id, p.token_id, p.signal_type, p.entry_price, p.exit_price,
       p.amount_usd, p.quantity, p.remaining_qty, p.realized_pnl_usd,
       p.closed_at, s.entry_snapshot_version, s.sl_pct_at_entry
FROM paper_trades p
JOIN paper_trade_entry_snapshots s ON s.paper_trade_id = p.id
WHERE p.status = 'closed_sl'
  AND p.exit_provenance = 'market' AND p.price_source != 'legacy'
  AND julianday(p.closed_at) >= julianday(
    (SELECT cutover_ts FROM paper_migrations WHERE name = 'price_provenance_v1'))
  AND s.sl_pct_at_entry IS NOT NULL
  AND p.entry_price > 0 AND p.exit_price >= 0
  AND p.conviction_locked_at IS NULL
  AND p.leg_1_filled_at IS NULL AND p.leg_2_filled_at IS NULL
ORDER BY p.closed_at DESC;
```

Validate snapshot version and quantity/realized conditions above before
aggregating; they were verified on the 23 returned rows. Use unrounded prices
for arithmetic. Do not relabel this query as an all-history or per-source rank.

## Recorded observations

| Trade ID | Token | Closed (UTC) | Entry stop % | Exit-price return % | Shortfall pp |
|---:|---|---|---:|---:|---:|
| 2911 | stonk-3 | 2026-08-09 | 25.0 | -25.868 | 0.868 |
| 2913 | siren-2 | 2026-08-08 | 25.0 | -27.274 | 2.274 |
| 2910 | cysic | 2026-08-08 | 25.0 | -26.801 | 1.801 |
| 2872 | shibinhood | 2026-08-07 | 25.0 | -25.572 | 0.572 |
| 2855 | tomochain | 2026-08-06 | 25.0 | -26.342 | 1.342 |
| 2865 | teh-epik-duck | 2026-08-06 | 25.0 | -26.131 | 1.131 |
| 2867 | wen-lambo-2 | 2026-08-06 | 25.0 | -26.503 | 1.503 |
| 2862 | siren-2 | 2026-08-06 | 25.0 | -27.160 | 2.160 |
| 2838 | manifesting | 2026-08-06 | 25.0 | -27.555 | 2.555 |
| 2834 | what-if-3 | 2026-08-04 | 25.0 | -25.822 | 0.822 |
| 2824 | zircuit | 2026-08-04 | 25.0 | -25.936 | 0.936 |
| 2811 | catecoin | 2026-08-04 | 25.0 | -26.120 | 1.120 |
| 2769 | chill-guy | 2026-07-28 | 25.0 | -25.661 | 0.661 |
| 2806 | stonkbroker | 2026-07-27 | 25.0 | -25.563 | 0.563 |
| 2780 | cupsey-2 | 2026-07-26 | 25.0 | -26.573 | 1.573 |
| 2771 | bitmart-token | 2026-07-26 | 25.0 | -27.262 | 2.262 |
| 2760 | corn-3 | 2026-07-25 | 25.0 | -25.446 | 0.446 |
| 2699 | projectvex | 2026-07-22 | 25.0 | -26.737 | 1.737 |
| 2710 | bless-2 | 2026-07-22 | 25.0 | -26.492 | 1.492 |
| 2657 | ava-ai | 2026-07-20 | 25.0 | -25.576 | 0.576 |
| 2660 | zerebro | 2026-07-20 | 25.0 | -25.394 | 0.394 |
| 2643 | vulcan-forged | 2026-07-12 | 25.0 | -25.432 | 0.432 |
| 2639 | owlto-finance | 2026-07-10 | 25.0 | -28.765 | 3.765 |

## Next allowed step

DASH-09's interactive closed-trade column remains unbuilt. This report advances
its descriptive evidence contract without claiming a dashboard shipped.
`dashboard/db.py:get_trading_history` currently omits stop/snapshot fields;
`_outcome_integrity` is not a strict eligibility predicate because unknown
provenance defaults to priced. A future read-only slice should expose explicit
exclusion reasons, distinguish entry versus terminal stop, and retain modeled
and unavailable states without silently dropping rows. This descriptive
contract needs no new forward soak; calibration or actual execution-slippage
claims require their own evidence. Zero closed trades occurred in the latest
30 days; no dispatch change was made to manufacture new observations.
