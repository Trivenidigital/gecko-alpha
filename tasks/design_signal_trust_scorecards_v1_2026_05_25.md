**New primitives introduced:** endpoint `GET /api/signal_trust/scorecards`; Signal Trust dashboard scorecards sub-panel.

# Design - Signal trust scorecards (BL-NEW-SIGNAL-TRUST-ROADMAP) - 2026-05-25

## Intent

Add a read-only scorecards surface that combines:

- signal trust registry fields: maturity, data-quality warning, and next gate
- objective cohort evidence from `paper_trades`

The operator should be able to inspect signal-family evidence without the system making pruning, sizing, or execution decisions.

## Hermes-first analysis

This design keeps truth computation inside Gecko-Alpha because the source facts
are local DB rows, the existing signal-trust registry, and existing dashboard
cohort helpers. Hermes may later enrich explanations, but it is not a source of
price, PnL, eligibility, or maturity truth for this V1.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Read-only scorecards over `paper_trades` | none found | Build custom in Gecko-Alpha; DB truth stays local. |
| Trust registry export + validation | none found | Reuse the existing custom registry contract. |
| Signal explanation text | possible generic summarization only | Defer; explanation-only if added later. |

awesome-hermes-agent ecosystem check was completed during the plan pass and
found no drop-in scorecard primitive that preserves Gecko's read-only,
not-for-pruning constraints.

## Drift-check

Reuse in-tree truth:

- `dashboard/db.py:get_trading_stats_by_signal_cohort(db_path, days=...)` for closed-trade PnL/win-rate cohorts.
- `docs/superpowers/registries/signal_trust_registry.v1.json` through the same file contract as `/api/signal_trust_registry`.

Add only the residual gap:

- actionable/would_be_live stamp coverage + disagreement confusion matrix per signal_type per window
- current open-trade count + open exposure notional per signal_type

## Contract

### Query params

No params in V1. The endpoint always returns fixed windows `[7, 14, 30]` to avoid operator-side window shopping.

### Response shape

```json
{
  "meta": {
    "ok": true,
    "read_only": true,
    "not_for_pruning": true,
    "not_for_suppression": true,
    "not_for_auto_disable": true,
    "not_for_sizing": true,
    "not_for_execution": true,
    "not_for_alerting": true,
    "not_for_source_ranking": true,
    "experimental": true,
    "visibility_only": true,
    "not_live_eligibility_verdict": true,
    "cohort_policy": "full_closed_paper_trades",
    "sort_policy": "signal_type_asc_not_ranked",
    "generated_at": "ISO8601",
    "windows_days": [7, 14, 30],
    "data_missing_reason": null
  },
  "rows": [
    {
      "signal_type": "chain_completed",
      "registry": {
        "maturity_state": "trusted_experimental",
        "data_quality_warning": null,
        "next_gate_type": "sample_size",
        "next_gate_threshold": "n>=30"
      },
      "open": {
        "open_count": 3,
        "open_exposure_usd": 150.0
      },
      "windows": [
        {
          "days": 7,
          "closed": {
            "closed_n": 12,
            "wins": 9,
            "win_rate_pct": 75.0,
            "total_pnl_usd": 123.45,
            "avg_pnl_pct": 4.56
          },
          "stamps": {
            "both_known_n": 10,
            "null_mismatch_n": 1,
            "unknown_n": 2,
            "actionable_known_n": 11,
            "actionable_unknown_n": 1,
            "actionable_rate": 0.7,
            "would_be_live_known_n": 10,
            "would_be_live_unknown_n": 2,
            "would_be_live_rate": 0.6,
            "confusion": {
              "a1_w1": 5,
              "a1_w0": 2,
              "a0_w1": 1,
              "a0_w0": 2
            },
            "disagree_n": 3,
            "disagree_rate": 0.3
          },
          "warnings": ["low_n"]
        }
      ]
    }
  ]
}
```

### Deterministic ordering

Rows are sorted by `signal_type` ascending. Windows are always `[7, 14, 30]`.
This is alphabetical presentation, not ranking. The endpoint exposes
`meta.sort_policy="signal_type_asc_not_ranked"` and must not be consumed by
alerting, pruning, execution, source-ranking, or sizing code.

### Anti-scope contract

