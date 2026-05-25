# Design — Live candidates determinism + contract delta (BL-NEW-LIVE-DECISION-COCKPIT) — 2026-05-25

## Intent

Make `/api/live_candidates` deterministic and ensure the frozen V1 contract is enforced by a validator that can be run against prod.

This is a **read-only hardening** delta: no new fields, no scoring changes, and no behavior that could trigger trading, pruning, or configuration flips.

## Design review folds (2-vector)

Applied critical/important folds from design reviewers:

- Timestamp coercion must be explicit: backend must emit `opened_at` / `price_updated_at` as ISO8601-or-null (never dirty strings), and keep the existing risk reasons `opened_at_unparseable` / `price_timestamp_unparseable`.
- Determinism scope must be explicit: **row ordering is deterministic**; nested lists (`open_trade_ids`, `recent_trade_ids`) will also be made deterministic by ordering inputs and sorting the emitted lists.
- SQL ordering under `LIMIT` is correctness-critical; SQL ordering must be consistent with the normalization rules (`NULL/empty/invalid` last).
- Frontend must not use index keys; React keys must match identity (`token_id`).

## Identity contract

- **Row identity:** `token_id` (globally unique in this endpoint; join key to `price_cache.coin_id`).
- **Uniqueness invariant (CRITICAL):** `rows[*].token_id` must be unique across the response.
- **Frontend stable key:** React key must be `token_id` (no index-based keys).

## Timestamp contract

- `opened_at` and `price_updated_at` are **either ISO8601 strings or null**.
- **Backend rule:** if the stored value is not parseable as ISO8601, emit `null` and append a risk reason (`*_unparseable`) rather than emitting an unparseable string.
- The contract validator treats non-ISO timestamp strings as **CRITICAL**.

## Determinism contract (total order)

Rows are sorted by this total order:

1. `verdict_rank asc` where:
   - `candidate_review` = 0
   - `watch` = 1
   - `blocked` = 2
   - `data_insufficient` = 3
2. `opened_at desc` where `opened_at=null` sorts **last**
3. `token_id asc` (final stable tie-break)

### Backend implementation

Within `dashboard/db.py:get_live_candidates`:

- Open-cohort scan query:
  - `ORDER BY datetime(opened_at) IS NULL ASC, datetime(opened_at) DESC, id DESC`
- Recent-context scan query:
  - `ORDER BY opened_at DESC, id DESC` after filtering by an ISO cutoff; emitted id lists are sorted deterministically.
- Final in-memory results ordering:
  - Sort by `(verdict_rank, opened_at_is_null, -opened_at_epoch, token_id)`.

Note: the open scan is bounded by `LIMIT max(limit * 20, 400)`, so the SQL order must parse timestamps before applying the bound.

## Determinism for nested lists

- `open_trade_ids` is sorted deterministically (descending `id`).
- `recent_trade_ids` is sorted deterministically (descending `id`).

## Contract validator changes

In `scripts/check_live_candidates_contract.py`:

- Add CRITICAL checks:
  - Duplicate `token_id` detection.
  - Ordering check: rows must be sorted by the pinned total-order key, using the same timestamp normalization as backend.

## Tests

### Validator unit tests

In `tests/test_check_live_candidates_contract.py`:

- Duplicate token ids -> CRITICAL.
- Unsorted rows (swap two) -> CRITICAL.

### Endpoint integration tests

In `tests/test_live_candidates_endpoint.py`:

- Tie-regime determinism:
  - Create two open trades with the same `opened_at` and same verdict_rank.
  - Assert the response order is deterministic via `token_id` tie-break.
- Timestamp coercion regression:
  - Insert a trade with `opened_at='not-iso'` and a price_cache row with `updated_at='not-iso'`.
  - Assert response emits `opened_at=null` / `price_updated_at=null` and includes `opened_at_unparseable` / `price_timestamp_unparseable`.
- Bounded SQL scan regression:
  - Insert more than the scan cap of dirty timestamp rows plus one valid candidate.
  - Assert valid parseable timestamps are not pushed out of the bounded scan by lexically high dirty strings.
- Contract alignment:
  - Run `validate_payload()` on the ASGI response payload and assert clean.
  - Exclude `meta.generated_at` from any byte-identical comparisons (it is expected to change).

## Verification

- Focused: `uv run pytest -q tests/test_check_live_candidates_contract.py tests/test_live_candidates_endpoint.py`
- Frontend: if source changes, refresh `dashboard/frontend/dist/` via `npm run build:codex`.

## Rollback

Revert commits that touch:

- `dashboard/db.py`
- `scripts/check_live_candidates_contract.py`
- `dashboard/frontend/components/NowTradableTab.jsx`
- Tests and optional `dashboard/frontend/dist/`

No DB migrations; rollback is source-only.
