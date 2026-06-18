# first_signal post-revival verdict - EXTEND-SOAK

**Date:** 2026-06-18
**Finding:** P2 from the original review set
**Source:** srilu-vps alias, host `ubuntu-4gb-hel1-1`, `/root/gecko-alpha/scout.db`
**Scope:** Doc-only verdict. No code change, no prod mutation, no service restart.

## Verdict

`first_signal` is **not a retire verdict yet**.

The pre-registered 2026-05-17 gate says:

| Observation post-revival | Verdict | Action |
|---|---|---|
| `n >= 10` and cumulative PnL < 0 but positive-tail win rate >= 17% | EXTEND-SOAK 14d | File `BL-NEW-FIRST-SIGNAL-EXTEND-SOAK` |

The live post-revival cohort matches that row:

- Post-revival trades: 14 total, 12 closed, 2 open
- Net PnL: -$439.83
- Positive-tail rate: 3/12 = 25.0% under the original strict status definition (`closed_trailing_stop` only in this cohort); 5/12 = 41.7% if `closed_floor` positive exits are included
- Current signal state: `enabled=1`, `suspended_at=NULL`, `suspended_reason=NULL`

Therefore the correct action is **EXTEND-SOAK 14d**, not retire.

## Runtime Verification

Queried live prod on 2026-06-18T21:38:59Z using read-only SQLite access.

### Current signal_params row

| field | value |
|---|---|
| `signal_type` | `first_signal` |
| `enabled` | 1 |
| `tg_alert_eligible` | 0 |
| `live_eligible` | 0 |
| `suspended_at` | NULL |
| `suspended_reason` | NULL |
| `updated_at` | 2026-05-31T12:46:51.476977+00:00 |
| `updated_by` | `operator` |
| `drawdown_baseline_at` | 2026-05-31T12:46:51.476977+00:00 |
| `conviction_lock_enabled` | 1 |
| `high_peak_fade_enabled` | 0 |

### Revival boundary

`signal_params_audit.id=32` re-enabled the signal:

| field | value |
|---|---|
| `applied_at` | 2026-05-31T12:46:51.476977+00:00 |
| `field_name` | `enabled` |
| `old_value` | 0 |
| `new_value` | 1 |
| `applied_by` | `operator` |
| `reason` | `operator-authorized 2026-05-31 first_signal revive-and-soak start; prior soak never started because signal remained disabled` |

`signal_params_audit.id=33` kept `tg_alert_eligible=0`, preserving the existing operator opt-out from Telegram alerts.

## Post-Revival Cohort

Boundary: `opened_at >= 2026-05-31T12:46:51.476977+00:00`.

| status | n | PnL | Avg |
|---|---:|---:|---:|
| `closed_expired` | 2 | -$83.02 | -$41.51 |
| `closed_floor` | 2 | +$39.28 | +$19.64 |
| `closed_sl` | 5 | -$443.85 | -$88.77 |
| `closed_trailing_stop` | 3 | +$47.77 | +$15.92 |
| `open` | 2 | $0.00 | $0.00 |
| **TOTAL** | **14** | **-$439.83** | |

Closed-only average PnL: -$36.65.

Time range:

| metric | value |
|---|---|
| first post-revival open | 2026-06-03T22:02:20.276179+00:00 |
| latest post-revival open | 2026-06-14T20:02:05.059275+00:00 |
| latest post-revival close | 2026-06-14T19:00:57.983239+00:00 |

## Interpretation

The cohort is negative, but it has not lost its positive-tail behavior:

- Original strict positive-tail definition from the May finding counts `closed_tp`, `closed_trailing_stop`, `closed_moonshot_trail`, and `closed_peak_fade`.
- This cohort has 3 strict positive-tail closes out of 12 closed trades = 25.0%, above the 17% threshold.
- The newer `closed_floor` exit is also positive PnL. If counted as a positive exit, the cohort is 5/12 = 41.7%, matching the operator's prior "positive-tail win rate roughly 40-50%" read.

That means the signal still has the profile the May decision intended to measure: negative aggregate PnL, but enough right-tail behavior to avoid a clean retire verdict.

## Next Gate

Extend the soak and re-check at the next data/date boundary:

- Calendar boundary: 2026-07-02T12:46:51Z, 14 days after this verdict date.
- Data boundary: evaluate early if post-revival closed trades reach `n >= 20` before 2026-07-02.
- If the calendar boundary arrives with fewer than 20 closed post-revival trades, record the data-starved state and continue until `n >= 20` or an auto-suspend event fires.

Verdict table for the next check:

| Observation at next boundary | Verdict | Action |
|---|---|---|
| Auto-suspended under current gate | RETIRE | File retire finding / code-removal follow-up |
| Closed `n >= 20`, PnL >= 0, positive-tail win rate >= 17% | KEEP-PAPER | Close as validated paper signal |
| Closed `n >= 20`, PnL < 0, positive-tail win rate >= 17% | EXTEND-SOAK or narrow policy review | Decide based on stop-loss concentration and open-trade state |
| Closed `n >= 20`, PnL < 0, positive-tail win rate < 17% | RETIRE | File retire finding / code-removal follow-up |

## Status

Original P2 finding is closed as **EXTEND-SOAK**, with the signal left enabled for continued paper observation. No production write was made by this PR.
