# Design: BL-NEW-LIVE-DECISION-COCKPIT (V1) — `/api/live_candidates` (read-only)

Date: 2026-05-23

## Goal

Ship a **read-only**, **deterministic** endpoint that returns a small list of per-token “live candidates” derived from existing DB truth (paper-trades + current price snapshot) with conservative labels and explicit reasons.

V1 is visibility-only: no live execution, sizing, suppression, pruning, source ranking, or actionability-v2 consumption.

## **New primitives introduced**

- `dashboard/db.py`: `get_live_candidates(db_path: str, *, limit: int, window_hours: int) -> list[dict]`
- `dashboard/models.py`: `LiveCandidateResponse`
- `dashboard/api.py`: `GET /api/live_candidates`
- `tests/test_live_candidates_endpoint.py`

## Hermes-first analysis (design posture)

Use Hermes for enrichment only (optional future), never as the substrate for price truth, PnL attribution, identity, or execution decisions.

- Public hub + ecosystem scan is documented in the plan: `tasks/plan_live_decision_cockpit_v1_2026_05_23.md`.
- Deployed-surface check on the target VPS is mandatory before claiming “no skill fits”.

Deployed-surface evidence to capture (paste into PR description):

```bash
ls -la ~/.hermes || true
ls -la ~/.hermes/skills || true
ls -la ~/.hermes/cron || true
test -f ~/.hermes/cron/jobs.json && jq '.jobs | keys' ~/.hermes/cron/jobs.json | head
```

## Runtime-state verification (required)

This feature’s usefulness depends on runtime schema + join invariants. Before deploy, verify:

- `paper_trades.token_id` joins to `price_cache.coin_id` with non-trivial hit-rate for the open set.
- `opened_at` is parseable ISO timestamps (or at least consistent type).
- `price_cache.updated_at` is fresh enough that V1 isn’t mostly `data_insufficient`.

Proof queries (copy/paste):

```bash
sqlite3 scout.db "SELECT COUNT(*) FROM paper_trades WHERE status='open';"
sqlite3 scout.db "SELECT typeof(opened_at), COUNT(*) FROM paper_trades GROUP BY 1;"
sqlite3 scout.db "SELECT COUNT(*) FROM paper_trades WHERE entry_price IS NULL OR entry_price <= 0;"
sqlite3 scout.db "SELECT MIN(updated_at), MAX(updated_at) FROM price_cache;"
sqlite3 scout.db "SELECT COUNT(*) FROM paper_trades pt JOIN price_cache pc ON pc.coin_id = pt.token_id WHERE pt.status='open';"
```

### Join invariants (V1 explicit)

- `paper_trades.token_id` is an opaque asset key (often CoinGecko `coin_id`, but may be a contract address).
- `price_cache.coin_id` and `predictions.coin_id` are CoinGecko `coin_id`.
- Therefore price/prediction enrichments are best-effort: they only populate when `paper_trades.token_id == <coin_id>`.
- If the join misses, V1 labels `verdict=data_insufficient` with an explicit reason like `no_price_snapshot_for_token_id`.

## API contract

`GET /api/live_candidates?limit=20&window_hours=36`

Parameter caps (hard):
- `limit`: 1..50
- `window_hours`: 6..72

Response: `list[LiveCandidateResponse]`

Response includes an explicit disclaimer field:
- `disclaimer`: constant string: “read-only labels; not trading advice; triggers no actions”

## Response model (V1)

Per-token row shape:

- Identity:
  - `token_id: str`
  - `symbol: str`
  - `name: str`
  - `chain: str`
- Paper-trade evidence:
  - `open_trade_ids: list[int]`
  - `recent_trade_ids: list[int]`
  - `surfaces: list[str]` (distinct `signal_type`)
  - `actionable: int | None` (0/1/NULL)
  - `would_be_live: int | None` (0/1/NULL)
  - `opened_at: str | None` (most recent open)
  - `entry_price: float | None` (from open trade)
  - `pct_from_entry: float | None`
- Price snapshot:
  - `current_price: float | None`
  - `market_cap: float | None`
  - `price_change_24h: float | None`
  - `price_updated_at: str | None`
  - `price_is_stale: bool`
- Optional enrichments (never required for row to exist):
  - `narrative_fit_score: int | None`
  - `counter_risk_score: int | None`
  - `counter_flags: list[str]`
  - `latest_chain_match: dict | None` (pattern + completed_at; V1 optional)
