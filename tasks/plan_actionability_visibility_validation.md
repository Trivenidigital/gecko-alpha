**New primitives introduced:** `/api/trading/actionability` read-only endpoint; `dashboard.db.get_trading_actionability_summary`; `actionability=all|actionable|exploratory|unknown` history filter; Trading tab actionability summary/filter UI; `tasks/runbook_actionability_validation_2026_05_19.md`.

# Plan: Actionability Visibility + Validation

## Goal

Make PR #181's `paper_trades.actionable`, `actionability_reason`, and
`actionability_version` visible and measurable without changing paper/live
entry behavior.

## Drift Check

- `paper_trades` now has actionability columns via PR #181.
- `dashboard/db.py` already has paper-trade positions/history/stats helpers, but
  those helpers expose only `would_be_live`, not actionability metadata.
- `dashboard/frontend/components/TradingTab.jsx` already has live-eligible
  summary/filter UI, but no actionable/exploratory/unknown view.
- No existing `/api/trading/actionability` endpoint or validation runbook exists.

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Gecko paper-trade actionability dashboard | none found in Hermes Skills Hub | Build in repo; this reads gecko-alpha SQLite columns and existing dashboard state. |
| SQLite paper-trade PnL validation | none found in Hermes Skills Hub | Build a repo runbook with exact SQL; no Hermes runtime primitive owns this DB. |
| Generic dashboard analytics | partial ecosystem analogs only | Reuse existing `dashboard/` patterns; generic Hermes dashboards do not replace project-specific trading cohorts. |

Awesome-hermes-agent ecosystem check: generic agent analytics/dashboard plugins
exist, but no drop-in gecko-alpha `paper_trades` actionability/PnL cohort
surface. Verdict: custom in-repo visibility is justified.

## Scope

1. Backend API:
   - Add `actionable`, `actionability_reason`, and `actionability_version` to
     `/api/trading/positions` and `/api/trading/history` rows.
   - Add `actionability=all|actionable|exploratory|unknown` to
     `/api/trading/history` and `/api/trading/history/count`.
   - Add `/api/trading/actionability?days=7` summary with:
     - open counts by state,
     - closed counts and PnL by state,
     - top actionability reasons,
     - `unknown` count for pre-cutover / unstamped rows.

2. Dashboard:
   - Add concise actionability stat cards to the Trading tab.
   - Add an all/actionable/exploratory/unknown segmented filter for closed
     trades.
   - Show an actionability badge and reason/version per open and closed row.
   - Keep live-eligible UI separate; do not conflate `would_be_live` with
     `actionable`.

3. Validation runbook:
   - Fresh post-deploy row stamp check.
   - 24h actionable vs exploratory PnL query.
   - False-negative exploratory winners query.
   - Explicitly state no suppression/capital-allocation changes yet.

## Out of Scope

- X outcome linkage.
- TG outcome linkage.
- Source-quality ledger.
- Discovery-vs-entry attribution dashboard.
- Peak-giveback/freshness filter.
- Actual trade suppression or capital allocation based on actionability.

## TDD Tasks

- Add failing dashboard API tests in `tests/test_trading_dashboard.py` for row
  fields, history filter/count semantics, summary counts/PnL, and reason
  breakdown.
- Implement backend helpers in `dashboard/db.py` and route wiring in
  `dashboard/api.py`.
- Add dashboard UI changes in `dashboard/frontend/components/TradingTab.jsx`.
- Add validation runbook under `tasks/`.
- Verify with targeted pytest, existing actionability/paper tests, frontend
  build, and a browser/screenshot check if a local dashboard server is started.
