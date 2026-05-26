**New primitives introduced:** tracker-promoted Trade Inbox rows; promotion-source metadata counters. No new DB table, no alerting primitive, no execution primitive.

# Tracker Cockpit Promotion Path Plan

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard decision-support queue composition | None found that composes gecko-alpha SQLite `paper_trades` with Top Gainers tracker rows. | Build in repo; the source-of-truth and bucket semantics are local. |
| Tracker-to-cockpit promotion | None found for watcher/tracker corpus promotion into the existing Trade Inbox. | Build in repo as read-only API composition. |
| Ranking / urgency tiers | Not used. | Defer until promoted queue volume proves a ranking problem. |
| Telegram alert qualification | Not used. | Defer until tracker promotion is deployed and measured. |

Awesome-hermes-agent ecosystem check: no checked Hermes ecosystem primitive replaces a local dashboard query over `gainers_comparisons`, `gainers_snapshots`, `price_cache`, and `paper_trades`.

Verdict: use the existing dashboard API and models; do not introduce Hermes runtime coupling.

## Drift / Runtime Findings

- `/api/trade_inbox` is already the trader-facing review queue; it currently scans only `paper_trades.status='open'`.
- `/api/live_candidates` and the Trade Inbox are downstream of paper rows, so disabled paper signals such as `gainers_early` cannot surface tracker-only winners.
- `gainers_comparisons` stores watcher corpus rows with `appeared_on_gainers_at`, lead flags, `price_change_24h`, and optional `detected_price`.
- `price_cache` may contain fresher price/mcap context, but tracker rows must remain useful with only snapshot/comparison data.
- `trade_decision_events` already instruments admission/skip decisions; this PR is a read-only dashboard composition and does not add a new pipeline writer, so §12a watchdog does not apply.

## Scope

1. Extend `/api/trade_inbox` to include recent Top Gainers tracker rows that do not already have an open paper-backed row.
2. Label promoted rows with `source_corpus="tracker"` and `surfaces` containing `top_gainers_tracker`, while preserving paper-backed rows as `source_corpus="paper"`.
3. Enrich matching paper-backed rows with `top_gainers_tracker` before suppressing tracker-only duplicates.
4. Keep tracker-promoted rows dashboard-only: no paper trade insert, no live execution, no signal re-enable, no Telegram alert.
5. Add metadata counters for paper rows vs tracker-promoted rows so the future alert-quality design has a measured queue-volume trigger.
6. Update the Trade Inbox copy and row keys so tracker-only rows are visible and stable.
7. Record the soak criterion in `tasks/todo.md` and the lesson that ranking/tiering waits until surface completeness plus volume measurement.

## Promotion Universe Pin

A tracker-promoted candidate is a recent `gainers_comparisons` row whose `appeared_on_gainers_at` is inside the Trade Inbox `window_hours`, has a non-empty `coin_id`, has enough display identity (`symbol` or `name`), and does not already have an open paper-backed row for the same `coin_id`.

## Soak Metric

The promotion PR records `tracker_rows_promoted` and `paper_rows_considered` in the API metadata for live operator visibility. The alert-design trigger must use a request-independent daily count:

- Fixed query window: Trade Inbox defaults, `window_hours=36`.
- Unique key: one promoted `coin_id` per UTC day, where the UTC day is derived from `appeared_on_gainers_at`.
- Dedupe: repeated dashboard/API requests do not add fires; the count is computed from source rows, not from rendered responses.
- Inclusion: count only rows that satisfy the promotion universe and have no open paper-backed row at measurement time.
- Export path: use `scripts/trade_inbox_tracker_promotion_soak.sql` from prod; do not use ad-hoc manual screenshots.
- Maturity: queue-volume counts are immediate, but outcome judgments require a lookback-mature cohort and must not use future-runner labels for live ranking.

Alert qualification design remains blocked until deployed data shows `>= 5` unique tracker-promoted `coin_id`s/day for `>= 3` mature UTC days, or the 14-day calendar backstop closes with an explicit low-volume decision.

## Non-Scope

- No `TRADE_NOW`, `WATCH_BREAKOUT`, `RESEARCH_ONLY`, or other urgency tiers.
- No TG alert thresholds or Telegram sends.
- No paper trading dispatch changes.
- No `gainers_early` re-enable.
- No new durable writer table.
- No new ranking formula, queue priority tuning, or urgency/order changes beyond preserving the existing Trade Inbox group/sort behavior while adding visibility.
- No cross-identifier resolver between contract-address paper rows and CoinGecko tracker rows; this PR dedupes only when identifiers already match.

## Build Steps

- [ ] Add failing Trade Inbox endpoint tests for tracker-only rows, dedupe/enrichment behind open paper rows including rows beyond display cap, stale/no-price routing, metadata counters, and tracker truncation.
- [ ] Extend `dashboard.models.TradeInboxRow` and `TradeInboxMeta` with source-corpus and promotion counters.
- [ ] Refactor `dashboard.db.get_trade_inbox` to build rows from paper and tracker sources through shared scoring/grouping logic.
- [ ] Update `dashboard/frontend/components/TradeInboxTab.jsx` copy and stable seen/dismiss/render keys for tracker-only rows.
- [ ] Run focused backend tests and frontend build.
- [ ] Open PR and request two parallel reviews: one product/strategy review for scope creep, one code/schema/API review for correctness.
