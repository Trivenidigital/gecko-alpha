**New primitives introduced:** tracker-promoted Trade Inbox rows; promotion-source metadata counters. No new DB table, no alerting primitive, no execution primitive.

# Tracker Cockpit Promotion Path Design

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard decision-support queue composition | None found for this repo's SQLite Trade Inbox. | Build in `dashboard/db.py` and `dashboard/models.py`. |
| Tracker-to-cockpit promotion | None found for `gainers_comparisons` -> Trade Inbox composition. | Build local read-only query composition. |
| Queue ranking / urgency classification | Not applicable to this PR. | Defer until volume measurement. |
| Telegram alert qualification | Not applicable to this PR. | Defer until promotion soak. |

Awesome-hermes-agent ecosystem check: no ecosystem skill replaces the local dashboard composition over gecko-alpha tracker and paper-trade tables.

Verdict: custom in-repo implementation is warranted; keep it as API/model composition with no runtime Hermes dependency.

## API Contract

`GET /api/trade_inbox` remains the endpoint. Response shape is backward compatible with additive fields:

- `TradeInboxRow.source_corpus`: `"paper"` or `"tracker"`.
- `TradeInboxRow.open_trade_ids`: empty for tracker-only rows.
- `TradeInboxRow.recent_trade_ids`: empty unless future enrichment adds recent paper rows.
- `TradeInboxRow.surfaces`: includes paper signal names for paper rows and `top_gainers_tracker` for tracker rows.
- `TradeInboxMeta.paper_rows_considered`: number of deduped paper-backed source rows scored.
- `TradeInboxMeta.tracker_rows_considered`: number of tracker rows scanned after the time-window filter.
- `TradeInboxMeta.tracker_rows_promoted`: tracker rows actually added after dedupe.

Existing groups stay unchanged: `act_now`, `watch`, `already_ran`, `blocked`.

## Query Shape

1. Read open `paper_trades` as today, preserving deterministic order and token-level dedupe.
2. Preload price data for paper token ids from `price_cache`.
3. Read recent `gainers_comparisons` rows:
   - `appeared_on_gainers_at >= now - window_hours`
   - non-empty `coin_id`
   - deterministic order: `appeared_on_gainers_at DESC`, `id DESC`
   - scan cap based on existing `source_limit`
4. Record recent tracker matches by `coin_id` before suppressing tracker-only rows, so paper-backed rows can add `top_gainers_tracker` to `surfaces`.
5. Suppress tracker-only emission when `coin_id` matches any scanned open paper-backed row, not only displayed paper rows.
5. Preload `price_cache` for tracker token ids.
6. Build all rows through the same group/sort pipeline, without changing the existing paper-row ranking formula or queue priority semantics.

## Tracker Row Semantics

Tracker rows use the first Top Gainers appearance as the review timestamp:

- `opened_at = appeared_on_gainers_at`
- `opened_age_hours` = age from first Top Gainers appearance
- `entry_price = detected_price` when available; tracker rows without a positive detected price are data-insufficient even if current price exists
- `pct_from_entry = current_price vs entry_price` when both are present
- `actionable = None`, `would_be_live = None`
- `verdict = "watch"` when price and timestamp are usable and not hard-stale
- `verdict = "data_insufficient"` when price/timestamp is missing or hard-stale
- `inclusion_reasons = ["tracker_promotion", "top_gainers_tracker"]`
- `risk_reasons` includes `tracker_only_no_paper_trade` so the UI makes the source distinction visible.

This deliberately avoids urgency tiers. A tracker row can land in `watch`, `already_ran`, or `blocked` according to existing price/window/staleness rules. It should not be promoted into `act_now` until a separate urgency design exists. This PR must not tune the existing paper-row ranking formula or reorder priorities to make tracker rows look more urgent.

## Deduplication

Paper-backed rows win when identifiers match. If a token has an open paper trade and a recent tracker row with the same `coin_id` / `token_id`, the paper row remains the single Trade Inbox row and adds `top_gainers_tracker` to its `surfaces` when the tracker row is recent. This prevents duplicate rows while preserving the multi-surface signal.

Known residual: this does not resolve contract-address paper rows to CoinGecko ids. If the same asset is represented by different identifiers, duplicate rows can still happen. That requires a separate cross-id resolver and should not be smuggled into this promotion PR.

## Metadata / Query Risk

The API metadata must expose both scanned and capped state:

- `tracker_rows_considered`: tracker rows scanned and scored after the time-window filter.
- `tracker_rows_promoted`: tracker rows emitted after paper-row dedupe.
- `tracker_source_truncated`: true when the tracker query hits its scan cap.

The tracker scan cap is wider than the display source limit so recent tracker rows that duplicate open paper rows do not consume the whole promotion cohort. The endpoint uses `ORDER BY appeared_on_gainers_at DESC, id DESC` rather than wrapping the indexed/filter column in `datetime()`. This PR adds a lightweight index migration for `gainers_comparisons(appeared_on_gainers_at)`.

Operational soak measurement uses `scripts/trade_inbox_tracker_promotion_soak.sql`, which counts unique promoted tracker `coin_id`s by UTC day from source tables rather than repeated dashboard/API responses.

## Frontend

`TradeInboxTab.jsx` changes:

- Header copy says the inbox covers open paper trades plus promoted tracker rows.
- Row/session keys include `source_corpus` so a tracker-only row and a paper-backed row for the same token do not collide across refresh transitions.
- Both seen-state keys and dismiss/render keys must include `source_corpus`; otherwise dismissing a tracker row can suppress a later paper-backed row.
- Add a small source line under the token status.

## Tests

Focused tests in `tests/test_trade_inbox_endpoint.py`:

- tracker-only Top Gainers row appears in Watch with `source_corpus="tracker"`, `open_trade_ids=[]`, and `top_gainers_tracker` surface.
- open paper row dedupes tracker row and adds `top_gainers_tracker` to surfaces.
- tracker row with no usable price is blocked/data-missing without creating a paper row.
- metadata exposes paper and tracker counts.
- tracker source truncation is exposed when the tracker scan cap is hit.
- frontend static test covers source-corpus keys for seen/dismiss state.

## Failure Modes

- **Scope creep into urgency tiers:** blocked by tests expecting tracker-only rows to default to Watch when usable.
- **Duplicate token rows:** blocked by dedupe test.
- **Future alert design declares volume without evidence:** metadata counters give the soak gate direct evidence.
- **Hidden source confusion:** `source_corpus`, `surfaces`, and `tracker_only_no_paper_trade` are visible in API/UI.
- **Cross-id duplicate rows:** known residual when paper row uses contract address and tracker row uses CoinGecko id; do not claim this PR solves cross-identifier reconciliation.
