# Profit Pattern Findings - 2026-05-19

Primary cohort: production `scout.db`, closed paper trades opened since
`2026-05-01 14:06:00` UTC.

Verification command:

```bash
python3 /tmp/analyze_profit_patterns.py --db scout.db --since '2026-05-01 14:06:00' --min-n 5 --limit 30
```

## Data Coverage

- Closed trades analyzed: 531.
- Current-regime result: +$1,545.85 net, +$2.91/trade, 58.8% win.
- All-time closed trades: 1,396, +$668.52 net, +$0.48/trade, 49.8% win.
- Market cap coverage is usable: 527/531 current-regime trades.
- Liquidity is not usable: 531/531 current-regime trades bucketed `liq:unknown`.
- X handle is not usable for trade P&L: `paper_trades` has `x_handle:unknown` for all 531 trades.
- X alert dashboard outcome path is also not usable yet: 215 X alerts, 0 priced, all unresolved/no `resolved_coin_id`.
- TG channel is not statistically usable: only 4 TG signals linked to paper trades, 2 closed in current regime, both losses.

## Top Profitable Patterns

Current-regime signal types:

| Pattern | n | Net P&L | Avg P&L | Win |
|---|---:|---:|---:|---:|
| `narrative_prediction` | 78 | +$1,294.96 | +$16.60 | 65.4% |
| `chain_completed` | 16 | +$1,123.15 | +$70.20 | 68.8% |
| `volume_spike` | 28 | +$593.88 | +$21.21 | 64.3% |

Current-regime market cap:

| Pattern | n | Net P&L | Avg P&L | Win |
|---|---:|---:|---:|---:|
| `mcap:10-50m` | 265 | +$1,446.85 | +$5.46 | 57.0% |
| `mcap:10-50m+` | 168 | +$478.56 | +$2.85 | 63.1% |

Useful cross-cells:

| Pattern | n | Net P&L | Avg P&L | Win |
|---|---:|---:|---:|---:|
| `narrative_prediction` + `mcap:10-50m` | 49 | +$895.54 | +$18.28 | 61.2% |
| `volume_spike` + `mcap:10-50m` | 10 | +$242.29 | +$24.23 | 70.0% |
| `chain_completed` + `mcap:10-50m+` | 9 | +$357.08 | +$39.68 | 88.9% |
| `gainers_early` + `mcap:10-50m+` | 77 | +$310.06 | +$4.03 | 62.3% |

Exit/outcome pattern, not an entry feature:

| Peak giveback bucket | n | Net P&L | Avg P&L | Win |
|---|---:|---:|---:|---:|
| `<5pp` | 139 | +$8,655.72 | +$62.27 | 100.0% |
| `5-15pp` | 140 | +$1,715.77 | +$12.26 | 86.4% |

## Worst Junk Patterns

Current-regime signal types:

| Pattern | n | Net P&L | Avg P&L | Win |
|---|---:|---:|---:|---:|
| `losers_contrarian` | 146 | -$803.22 | -$5.50 | 54.1% |
| `gainers_early` | 252 | -$382.93 | -$1.52 | 59.1% |
| `trending_catch` | 7 | -$242.82 | -$34.69 | 28.6% |

Bad cross-cells:

| Pattern | n | Net P&L | Avg P&L | Win |
|---|---:|---:|---:|---:|
| `losers_contrarian` + `mcap:10-50m` | 74 | -$716.08 | -$9.68 | 50.0% |
| `gainers_early` + `mcap:5-10m` | 49 | -$701.77 | -$14.32 | 57.1% |
| `gainers_early` + `confluence:3` | 37 | -$468.14 | -$12.65 | 32.4% |
| `trending_catch` + `mcap:10-50m+` | 5 | -$239.98 | -$48.00 | 20.0% |

Exit/outcome junk:

| Peak giveback bucket | n | Net P&L | Avg P&L | Win |
|---|---:|---:|---:|---:|
| `no_peak_<5` | 99 | -$6,090.86 | -$61.52 | 5.1% |
| `unknown` | 48 | -$2,938.30 | -$61.21 | 0.0% |
| `15-30pp` | 61 | -$458.95 | -$7.52 | 37.7% |
| `30-60pp` | 38 | -$444.43 | -$11.70 | 47.4% |

## Actionability Gate v1

This should gate "actionable / live-eligible paper" separately from exploratory
paper collection.

1. Allow actionable by default for:
   - `narrative_prediction`
   - `chain_completed`
   - `volume_spike`
2. Require usable market cap at entry.
   - Strong bucket: `10-50m`.
   - Acceptable current-regime bucket: `>50m`, but keep a dashboard watch because all-time `>50m` was negative.
   - Block or exploratory-paper only: unknown mcap, `<1m`, and `5-10m` unless the signal-specific exception below applies.
3. Signal-specific exceptions:
   - `gainers_early`: block `mcap:5-10m`; block `confluence:3`; only consider `mcap:10-50m+` as actionable, and treat `10-50m` as weak/observe because it is only +$8.78 over 126 trades.
   - `losers_contrarian`: block `mcap:10-50m` and `>50m`; if kept, restrict to `mcap:5-10m` exploratory paper until n>=50 confirms the +$172.92 current-regime cell.
   - `trending_catch`: exploratory paper only until n>=20 and net positive.
   - `tg_social`: exploratory paper only until each channel has n>=20 closed trades and positive net.
4. Do not use raw source confluence as a positive gate yet.
   - Current-regime `confluence:1`: +$1,960.51, n=492.
   - Current-regime `confluence:3`: -$468.14, n=37.
5. Exit overlay:
   - If `peak_pct < 5%` after the selected early checkpoint, close or cut size aggressively.
   - If `peak_pct` is unknown/stale, treat the trade as telemetry-broken and do not let it ride full-duration.
   - Tune trails toward preserving giveback <=15pp; buckets above 15pp are net negative.

## Dashboard Fields Needed

- `signal_type`
- `detected_by_combo` and parsed combo parts
- `source_confluence_count`
- `mcap_at_entry` and `mcap_bucket`
- `liquidity_at_entry` and `liquidity_bucket`
- `token_age_days`, `first_seen_at`, `freshness_minutes`, and freshness bucket
- `peak_pct`, `pnl_pct`, `peak_giveback_pp`, and giveback bucket
- `exit_reason`, `status`, `opened_at`, `closed_at`
- `x_handle`, `tweet_id`, `resolved_coin_id`, `x_outcome_status`, X entry/current price, and $300 notional P&L
- `tg_channel`, `tg_resolution_state`, `tg_message_pk`, `posted_at`, `paper_trade_id`, `mcap_at_sighting`
- Coverage badges per cohort: mcap known %, liquidity known %, X priced %, TG linked %

## Paper-Trade Rule Changes

1. Split paper trades into `actionable=true/false`; keep broad exploration, but only the Actionability Gate cohort should be used for live-readiness decisions.
2. Add `actionability_reason` JSON/list so blocked patterns are auditable.
3. Persist the requested segmentation fields at trade-open time instead of reconstructing later from side tables.
4. Add a `peak_stale` / `peak_unknown_exit` rule: unknown peak is currently a loss bucket, not neutral telemetry.
5. Continue collecting X/TG data, but do not rank X handles or TG channels by profitability until outcome linkage is fixed.
