# Finding: slow_burn paper-dispatch forward soak FAILED — disabled 2026-06-18

**Status:** CLOSED-FAILED. slow_burn paper dispatch is OFF on both gates:
`signal_params.slow_burn.enabled=0` (auto_suspend 2026-06-17) AND prod `.env`
`SLOW_BURN_DISPATCH_ENABLED=False` (operator revert 2026-06-18, this finding).

## What it was

`BL-NEW-SLOW-BURN-DISPATCH-PROMOTION` promoted the slow_burn detector
(`detect_slow_burn_7d`, gradual 7-day accumulation → continuation thesis) from
shadow-only to **paper-trade dispatch** behind `SLOW_BURN_DISPATCH_ENABLED`
(default False). The 14-day shadow soak passed its pre-registered bar (hit-rate
vs velocity_alerter); the **forward paper-dispatch soak is what failed.**

## Verified cohort (prod scout.db, 2026-06-18)

Full closed cohort — **39 trades, 14 wins (35.9%), net −$572.43**:

| exit status | n | net $ |
|---|---|---|
| closed_sl | 19 | **−961.56** |
| closed_expired | 7 | −97.97 |
| closed_peak_fade | 9 | +305.42 |
| closed_trailing_stop | 4 | +181.68 |
| **total closed** | **39** | **−572.43** |

- Cohort opened through **2026-06-17T00:21:12Z**; **0 trades opened after the
  suspension** (2026-06-17T01:01:50Z) — the gate held; cohort is final.
- 0 `open` positions remain.

## Why it failed

Stop-loss exits dominate the loss: **19 closed_sl / −$961.56** — half the cohort
hit the stop, and that loss is not offset by the net-positive exit modes
(peak_fade +$305, trailing_stop +$182). Win rate 35.9% with a left-skewed PnL
distribution. The slow-burn continuation thesis did not translate into positive
tradeable paper expectancy over the dispatch soak.

## Auto-suspend trigger (2026-06-17T01:01:50Z)

`signal_params_audit`: `enabled 1→0`, `applied_by=auto_suspend`,
reason **`hard_loss: net $-544, drawdown $-578 (n=32)`**. This is the
combined-gate (`hard_loss`) windowed metric (n=32 recent trades), distinct from
the full-cohort net −$572.43 over 39. Both agree: net-negative, stop-dominated.

## Pre-registered disposition

Per the soak criteria, negative dispatch PnL → **retire dispatch** (do not keep
paper-trading the signal). KEEP suspended. The detector itself
(`detect_slow_burn_7d`) remains as a shadow/observability signal; only the
**paper-trade dispatch** is disabled.

## Enforcement (§9a path-reaches-lever, verified)

Dispatch is gated at TWO points, both now closed:
1. `scout/main.py:1004` — `if trading_engine and settings.SLOW_BURN_DISPATCH_ENABLED:`
   guards the whole `trade_slow_burn` dispatch loop → now False, loop never runs.
2. `scout/trading/engine.py:269` — `if not signal_params.enabled:` skips each
   open (`trade_skipped_signal_disabled`) → enabled=0, blocks even if (1) flips.

Verified on prod: `.env:122 SLOW_BURN_DISPATCH_ENABLED=False`, pipeline restarted
active (NRestarts=0), `load_settings().SLOW_BURN_DISPATCH_ENABLED == False`.

## Re-enable criteria (do NOT flip back without these)

Re-enabling requires a fresh analysis showing **positive expectancy** — e.g. a
tightened entry cohort (mcap/liquidity band), a revised exit policy that cuts the
closed_sl bleed, or a regime where the continuation thesis holds — pre-registered
with a kill criterion, NOT just "it's been a while." Flipping `.env` back to True
alone is insufficient: `signal_params.slow_burn.enabled` is also 0 and would need
an explicit operator/revival-helper re-enable.

## Revert (if needed)

`.env` backup at `/root/gecko-alpha/.env.bak.slowburn-revert-20260618`; restore
the line + restart `gecko-pipeline` (still also gated by enabled=0).
