**New primitives introduced:** read-only endpoint `GET /api/signal_trust/scorecards`; Signal Trust dashboard scorecards sub-panel.

# Plan - Signal trust scorecards (BL-NEW-SIGNAL-TRUST-ROADMAP) - 2026-05-25

## Goal

Ship a read-only signal-family scorecards surface that joins:

- the existing V1 trust registry (`/api/signal_trust_registry`) maturity states, and
- objective recent performance + cohort stats from the Gecko DB (`paper_trades`)

into one operator-visible panel that answers: "which signal families look healthy right now, and which are context-only or data-insufficient?"

V1 remains **read-only** and **not for pruning / suppression / auto-disable / sizing / execution / source ranking**. These anti-scope claims are part of the API meta contract, not only prose.

## Non-goals

- No parameter changes, config flips, kill-switch changes, paid/vendor calls, pruning, sizing, or execution.
- No Hermes dependency for truth computation; Hermes may be used later for explanation/enrichment only.
- No schema migrations in this iteration.
- No signal-family promotion/demotion verdicts. The surface provides evidence and caveats only.

## Review folds - 2026-05-26 refresh

- Reuse the validated `/api/signal_trust_registry` loader for scorecards rather
  than silently loading registry JSON inside the DB helper.
- Treat unknown DB `OperationalError`s as endpoint failures. Do not convert
  required schema/query failures into `$0` exposure or empty rows.
- Add machine-readable anti-scope to scorecard metadata:
  `not_live_eligibility_verdict=true`,
  `cohort_policy=full_closed_paper_trades`, and
  `sort_policy=signal_type_asc_not_ranked`.
- Add an executable anti-scope test that fails if the scorecards endpoint/helper
  is consumed from alerting, pruning, execution, source-ranking, or scripting
  paths.
- Rebuild and commit the dashboard `dist` asset so deployed static files include
  the new Signal Trust scorecards panel.

## Drift-check (Section 7a)

In-tree primitives already exist:

- Trust registry export: `/api/signal_trust_registry` + `dashboard/frontend/components/SignalTrustTab.jsx`
- Trading stats by signal: `/api/trading/stats/by-signal` and `/api/trading/stats/by-signal-cohort`

Residual gap:

- No single scorecard surface combines maturity state + recent cohort stats + explicit sample-size warnings.
- No explicit actionable-vs-would_be_live disagreement rate per signal family in one place.
- Signal Trust currently renders the registry only, without objective cohort evidence.

**Plan fold:** do not duplicate existing aggregation logic. The new scorecards endpoint composes:

- registry file + existing `get_trading_stats_by_signal_cohort`, and
- a minimal incremental query for actionable/would_be_live stamp coverage + disagreement confusion matrix.

## Hermes-first analysis (Section 7b)

This work is DB aggregation + dashboard presentation over Gecko-owned truth. Hermes does not own these primitives.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Read-only scorecards over `paper_trades` | none found | build from scratch (KEEP_CUSTOM) |
| Trust registry export + validation | none found | keep custom (already in-tree) |
| Signal explanation text | possible generic summarization skills | defer; enrichment-only if later useful |

awesome-hermes-agent ecosystem check: completed 2026-05-25. No skill was found that provides Gecko-compatible, not-for-pruning signal scorecards.

## Runtime-state verification (Section 9)

Before treating any value as a signal health verdict, verify on the target DB:

1. `paper_trades` has required columns: `signal_type`, `status`, `opened_at`, `closed_at`, `pnl_usd`, `pnl_pct`, `actionable`, `would_be_live`, `amount_usd`.
2. NULL policy: measure how often `actionable` / `would_be_live` are NULL so rates are not misread.
3. Current event rate is sufficient for the fixed 7d/14d/30d windows to produce non-trivial `n`.
4. `would_be_live` semantics match the shipped live-eligibility definition.

The endpoint returns an explicit 503 when required scorecard columns are
missing. Only optional stamp columns (`actionable`, `would_be_live`) degrade to
`stamps_unavailable`, because older DB copies may not have those stamps.

Runtime smoke queries before deploy:

```sql
PRAGMA table_info(paper_trades);
SELECT status, COUNT(*) FROM paper_trades GROUP BY status;
SELECT
  SUM(CASE WHEN closed_at IS NOT NULL AND julianday(closed_at) >= julianday('now','-7 days') THEN 1 ELSE 0 END) AS closed_7d,
  SUM(CASE WHEN closed_at IS NOT NULL AND julianday(closed_at) >= julianday('now','-14 days') THEN 1 ELSE 0 END) AS closed_14d,
  SUM(CASE WHEN closed_at IS NOT NULL AND julianday(closed_at) >= julianday('now','-30 days') THEN 1 ELSE 0 END) AS closed_30d
FROM paper_trades
WHERE status!='open';
SELECT
  COUNT(*) AS closed_30d,
  SUM(CASE WHEN actionable IS NULL THEN 1 ELSE 0 END) AS actionable_nulls,
  SUM(CASE WHEN would_be_live IS NULL THEN 1 ELSE 0 END) AS would_be_live_nulls
FROM paper_trades
WHERE status!='open' AND closed_at IS NOT NULL AND julianday(closed_at) >= julianday('now','-30 days');
SELECT signal_type, COUNT(*) AS open_count, COALESCE(SUM(amount_usd),0) AS open_exposure_usd
FROM paper_trades
WHERE status='open' AND closed_at IS NULL
GROUP BY signal_type
ORDER BY signal_type;
```

## Plan steps

1. Define the V1 scorecard contract.
   - Fixed windows: `[7, 14, 30]`.
   - Closed-trade stats are computed on `closed_at`.
   - Open stats are current `status='open'` rows and are not windowed.
   - Win definition follows existing cohort stats: `pnl_usd > 0`.
   - No median/percentile metrics in V1.
   - Return registry fields, open exposure, closed-trade stats, stamp coverage, confusion counts, disagreement rate, and sample-size warnings.

2. Backend implementation.
   - Add `dashboard/db.py:get_signal_trust_scorecards(...)`.
   - Reuse `get_trading_stats_by_signal_cohort(db_path, days=...)` for closed stats.
   - Add minimal aggregate queries for stamp coverage and open exposure.
   - Reuse the same registry validation/path-override contract as `/api/signal_trust_registry`.
   - Add `dashboard/api.py` handler `/api/signal_trust/scorecards`.
   - Add Pydantic response models in `dashboard/models.py`.

3. Frontend.
   - Extend `SignalTrustTab.jsx` to fetch `/api/signal_trust/scorecards`.
   - Render a scorecards table under the existing registry panel.
   - Use stable row keys derived from `signal_type`.

4. Tests.
   - Endpoint invariants and `Cache-Control: no-store`.
   - Missing `paper_trades` table returns 503 with structured error.
   - Missing stamp columns degrades to `data_missing_reason=stamps_unavailable`.
   - Deterministic row/window ordering.
   - NULL stamps are unknown, not false.
   - Window boundary respects time of day for ISO timestamps.

5. Verification.
   - `uv run pytest -q tests/test_signal_trust_scorecards_endpoint.py`
   - Relevant dashboard endpoint tests.
   - `npm.cmd run build:codex`

## Acceptance criteria

- Operator can see maturity state + objective recent performance + explicit sample-size caveats in one read-only panel.
- The endpoint and UI do not change trading behavior.
- Missing historical columns do not break the dashboard.
- Contract remains visibility-only and explicitly not for pruning/auto-disable.
