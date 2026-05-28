# Price-Path Coverage Audit — Findings

**Audit date:** 2026-05-28
**Audit script:** `scripts/audit_price_path_coverage.py`
**Plan:** `tasks/plan_price_path_coverage_audit_2026_05_28.md`

## Purpose

Gate the build PR for `BL-NEW-TODAYS-FOCUS-SPARKLINE` (PR-C) with a measurement of intraday price-point density per Today's Focus row.

## Audit Approach

Consumes live `/api/todays_focus?window_hours=36` for cohort match. Per row, counts `volume_history_cg` records within the last 24h (capped at 7d writer retention) with valid prices. Reports joinable/unjoinable counts as first-class fields so a low coverage rate is not silently attributed to "missing data" when truth is "unjoinable key space."

Source-of-truth scope: `volume_history_cg` only (markets-watcher cadence source; PR-C's intended sparkline data path). Other price+timestamp tables (`gainers_snapshots`, `losers_snapshots`, `momentum_7d`, `slow_burn_candidates`, `volume_spikes`) documented but NOT counted; PR-C decides whether to widen.

## srilu prod snapshot — 2026-05-28T22:30:15Z

Live audit on srilu at master `12edd513` (audit-script merge commit). Cohort: 5 rows from `/api/todays_focus?window_hours=36`. Lookback: 24h.

```json
{
  "audited_at": "2026-05-28T22:30:15Z",
  "window_hours": 36,
  "lookback_hours": 24,
  "cutoff_iso": "2026-05-27T22:30:15.514450+00:00",
  "endpoint_url": "http://127.0.0.1:8000/api/todays_focus?window_hours=36",
  "total_rows": 5,
  "paper_corpus": {
    "rows": 3,
    "joinable_by_token_id": 3,
    "unjoinable_or_zero_points": 0,
    "join_rate": 1.0,
    "points_distribution": null,
    "per_row": [
      {"token_id": "octra", "symbol": "OCT", "points": 125},
      {"token_id": "virtual-protocol", "symbol": "VIRTUAL", "points": 547},
      {"token_id": "okb", "symbol": "OKB", "points": 547}
    ]
  },
  "tracker_corpus": {
    "rows": 2,
    "rows_with_at_least_one_point": 2,
    "rows_with_zero_points": 0,
    "join_rate": 1.0,
    "points_distribution": null,
    "per_row": [
      {"token_id": "allora", "symbol": "ALLO", "points": 547},
      {"token_id": "verified-emeralds", "symbol": "VEREM", "points": 329}
    ]
  },
  "schema_findings": {
    "volume_history_cg_has_price": true,
    "volume_history_cg_has_recorded_at": true,
    "price_cache_has_history_table": false,
    "alternate_price_history_tables_present": {
      "gainers_snapshots": true,
      "losers_snapshots": true,
      "momentum_7d": true,
      "slow_burn_candidates": true,
      "volume_spikes": true
    }
  }
}
```

## Factual observations from this snapshot

These are the raw measurements. Threshold-driven interpretation lives in PR-C's plan.

1. **Paper-corpus join_rate = 1.0** — All 3 paper rows joined directly to `volume_history_cg.coin_id` via `token_id`. The token_id for these paper rows IS the CoinGecko slug. The key-space mismatch concern from PR #310 review does not manifest in this snapshot.

2. **Tracker-corpus join_rate = 1.0** — Both tracker rows joined directly (expected; tracker token_id is the CG slug by construction).

3. **All 5 rows have substantial point density in the 24h window** (per-row counts):
   - OCT: 125 points
   - VIRTUAL: 547 points
   - OKB: 547 points
   - ALLO: 547 points
   - VEREM: 329 points

   Total: 2,095 points across 5 tokens over 24h. Roughly 5-22 points per hour per token.

4. **`points_distribution: null` for both corpora** — by spec, N<5 emits null aggregate stats. Paper N=3, tracker N=2. Per-row arrays remain populated.

5. **All 5 alternate price+timestamp tables exist** (`gainers_snapshots`, `losers_snapshots`, `momentum_7d`, `slow_burn_candidates`, `volume_spikes`) — confirmed by `PRAGMA` at runtime. They are excluded from this audit's count by scope (named in plan §"Source-of-truth scope"). PR-C may widen the data source by also reading them; this audit reports their presence as a measured fact.

6. **`price_cache_has_history_table: false`** — confirmed by `PRAGMA`: no dedicated `price_cache_history` table exists. `price_cache` itself holds only a single current snapshot per coin.

## Forward-reference for PR-C scoping

The threshold logic lives in PR-C's plan. This snapshot supplies the factual inputs:

- Headline: **all current Today's Focus rows have ≥125 price points over 24h in `volume_history_cg`** — well above any reasonable sparkline-density threshold (e.g., ≥12 would be 10× under the lowest current count).
- The earlier "backfill required" branch (which fired for the liquidity audit) does NOT apply here. PR-C can proceed without a price-path backfill prerequisite.
- Outstanding decisions for PR-C's plan: (a) what point-density floor justifies a renderable sparkline (suggested ≥12, but PR-C decides), (b) whether to also consume `gainers_snapshots` / `momentum_7d` / etc. for cross-source density (probably unnecessary at current density), (c) per-row "insufficient data" fallback semantics if a future cohort drops below the floor.
- Re-run the audit any time before the PR-C build PR opens to confirm density still supports the design.

## Branch Decision Logic (lives in PR-C plan, not here)

Density thresholds (e.g., "≥12 points per row for ≥80% of cohort as PR-C green-light") are PR-C planning decisions. This audit's deliverable is the raw measurement; interpretation belongs to PR-C.
