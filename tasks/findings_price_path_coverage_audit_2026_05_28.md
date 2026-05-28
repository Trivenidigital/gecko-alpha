# Price-Path Coverage Audit — Findings

**Audit date:** 2026-05-28
**Audit script:** `scripts/audit_price_path_coverage.py`
**Plan:** `tasks/plan_price_path_coverage_audit_2026_05_28.md`

## Purpose

Gate the build PR for `BL-NEW-TODAYS-FOCUS-SPARKLINE` (PR-C) with a measurement of intraday price-point density per Today's Focus row.

## Audit Approach

Consumes live `/api/todays_focus?window_hours=36` for cohort match. Per row, counts `volume_history_cg` records within the last 24h (capped at 7d writer retention) with valid prices. Reports joinable/unjoinable counts as first-class fields so a low coverage rate is not silently attributed to "missing data" when truth is "unjoinable key space."

Source-of-truth scope: `volume_history_cg` only (markets-watcher cadence source; PR-C's intended sparkline data path). Other price+timestamp tables (`gainers_snapshots`, `losers_snapshots`, `momentum_7d`, `slow_burn_candidates`, `volume_spikes`) documented but NOT counted; PR-C decides whether to widen.

## srilu prod snapshot — pending

<srilu_run_pending>

(To be appended via follow-up commit on master after deploy. Run:
`python scripts/audit_price_path_coverage.py --db /root/gecko-alpha/scout.db --url http://127.0.0.1:8000 --window-hours 36 --lookback-hours 24 --json`)

## Branch Decision Logic (lives in PR-C plan, not here)

Density thresholds (e.g., "≥12 points per row for ≥80% of cohort as PR-C green-light") are PR-C planning decisions. This audit's deliverable is the raw measurement; interpretation belongs to PR-C.
