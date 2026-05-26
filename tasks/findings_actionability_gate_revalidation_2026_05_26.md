**New primitives introduced:** NONE

# Findings - Actionability Gate Revalidation 2026-05-26

**Date:** 2026-05-26
**Scope:** Read-only prod validation of the actionability re-check gate.
**Verdict:** **CLEARED / no immediate implementation authorized.**

The primary descriptive gate is now met: `n_actionable_closed=55` and `n_exploratory_closed=16`. This updates the stale 2026-05-22 state (`21/3`). The gate is a revalidation trigger only; it does not authorize actionability v2, suppression, source-quality consumption, Telegram alert qualification, live dispatch, sizing, or capital allocation.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Paper-trade cohort validation | none found in Hermes bundled/optional skills catalog for project-local SQLite actionability stamps | Use read-only SQL against prod `scout.db`; no new runtime primitive. |
| Statistical actionability interpretation | none found for gecko-alpha-specific `paper_trades` PnL attribution | Keep as repo findings with explicit uncertainty and outlier checks. |
| Backlog gate status | Hermes can remember/schedule, but the canonical gate state lives in repo artifacts | Update `backlog.md` and `tasks/todo.md`; no Hermes job in this PR. |

Awesome-hermes-agent ecosystem check: no drop-in actionability audit skill or trading-signal cohort validator for this SQLite schema. Verdict: custom read-only evidence pass is justified.

## Runtime Safety Checks

Prod query used `sqlite3 -readonly scout.db` through the Windows SSH two-step. SQLite version was `3.45.1`, so window functions were available.

Migration marker:

| name | cutover_ts |
|---|---|
| `bl_new_actionability_gate_v1` | `2026-05-19T11:39:09.121422+00:00` |

Version inventory:

| actionability_version | actionable | rows | first_opened_at | latest_closed_at |
|---|---:|---:|---|---|
| `v1` | 0 | 37 | 2026-05-19T11:42:27Z | 2026-05-26T18:28:54Z |
| `v1` | 1 | 156 | 2026-05-19T12:34:52Z | 2026-05-26T20:13:46Z |

Anomaly checks all returned zero:

| check | count |
|---|---:|
| closed status missing `closed_at` | 0 |
| non-closed status with `closed_at` | 0 |
| closed stamped row missing `actionable` | 0 |
| closed stamped row missing `actionability_reason` | 0 |
| closed stamped row missing `pnl_usd` | 0 |
| closed stamped row missing `pnl_pct` | 0 |
| stamped row opened before cutover | 0 |

## Gate Cohort

Eligibility was pinned to `actionability_version='v1'`, `actionable IN (0,1)`, `status GLOB 'closed_*'`, and `closed_at IS NOT NULL`.

| Cohort | n_closed | total_pnl | avg_pnl | wins | losses | win_rate | max_win | max_loss | median_pnl |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Exploratory (`actionable=0`) | **16** | **-$385.63** | -$24.10 | 7 | 9 | 43.8% | +$38.60 | -$143.67 | -$1.73 |
| Actionable (`actionable=1`) | **55** | **+$335.53** | +$6.10 | 43 | 12 | 78.2% | +$203.31 | -$121.99 | +$20.56 |

Interpretation guardrail: exploratory `n=16` is above the minimum descriptive trigger but below `n=20`. That allows hypothesis leads only, not directional claims about classifier quality, expected PnL, or suppression.

## Outlier Checks

| Cohort | total_pnl | without max win | without max loss | without largest abs | without top-2 abs | top-3 abs share |
|---|---:|---:|---:|---:|---:|---:|
| Exploratory | -$385.63 | -$424.23 | -$241.96 | -$241.96 | -$119.79 | 42.4% |
| Actionable | +$335.53 | +$132.22 | +$457.52 | +$132.22 | +$254.21 | 21.2% |

Neither cohort flips sign after removing the largest absolute row or top two absolute rows. The actionable cohort remains positive after removing the largest win. The exploratory cohort remains negative after removing the largest loss and top two absolute rows, but because `n=16`, this remains descriptive only.

## Signal Split

| signal_type | actionable | n | total_pnl | wins | losses | max_win | max_loss |
|---|---:|---:|---:|---:|---:|---:|---:|
| chain_completed | 0 | 12 | -$227.92 | 7 | 5 | +$38.60 | -$143.67 |
| chain_completed | 1 | 23 | +$392.23 | 19 | 4 | +$203.31 | -$100.63 |
| narrative_prediction | 1 | 16 | +$49.42 | 12 | 4 | +$51.32 | -$76.40 |
| tg_social | 0 | 3 | -$80.06 | 0 | 3 | $0.00 | -$76.60 |
| volume_spike | 0 | 1 | -$77.66 | 0 | 1 | -$77.66 | -$77.66 |
| volume_spike | 1 | 16 | -$106.13 | 12 | 4 | +$29.56 | -$121.99 |

## Reason Split