- Labels:
  - `entry_quality: str` enum:
    - `fresh_entry`
    - `acceptable_pullback`
    - `already_ran`
    - `already_faded`
    - `too_stale`
    - `data_insufficient`
  - `verdict: str` enum:
    - `candidate`
    - `watch`
    - `blocked`
    - `data_insufficient`
  - `inclusion_reasons: list[str]`
  - `risk_reasons: list[str]`

## Candidate cohort (query semantics)

Primary cohort:
- `paper_trades` where `status='open'`
- never hide an open position. `window_hours` applies only to optional context (`recent_trade_ids`), not the primary open-trade cohort.

Notes:
- The engine already prevents multiple open trades per token; still group defensively by `token_id`.
- Closed/history trades are optional context only (V1 will return `recent_trade_ids` limited to the window, but not compute PnL cohorts).

## DB query design

### Base rows (required)

Implementation constraint (V1): 2-phase batch lookups (avoid join-multiplication).

1) Fetch open trades from `paper_trades` (bounded set; engine blocks >1 open per token_id).
2) Batch-fetch `price_cache` rows with `coin_id IN (...)` for those token_ids.
3) Batch-fetch optional latest `predictions`/`chain_matches` via window-function subqueries constrained by `IN (...)`.

### Optional enrichment selection (latest-per-token)

Predictions:
- If `predictions` exists and has `coin_id`, take the latest row per `coin_id` by `predicted_at`:
  - window function: `ROW_NUMBER() OVER (PARTITION BY coin_id ORDER BY predicted_at DESC, id DESC) = 1`
- Decode `counter_flags` JSON text into `list[str]` if present.
- V1 rule: `counter_risk_score` is display-only (enrichment-only); never the sole cause of `blocked` or `candidate → watch` without an explicit reviewed threshold.

Chain matches:
- If `chain_matches` exists, take latest by `completed_at` for `token_id`.
  - window function: `ROW_NUMBER() OVER (PARTITION BY token_id ORDER BY completed_at DESC, id DESC) = 1`
  - include `pipeline` in the payload; do not assume a single pipeline.

### Degenerate states

- Missing `price_cache` row: keep token row but set `verdict=data_insufficient` and add reason.
- Stale `price_cache.updated_at`:
  - `price_is_stale = age > 1h` (warning label)
  - `verdict=data_insufficient` only when `age > 2h` (extreme stale)

## Deterministic labeling rules (V1)

No LLM calls, no probabilistic heuristics.

### Entry quality

Compute `pct_from_entry` when `entry_price` + `current_price` are both present.

- `fresh_entry`: -2% .. +8%
- `acceptable_pullback`: -6% .. +15%
- `already_ran`: > +25%
- `already_faded`: < -10%
- `data_insufficient`: missing prices or invalid entry

### Verdict

- `data_insufficient` if missing price snapshot OR invalid timestamps OR extreme stale (>2h)
- `blocked` if `actionable == 0`
- `candidate` if `actionable == 1` AND `would_be_live == 1` AND `entry_quality in {fresh_entry, acceptable_pullback}`
- otherwise `watch`

Counter-risk downgrade (optional, enrichment-only):
- Display-only in V1: surface in `risk_reasons` when present, but do not change `verdict` until coverage/range and a concrete threshold are verified and reviewed.

## Tests

Create a new test module using the existing pattern:
- Seed DB via `scout.db.Database.initialize()` (so schema/migrations stay aligned).
- Insert:
  - at least one open trade with `actionable=1`, `would_be_live=1`
  - a matching `price_cache` row with fresh `updated_at`
  - cases for: missing price_cache, extreme stale price, actionable=0 -> blocked
- Validate:
  - endpoint returns 200
  - parameter caps enforced (422 from FastAPI Query bounds)
  - `verdict` + `entry_quality` match deterministic rules
  - response never writes (no tables mutated beyond inserts in test setup)

Additional V1 tests (optional enrichments):
- “latest prediction wins”: insert two `predictions` rows for the same `coin_id` with different `predicted_at`.
- “latest chain match wins”: insert two `chain_matches` rows for the same `token_id` with different `completed_at`.

## Safety / operator gates

- Endpoint is read-only (`_ro_db` for all reads).
- No per-source ranking; TG/X not used for boosting.
- Labels are non-imperative (`candidate/watch/blocked/data_insufficient`), plus a disclaimer string in every row.