The scorecards are closed paper-trade evidence, not live eligibility verdicts.
The endpoint exposes `meta.not_live_eligibility_verdict=true` and
`meta.cohort_policy="full_closed_paper_trades"`. A test scans the tree and
fails if the scorecards helper or endpoint is imported or called from alerting,
pruning, execution, source-ranking, or scripting paths.

### Window anchors

- Closed-trade stats are windowed on `paper_trades.closed_at` with `closed_at IS NOT NULL`.
- Open stats are current-state rows: `status='open' AND closed_at IS NULL`.
- Date comparisons use SQLite `julianday(...)` so ISO timestamps with `T` separators compare correctly against SQLite's `datetime('now', ...)` cutoff.

### Win definition

Reuse the in-tree cohort stats definition:

- `trades = COUNT(*)` over the closed-trade cohort in-window.
- `wins = SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)`.

NULL `pnl_usd` counts toward trades but not wins, matching the existing cohort helper.

### Stamp semantics

NULL stamps are unknown, not false.

- `both_known_n`: actionable and would_be_live are both non-null.
- `actionable_known_n`: actionable is non-null.
- `would_be_live_known_n`: would_be_live is non-null.
- `null_mismatch_n`: exactly one of actionable / would_be_live is null.
- Confusion matrix values only count rows where both stamps are known.
- `disagree_n = a1_w0 + a0_w1`.
- `disagree_rate = disagree_n / both_known_n` when `both_known_n > 0`, else null.
- Individual rates use their own known denominators.

### Sample-size warnings

V1 emits:

- `low_n` when `0 < closed_n < 10`.
- `no_stamps` when `closed_n > 0` and `both_known_n == 0`.

## Backend implementation

`dashboard/db.py:get_signal_trust_scorecards(db_path)`:

- Queries current open stats by `signal_type`.
- Discovers DB signal types from open and closed paper trades.
- Queries stamp/confusion aggregates for each fixed window.
- Reuses `get_trading_stats_by_signal_cohort` for PnL/win-rate stats.
- Loads registry entries from `GECKO_SIGNAL_TRUST_REGISTRY_PATH` when set, else the shipped registry JSON.
- Uses the same path-override, size, JSON, and schema validation helper as `/api/signal_trust_registry`.
- Returns the union of registry signal types and DB signal types.

Failure behavior:

- Missing `paper_trades` table returns `meta.ok=false`, `rows=[]`, and `error.code=paper_trades_missing`; API maps this to HTTP 503.
- Missing registry or invalid registry returns HTTP 503; scorecards never silently render registry-less trust evidence.
- Missing stamp columns returns HTTP 200 with `meta.data_missing_reason=stamps_unavailable` and `stamps=null` on windows.
- Missing required `paper_trades` columns outside stamp columns returns HTTP 503 rather than false zeroes.
- Other unexpected operational errors are not broadly hidden unless they match the existing cohort helper's documented fallback path.

## API implementation

`dashboard/api.py` adds:

- `GET /api/signal_trust/scorecards`
- `Cache-Control: no-store`
- HTTP 503 only when `meta.ok=false`

## Frontend implementation

`dashboard/frontend/components/SignalTrustTab.jsx`:

- Fetches registry and scorecards in one refresh path.
- Keeps registry rendering working when scorecards are unavailable.
- Renders a compact read-only table with Signal, Maturity, Open, and
  "closed paper" 7d/14d/30d columns.
- Uses `signal_type` as the scorecard row key.

## Tests

`tests/test_signal_trust_scorecards_endpoint.py` covers:

- response invariants and no-store header
- deterministic ordering
- missing table 503 path
- shared registry validation failure path
- low-n/no-stamps warnings
- missing stamp-column degradation
- stamp confusion matrix and NULL denominator math
- registry-only + DB-only union
- ISO timestamp boundary correctness on both sides of the cutoff

## Verification

- `uv run pytest -q tests/test_signal_trust_scorecards_endpoint.py`
- `npm.cmd run build:codex`
- broader dashboard/API tests before PR merge

## Rollback

Revert source changes in:

- `dashboard/api.py`
- `dashboard/db.py`
- `dashboard/models.py`
- `dashboard/frontend/components/SignalTrustTab.jsx`
- `dashboard/frontend/dist/`
- `tests/test_signal_trust_scorecards_endpoint.py`

No DB migrations are involved.