| actionability_reason | actionable | n | total_pnl | wins | losses | max_win | max_loss |
|---|---:|---:|---:|---:|---:|---:|---:|
| `v1_block_core_signal_mcap_below_10m` | 0 | 13 | -$305.57 | 7 | 6 | +$38.60 | -$143.67 |
| `v1_block_tg_social_low_n` | 0 | 3 | -$80.06 | 0 | 3 | $0.00 | -$76.60 |
| `v1_pass_core_signal_mcap_10_50m` | 1 | 29 | +$50.88 | 22 | 7 | +$51.32 | -$121.99 |
| `v1_pass_core_signal_mcap_50m_plus` | 1 | 26 | +$284.65 | 21 | 5 | +$203.31 | -$94.04 |

## Exploratory Winners

All seven exploratory winners were `chain_completed` rows blocked by `v1_block_core_signal_mcap_below_10m`.

| id | symbol | pnl_usd | pnl_pct | exit | denominator |
|---:|---|---:|---:|---|---|
| 2293 | BELIEVE | +$38.60 | +12.87% | peak_fade | same signal/reason pair: 7 wins / 12 rows |
| 2392 | PARALOOM | +$37.74 | +12.58% | trailing_stop | same signal/reason pair: 7 wins / 12 rows |
| 2232 | SYS | +$32.18 | +10.73% | peak_fade | same signal/reason pair: 7 wins / 12 rows |
| 2223 | PAAL | +$31.45 | +10.48% | peak_fade | same signal/reason pair: 7 wins / 12 rows |
| 2257 | HMSTR | +$30.97 | +10.32% | peak_fade | same signal/reason pair: 7 wins / 12 rows |
| 2246 | MARAON | +$29.47 | +9.82% | peak_fade | same signal/reason pair: 7 wins / 12 rows |
| 2241 | ETHDYDX | +$29.09 | +9.70% | peak_fade | same signal/reason pair: 7 wins / 12 rows |

This is a real hypothesis lead: the below-$10M `chain_completed` block is not pure junk. But the same pair is still net negative at current `n=12`, with five losses large enough to dominate the seven small-to-medium wins. It should not trigger a classifier change in this PR.

## Exit Split

| Cohort | status / exit_reason | n | total_pnl | wins | losses |
|---|---|---:|---:|---:|---:|
| Exploratory | `closed_peak_fade` / `peak_fade` | 6 | +$191.75 | 6 | 0 |
| Exploratory | `closed_sl` / `stop_loss` | 6 | -$604.28 | 0 | 6 |
| Exploratory | `closed_floor` / `floor` | 2 | -$10.85 | 0 | 2 |
| Exploratory | `closed_expired` / `expired_stale_no_price` | 1 | $0.00 | 0 | 1 |
| Exploratory | `closed_moonshot_trail` / `trailing_stop` | 1 | +$37.74 | 1 | 0 |
| Actionable | `closed_peak_fade` / `peak_fade` | 29 | +$1,025.29 | 29 | 0 |
| Actionable | `closed_trailing_stop` / `trailing_stop` | 13 | +$139.23 | 13 | 0 |
| Actionable | `closed_sl` / `stop_loss` | 7 | -$599.28 | 0 | 7 |
| Actionable | `closed_expired` / `expired` | 4 | -$105.97 | 1 | 3 |
| Actionable | `closed_floor` / `floor` | 2 | -$123.75 | 0 | 2 |

The largest descriptive mechanism is still exit-shape, not classifier-shape: peak-fade winners and stop-loss losses dominate both cohorts. That argues against turning this finding directly into an actionability classifier policy.

## Branch Decision

**CLEARED / no immediate implementation authorized.**

The stale gate is closed: prod has enough v1 closed rows to stop saying "wait for n." However:

- Exploratory `n=16` is still too small for directional classifier-quality claims.
- The strongest exploratory false-negative lead is below-$10M `chain_completed`, but its bucket is net negative at current n.
- Exit policy dominates the PnL shape across both cohorts.
- No malformed-row, missing-PnL, mixed-version, or pre-cutover issue invalidated the query.

## Backlog Impact

- `BL-NEW-ACTIONABILITY-GATE`: update data-gate status from THIN to CLEARED / no immediate implementation.
- `BL-NEW-X-OUTCOME-LINKAGE`, `BL-NEW-TG-OUTCOME-LINKAGE`, and `BL-NEW-NO-PEAK-RISK-HANDLING`: no longer blocked by the `20/5` actionability n gate, but each still needs a fresh drift/runtime re-scope before implementation.
- Actionability v2 / suppression remains blocked by evidence quality, not by row count.

## Follow-Up Candidates

1. **Below-$10M `chain_completed` attribution pass**: revisit when that same signal/reason pair reaches at least `n=20` closed rows, or if the bucket turns positive after outlier checks.
2. **Exit-shape audit across actionability cohorts**: stop-loss losses dominate both cohorts; any policy work should trace whether this is entry quality, exit policy, or stale/freshness behavior.
3. **Downstream gated work re-scope**: X outcome linkage, TG outcome linkage, and no-peak risk handling can now be evaluated against current tree/runtime, but should not inherit this finding as automatic approval.

## Rollback

N/A. Read-only analysis and Markdown status updates only.
