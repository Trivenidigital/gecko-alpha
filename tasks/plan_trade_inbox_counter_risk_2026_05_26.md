**New primitives introduced:** Trade Inbox counter-risk context fields (`counter_risk_score`, `counter_flags`, `counter_risk_predicted_at`), supporting predictions coin/timestamp index, and read-only UI context.

# Trade Inbox Counter-Risk Context Plan

**Goal:** Surface existing narrative counter-risk context on `/api/trade_inbox` paper rows so the trader can see why a visible opportunity may be fragile, without changing ranking, grouping, dispatch, or alerts.

**Architecture:** Reuse the existing `predictions` table as the source of truth, selecting the latest prediction per open paper `token_id`. Add closed-contract fields to Trade Inbox rows, render them as compact informational context in `TradeInboxTab`, and keep tracker-only rows explicitly empty until a CoinGecko-id prediction mapping exists.

**Tech Stack:** FastAPI/Pydantic dashboard models, SQLite read-only dashboard queries, React/Vite dashboard frontend, existing contract smoke checker.

## Drift Check

- PR #278 is open but targets `NowTradableTab.jsx`; current backlog audit marked it relevant but stale because Trade Inbox is now the primary trader surface.
- `dashboard/db.py` already enriches `/api/live_candidates` from `predictions.counter_risk_score` and `predictions.counter_flags`; the same parsing rules should be reused for Trade Inbox.
- `/api/trade_inbox` has a closed contract firewall in `scripts/check_trade_inbox_contract.py`; any new row fields must be added to the checker and tests.
- `TradeInboxTab.jsx` currently renders Token, Action, Window, Score, price context, and Why/Risk only. It has no counter-risk display.

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard row contract extension | none checked in local repo/Hermes conventions apply only to external reusable skills | Build from existing in-tree contract checker pattern. |
| Counter-risk scoring/parsing | none needed; existing in-tree `predictions` schema and live-candidates parser already own this data | Reuse in-tree logic instead of introducing a new primitive. |
| React badge rendering | none needed | Build from existing dashboard components/styles. |

Awesome-Hermes ecosystem verdict: no external Hermes skill is appropriate for a small in-tree dashboard contract/UI extension.

## Scope

- Add `counter_risk_score: int | None`, `counter_flags: list[dict | str]`,
  and `counter_risk_predicted_at: str | None` to each Trade Inbox row.
- Populate fields for `source_corpus="paper"` rows from latest prediction by `coin_id == token_id`.
- Populate tracker-only rows as `counter_risk_score=None`, `counter_flags=[]`.
- Populate tracker-only `counter_risk_predicted_at=None`.
- Render compact read-only counter-risk context in Trade Inbox. Empty values must say
  `Counter-risk unavailable`, not imply zero risk.
- Add an index on `predictions(coin_id, predicted_at, id)` so the optional
  enrichment does not turn Trade Inbox into a latency-heavy dashboard query.
- Supersede PR #278 after replacement PR is opened or merged.

## Anti-Scope

- No row ranking, sorting, score, group, action-label, or dispatch changes.
- No Telegram alerts or TG alert qualification.
- No urgency tiers such as `TRADE_NOW`, `WATCH_BREAKOUT`, or `RESEARCH_ONLY`.
- No cross-identifier resolver between paper contract ids and CoinGecko ids.
- No signal parameter, auto-suspend, execution, pruning, or paper-trade policy changes.

## Implementation Checklist

- [ ] Plan review by two parallel agents: contract/runtime angle and trader-UX/product angle.
- [ ] Design doc after plan review folds.
- [ ] Design review by two parallel agents.
- [ ] Red tests:
  - endpoint test proving paper rows expose latest counter-risk prediction;
  - endpoint test proving tracker-only rows carry empty counter-risk fields;
  - endpoint regression proving different counter-risk values do not change
    `group`, `action_label`, `trade_score`, or relative sort;
  - contract test accepting rich dict/string flags and rejecting invalid flag items;
  - frontend static test proving Trade Inbox renders counter-risk context.
- [ ] Backend:
  - fetch latest predictions for `token_order`;
  - parse `counter_flags` using the same defensive list-of-dict/string rule as live candidates;
  - degrade gracefully to empty context if `predictions` or its counter-risk
    columns/indexed window query are unavailable;
  - expose `counter_risk_predicted_at` for freshness context;
  - attach fields without feeding `_trade_score`, `_trade_sort_key`, `_trade_block_reason`, or grouping logic.
- [ ] Pydantic and contract:
  - add row fields to `TradeInboxRow`;
  - update `EXPECTED_ROW_KEYS` and type validation in `scripts/check_trade_inbox_contract.py`;
  - mirror live-candidates `counter_flags` validation for dict/string items and
    nested banned-language scanning;
  - keep anti-scope firewall active for alert/ranking/urgency terms.
- [ ] Frontend:
  - display neutral `Counter-risk context` with a secondary numeric value and
    source age, avoiding high/medium/low labels or threshold colors;
  - display up to two sanitized flag labels/details in the Why/Risk cell;
  - display `Counter-risk unavailable` for null/empty fields;
  - keep display informational, not filterable/sortable.
- [ ] Verification:
  - `uv run pytest -q tests/test_trade_inbox_endpoint.py tests/test_check_trade_inbox_contract.py tests/test_dashboard_frontend_layout.py`;
  - `npm.cmd --prefix dashboard/frontend run build:codex`;
  - dashboard contract smoke tests;
  - full suite with dummy required secrets before merge.

## Review Notes

- Counter-risk values are operator context, not a truth label. They must not become an implicit live ranking feature.
- `counter_flags` content comes from narrative output and may contain rich dicts; the contract accepts dict or string items but rejects garbage items.
- Tracker rows remain empty because joining CoinGecko ids to paper prediction ids would recreate the cross-id resolver item that was audited as phantom.

## Plan Review Folds

- Product/UX review: avoid `CR <score>` as a ranking-looking badge; use neutral
  context copy, show unavailable state explicitly, include prediction freshness,
  cap/sanitize flags, and test that counter-risk cannot affect grouping/sorting.
- Runtime/contract review: optional prediction enrichment must fail soft like
  live candidates, add a coin/timestamp prediction index, mirror live-candidates
  flag validation, and make anti-scope executable through regression tests.
