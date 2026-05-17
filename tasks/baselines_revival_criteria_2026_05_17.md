**New primitives introduced:** NONE.

# Revival-Criteria Healthy-Signal Baselines (2026-05-17)

**Source:** srilu-vps `/root/gecko-alpha/scout.db` snapshot pulled 2026-05-17.

**Purpose:** Derive threshold values for `Settings.REVIVAL_CRITERIA_*` from empirical healthy-signal baselines, per Reviewer A finding #4 (CRITICAL) and Reviewer D finding #3 (IMPORTANT). Replaces the PROVISIONAL placeholders in plan v3 Task 8.

## Method

For each "healthy" signal (positive net P&L per the 2026-05-17 morning checkpoint Q2 — chain_completed, volume_spike, narrative_prediction), compute:

- `nb_loss` = fraction of closed trades where `(peak_pct <= 5% OR peak_pct IS NULL) AND pnl_usd < 0` — the no-breakout-AND-loss failure mode
- `sl_freq` = fraction with `exit_reason='stop_loss'`
- `exp_loss` = fraction with `exit_reason IN ('expired','expired_stale_price') AND pnl_usd < 0`
- `exit_machinery` = `(peak_fade ∪ trailing_stop ∪ moonshot_trail) positive pnl / all positive pnl`

## Raw observations

| signal_type | n | nb_loss | sl_freq | exp_loss | exit_machinery |
|---|---:|---:|---:|---:|---:|
| chain_completed | 12 | 0.083 | 0.167 | 0.000 | 1.000 |
| volume_spike | 36 | 0.278 | 0.028 | 0.306 | 0.991 |
| narrative_prediction | 185 | 0.368 | 0.011 | 0.427 | 0.756 |
| **healthy max** | | **0.368** | 0.167 | 0.427 | |
| **healthy min** | | 0.083 | 0.011 | 0.000 | **0.756** |
| losers_contrarian (control) | 288 | 0.330 | 0.174 | 0.285 | 0.851 |

## Threshold derivation

The plan v3 PROVISIONAL defaults (`MAX_NO_BREAKOUT_AND_LOSS=0.25`, `EXIT_MACHINERY_MIN=0.50`) are unsupportable against the observed healthy range:

- 0.25 nb_loss would FAIL narrative_prediction (0.368) — a profitable signal. Too tight.
- 0.50 exit_machinery would PASS any signal — even something dominated by TP exits or stop-loss luck. Too lenient.

**Chosen thresholds (final, replaces PROVISIONAL):**

| Setting | PROVISIONAL (v3 plan) | Final (post-Task-0) | Rationale |
|---|---|---|---|
| `REVIVAL_CRITERIA_MAX_NO_BREAKOUT_AND_LOSS` | 0.25 | **0.40** | `healthy_max (0.368) + 9% margin`; tight enough to reject pathological dud signals, loose enough to admit healthy ones |
| `REVIVAL_CRITERIA_EXIT_MACHINERY_MIN` | 0.50 | **0.70** | `healthy_min (0.756) - 8% margin`; below this means winners are not exit-machinery-driven (TP / luck) |
| `REVIVAL_CRITERIA_WIN_WILSON_LB_MIN` | 0.50 (coin-flip) | **0.55** | Per Reviewer D #3: coin-flip baseline is too lenient; 55% Wilson LB requires the signal be statistically better than chance with margin |

Stop_loss and expired_loss frequencies are NOT gated directly — they vary too widely across healthy signals (sl 0.011-0.167; exp 0.0-0.427) to set a defensible threshold. They remain in `WindowDiagnostics` for operator visibility but do NOT trip FAIL. (Plan v3 Task 10 `_evaluate_window_gates` reflects this: only the 4 primary gates fire FAIL — Wilson LB, bootstrap LB, no_breakout_and_loss, exit_machinery.)

## Defer-flag (per PR-stage reviewer #3 finding #14)

The `EXIT_MACHINERY_MIN=0.70` threshold is anchored to `narrative_prediction`'s 0.756 baseline. chain_completed's contribution to the healthy set (0.991 at n=12) is small-sample and is NOT the anchor. **If any future FAIL verdict is attributed SOLELY to `exit_machinery_contribution` (i.e., all other gates pass), the operator should re-derive baselines.** This is the trigger condition for `BL-NEW-REVIVAL-CRITERIA-QUARTERLY-RECALIBRATION`; document it in the next quarterly review.

## Cross-check against LC (control)

LC's aggregate metrics (nb_loss 0.33, exit_machinery 0.85) sit WITHIN healthy range — confirming the failure mode is NOT in the secondary diagnostics. The bleed is in the per-trade-pnl tail (Wilson LB / bootstrap LB) which the plan v3 §1b family-wise disclosure already addresses as the primary gate. The secondary diagnostics are sanity-check guardrails; the primary statistical gates are where signal-quality FAIL actually triggers.

## Provenance

Query:

```sql
WITH closed AS (
  SELECT pnl_usd, peak_pct, exit_reason
  FROM paper_trades
  WHERE signal_type = ? AND status LIKE 'closed_%'
    AND pnl_usd IS NOT NULL AND pnl_pct IS NOT NULL
)
SELECT COUNT(*) AS n,
  ROUND(AVG(CASE WHEN (peak_pct IS NULL OR peak_pct <= 5.0) AND pnl_usd < 0 THEN 1.0 ELSE 0.0 END), 3) AS nb_loss,
  ROUND(AVG(CASE WHEN exit_reason = 'stop_loss' THEN 1.0 ELSE 0.0 END), 3) AS sl_freq,
  ROUND(AVG(CASE WHEN exit_reason IN ('expired','expired_stale_price') AND pnl_usd < 0 THEN 1.0 ELSE 0.0 END), 3) AS exp_loss,
  ROUND(SUM(CASE WHEN exit_reason IN ('peak_fade','trailing_stop','moonshot_trail') AND pnl_usd > 0 THEN pnl_usd ELSE 0.0 END)
   / NULLIF(SUM(CASE WHEN pnl_usd > 0 THEN pnl_usd ELSE 0.0 END), 0), 3) AS exit_machinery
FROM closed;
```

Executed via `ssh srilu-vps 'sqlite3 /root/gecko-alpha/scout.db < /tmp/baseline.sql'` per project SSH constraint (CLAUDE.md global).
