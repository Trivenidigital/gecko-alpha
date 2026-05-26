# Plan — Signal trust scorecards (BL-NEW-SIGNAL-TRUST-ROADMAP) — 2026-05-25

## Goal

Ship a read-only “signal family scorecards” surface that joins:

- the existing V1 trust registry (`/api/signal_trust_registry`) maturity states, and
- objective recent performance + cohort stats from the Gecko DB (`paper_trades`)

…into one operator-visible panel that answers “which signal families look healthy right now, and which are context-only / data-insufficient?”.

V1 remains **read-only** and **not for pruning / suppression / auto-disable / sizing / execution**.

## Non-goals

- No parameter changes, no config flips, no kill-switch changes.
- No paid/vendor calls.
- No Hermes dependency for truth computation (Hermes may be used later for explanation/enrichment only).
- No schema migrations in this iteration.

## New primitives introduced

- New read-only endpoint: `/api/signal_trust/scorecards`
- New dashboard view inside “Signal Trust (V1)” tab (or a sibling sub-panel)

## Drift-check (§7a)

In-tree primitives already exist:

- Trust registry export: `/api/signal_trust_registry` + `dashboard/frontend/components/SignalTrustTab.jsx`
- Trading stats by signal: `/api/trading/stats/by-signal` and `/api/trading/stats/by-signal-cohort`

Residual gap (what’s missing for `BL-NEW-SIGNAL-TRUST-ROADMAP` usefulness):

- No single scorecard surface that combines maturity state + recent cohort stats + explicit sample-size warnings.
- No explicit “actionable vs would_be_live disagreement” rate per signal family in one place.
- Signal Trust tab currently renders the registry only (no objective cohort evidence).

## Hermes-first analysis (§7b)

This work is DB aggregation + dashboard presentation over Gecko-owned truth. Hermes does not own these primitives.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Read-only scorecards over `paper_trades` | none found | build from scratch (KEEP_CUSTOM) |
| Trust registry export + validation | none found | keep custom (already in-tree) |
| “Why this signal is interesting/dangerous” explanation text | possibly (generic summarization skills) | defer to later; enrichment-only (BRIDGE_TO_HERMES if it fits) |

awesome-hermes-agent ecosystem check: completed (no skill found that provides Gecko-compatible, not-for-pruning signal scorecards; proceed KEEP_CUSTOM).

## Runtime-state verification (§9)

Before we claim any scorecard interpretation is “healthy/unhealthy”, verify on the target DB:

1. `paper_trades` has the required columns (`signal_type`, `status`, `opened_at`, `closed_at`, `pnl_usd`, `pnl_pct`, `actionable`, `would_be_live`)
2. Current event rate is sufficient for the default windows (7d/14d/30d) to produce non-trivial `n` for at least 1–2 signal families.
3. `would_be_live` semantics match the shipped live-eligibility definition (no hidden override gates).

This iteration still ships code that can run without prod access by falling back to “table missing / column missing” empty surfaces (as existing endpoints do), but the operator should treat the values as *informational* until verified against prod.

## Plan steps

1. **Define the V1 scorecard contract**
   - Windows: 7d / 14d / 30d (configurable via query params with bounds).
   - For each `signal_type`, return:
     - closed trades `n`, `wins`, `win_rate_pct`, `total_pnl_usd`, `avg_pnl_pct`, `median_pnl_pct`
     - open trades `open_count`, `open_exposure_usd`
     - `actionable_rate` and `would_be_live_rate` over the closed-trades cohort in-window
     - registry fields: `maturity_state`, `data_quality.warning`, `next_gate`
     - sample-size warnings (e.g., `n<10` => “low_n”)
2. **Backend implementation**
   - Add `dashboard/db.py:get_signal_trust_scorecards(...)` (read-only).
   - Add `dashboard/api.py` handler `/api/signal_trust/scorecards`.
   - Add `dashboard/models.py` response model(s).
3. **Frontend**
   - Extend `SignalTrustTab.jsx` to render a scorecards table (sortable client-side).
   - Replace index-based React keys with `signal_type` for registry rows.
4. **Tests**
   - Add focused backend tests for:
     - empty DB/table-missing behavior
     - window bounds validation
     - deterministic ordering of returned rows (stable sort key)
     - basic aggregation correctness on a tiny seeded DB
5. **Verification**
   - `uv run pytest -q` on the new tests plus existing Signal Trust tests.
   - `npm.cmd run build:codex` if frontend sources change.

## Acceptance criteria

- Operator can see one table that includes:
  - maturity state (registry) + objective recent performance (DB-derived)
  - explicit sample-size warnings
  - explicit actionable-vs-would_be_live disagreement signals
- No behavior changes: read-only endpoint + dashboard view only.

