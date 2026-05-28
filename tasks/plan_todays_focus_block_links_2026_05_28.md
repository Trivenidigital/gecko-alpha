**New primitives introduced:** `block_cause` factual classifier on `/api/todays_focus`; explicit Today's Focus research-link chips.

# Today's Focus Block Links Plan

**Goal:** Make Today's Focus more usable for a trader scanning candidates by exposing factual block cause and direct research links without adding ranking, urgency, alerts, advice, sizing, or execution.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard UI composition | None found in Hermes bundled skills catalog that applies to gecko-alpha's React dashboard contract. | Build in existing React component. |
| Trading decision support / queue triage | None found in Hermes bundled skills catalog that owns this project's DB schema, signal contracts, or anti-scope firewalls. | Build repo-local factual fields only. |
| External chart/research linking | No Hermes skill found for deterministic CoinGecko/DexScreener link rendering inside this dashboard. | Build small frontend helper; no API calls. |

Awesome-Hermes/HermesHub check: general project-planning and API-builder skills exist, but no domain skill covers gecko-alpha Today's Focus row contracts or trader-surface anti-scope, so custom repo-local work is warranted.

## Drift-check

Existing primitives:
- `/api/todays_focus` already returns five rows from `/api/trade_inbox` with fixed 3 paper / 2 tracker curation.
- `TodayFocusPanel.jsx` already renders compact rows and localStorage save/dismiss/note state.
- `TokenLink.jsx` already links the token title to CoinGecko or DexScreener.
- `scripts/check_todays_focus_contract.py` already enforces no advice/ranking/urgency language.

Residual gaps:
- A blocked row exposes `block_reason_primary` but does not distinguish data-path/visibility causes from data-quality/actionability causes.
- The token title link is easy to miss; there are no explicit same-row `Chart` / `CG` chips for fast inspection.
- The frontend does not visually separate block category from generic diagnostics.

## Scope

Build:
1. Add a backend `block_cause` field to Today's Focus rows:
   - `data_path`: blocked by visibility/path classification; no implication about review priority or trade suitability.
   - `data_quality`: blocked by price/data/actionability quality; no implication about review priority or trade suitability.
   - `unknown`: blocked but not classifiable from current reason fields.
   - `null`: not a blocked row.
2. Add explicit frontend research chips:
   - `Chart` opens DexScreener search/contract route.
   - `CG` opens CoinGecko coin page/search route.
   - Link generation is deterministic and local; no external requests.
3. Render the cause as factual copy: `block=data_path`, `block=data_quality`, or `block=unknown`.
4. Extend the Today's Focus contract firewall and tests.

Non-scope:
- No ranking among the five rows.
- No urgency tiers or "best now" labels.
- No buy/sell/consider/watch-breakout/entry-late language.
- No Telegram alerts.
- No order execution or Kraken integration.
- No server-side personal position state.
- No cross-identifier resolver.

## Classification Rules

`data_path` when no immediate data-quality blocker is present and any risk/inclusion reason contains:
- `tracker_only_no_paper_trade`
- `actionable_null_pre_cutover`
- `would_be_live=0`
- future explicit identity/corpus/linkage reason strings containing `corpus`, `linkage`, or `identifier_mismatch`

`data_quality` when:
- `block_reason_primary` is one of `NO_PRICE`, `STALE_PRICE`, `NOT_ACTIONABLE`, `BAD_TIMESTAMP`, `DATA_INSUFFICIENT`; or
- risk reasons include `price_is_stale`, `not_actionable`, `opened_at_unparseable`, `price_timestamp_unparseable`, `detected_price_missing_or_invalid`, `entry_price_missing_or_invalid`, or `no_price_snapshot_for_token_id`.

`data_quality` wins over `data_path` if both appear. This keeps `block_cause`
focused on the immediate blocker; tracker-only visibility context remains
available in `risk_reasons`.

## Implementation Checklist

- [ ] Add failing endpoint/contract tests for `block_cause`.
- [ ] Add failing frontend static tests for explicit `Chart` / `CG` chips and category rendering.
- [ ] Add backend helper in `dashboard/db.py` and include field in `_today_focus_row`.
- [ ] Add Pydantic model field in `dashboard/models.py`.
- [ ] Update `scripts/check_todays_focus_contract.py` closed schema and allowed values.
- [ ] Add local React link helpers and row rendering.
- [ ] Add CSS for compact link/category chips including 375px mobile.
- [ ] Refresh frontend dist.
- [ ] Run focused tests and dashboard build.
- [ ] Open PR, get three orthogonal reviews, fold issues, merge, deploy, smoke `/api/todays_focus`.
