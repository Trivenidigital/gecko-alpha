# Design — Signal trust scorecards (BL-NEW-SIGNAL-TRUST-ROADMAP) — 2026-05-25

## Intent

Add a read-only scorecards surface that combines:

- signal trust registry (maturity + data-quality warnings + next gate), and
- objective cohort evidence from `paper_trades`

…so the operator can answer "which signal families do I trust today?" with explicit low-n / coverage caveats.

This is visibility-only: no writes, no suppression/pruning/auto-disable, no sizing, no execution.

## New primitives introduced

- Endpoint: `GET /api/signal_trust/scorecards`
- UI: render scorecards inside `SignalTrustTab.jsx` (still V1 / not-for-pruning)

## Drift-check (compose, don’t duplicate)

Reuse in-tree truth:

- `dashboard/db.py:get_trading_stats_by_signal_cohort(db_path, days=...)` for closed-trade PnL/win-rate cohorts (full vs would_be_live=1)
- `docs/superpowers/registries/signal_trust_registry.v1.json` via the existing `/api/signal_trust_registry` loader/validator logic

Add only the residual gap:

- actionable/would_be_live stamp coverage + disagreement confusion matrix per signal_type per window
- current open trades count + open exposure notional per signal_type

## Contract

### Query params

- No params in V1 (fixed windows) to avoid accidental operator misreads from custom windows.

### Response shape

```json
{
  "meta": {
    "read_only": true,
    "not_for_pruning": true,
    "not_for_auto_disable": true,
    "experimental": true,
    "generated_at": "ISO8601",
    "windows_days": [7, 14, 30]
  },
  "rows": [
    {
      "signal_type": "chain_completed",
      "registry": {
        "maturity_state": "trusted_experimental",
        "data_quality_warning": "string|null",
        "next_gate_type": "string|null",
        "next_gate_threshold": "string|null"
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
            "stamped_n": 10,
            "unknown_n": 2,
            "actionable_rate": 0.70,
            "would_be_live_rate": 0.60,
            "confusion": {
              "a1_w1": 5,
              "a1_w0": 2,
              "a0_w1": 1,
              "a0_w0": 2
            },
            "disagree_n": 3,
            "disagree_rate": 0.30
          },
          "warnings": ["low_n"]
        }
      ]
    }
  ]
}
```

### Deterministic ordering

Rows are returned in deterministic order:

1. `signal_type` ascending (primary key)

Within each row, `windows` are ordered `[7, 14, 30]`.

This avoids UI row jitter under ties and keeps the endpoint stable for contract checks.

### Window anchors

- Closed-trade stats are windowed on `paper_trades.closed_at` (require `closed_at IS NOT NULL`).
- Open stats are current-state: `paper_trades.status='open'` with no time window.

### Win definition

`wins = COUNT(where pnl_usd IS NOT NULL AND pnl_usd > 0)`.

### Stamp semantics (avoid NULL-as-false)

Stamps are computed on the closed-trade cohort in-window:

- `stamped_n = COUNT(where actionable IS NOT NULL AND would_be_live IS NOT NULL)`
- `unknown_n = closed_n - stamped_n`
- Rates are computed over `stamped_n` only (return `null` when stamped_n=0).
- Confusion matrix keys are for stamped rows only:
  - `a1_w1` => actionable=1 & would_be_live=1
  - `a1_w0` => actionable=1 & would_be_live=0
  - `a0_w1` => actionable=0 & would_be_live=1
  - `a0_w0` => actionable=0 & would_be_live=0
- `disagree_n = a1_w0 + a0_w1`
- `disagree_rate = disagree_n / stamped_n` when stamped_n>0 else null

### Sample-size warnings

Warnings are strings; V1 emits:

- `low_n` when `closed_n < 10` for that window
- `no_stamps` when `stamped_n == 0` but `closed_n > 0`

## Backend implementation

### Registry

Load the registry from the existing file path (same as `/api/signal_trust_registry`) and index by `signal_type`.

### Open stats (current)

One query:

- `SELECT signal_type, COUNT(*) open_count, COALESCE(SUM(amount_usd), 0) open_exposure_usd FROM paper_trades WHERE status='open' GROUP BY signal_type`

### Closed-trade window stats (per window)

Reuse `get_trading_stats_by_signal_cohort(db_path, days=...)` for:

- closed_n (trades), wins, total_pnl_usd, win_rate_pct, avg_pnl_pct per `signal_type`

Add one minimal aggregate per window for stamp/confusion stats (closed cohort only):

- filter: `closed_at >= datetime('now', ?)` AND `closed_at IS NOT NULL` AND `status != 'open'`
- compute:
  - closed_n (repeat defensively for alignment)
  - stamped_n
  - a1_w1/a1_w0/a0_w1/a0_w0

### Failure modes

- Missing `paper_trades` table or missing `would_be_live` / `actionable` columns:
  - return 200 with empty `rows` and meta flags (visibility surface stays up)
  - include a `meta.data_missing_reason` string for operator clarity

## Frontend

Extend `dashboard/frontend/components/SignalTrustTab.jsx`:

- fetch `/api/signal_trust/scorecards`
- render a table:
  - Signal, Maturity, Open count/exposure, and per-window columns (7d/14d/30d)
  - show low-n/no-stamps warnings inline
- replace index-based React keys for registry entries:
  - `key={e.signal_type || idx}`

## Tests

- Endpoint returns deterministic ordering for rows/windows.
- Missing table/columns returns `{meta..., rows: []}` rather than 500.
- Stamp/confusion metrics respect NULL-as-unknown (no NULL-as-false).
- Basic aggregation correctness on a tiny seeded DB.

## Verification

- `uv run pytest -q` for the focused new tests + existing trust registry tests.
- `npm.cmd run build:codex` if frontend sources change.

## Rollback

Revert commits touching:

- `dashboard/api.py`
- `dashboard/db.py`
- `dashboard/models.py`
- `dashboard/frontend/components/SignalTrustTab.jsx`
- tests + optional `dashboard/frontend/dist/`

No DB migrations; rollback is source-only.

