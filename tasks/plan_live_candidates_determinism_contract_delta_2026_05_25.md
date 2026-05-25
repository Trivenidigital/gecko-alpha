# Plan — Live candidates determinism + contract delta (BL-NEW-LIVE-DECISION-COCKPIT) — 2026-05-25

## Goal

Ship a small V1 delta that makes `/api/live_candidates` deterministic (backend ordering + frontend keys) and extends the existing contract firewall (`scripts/check_live_candidates_contract.py`) to catch ordering/uniqueness drift before prod.

Non-goals: no new verdict types, no trading/execution affordances, no score changes, no source/KOL ranking, no paid/vendor calls.

## New primitives introduced

- None (delta-only changes to existing endpoint/UI/validator/tests).

## Drift-check (§7a per AGENTS.md)

Shipped V1 primitives exist, but determinism + contract coverage has residual gaps:

- Backend query ordering lacks a stable tie-breaker under `opened_at` ties:
  - `dashboard/db.py:get_live_candidates` uses `ORDER BY opened_at DESC` for the open-cohort scan.
  - `dashboard/db.py:get_live_candidates` uses `ORDER BY opened_at DESC` for the recent-context scan.
- Backend row ordering has no total-order tie-breaker:
  - `dashboard/db.py:get_live_candidates` sorts by `(verdict_rank, opened_at desc)` only; rows with the same verdict + same opened_at can reorder run-to-run.
- Frontend uses unstable React keys:
  - `dashboard/frontend/components/NowTradableTab.jsx` renders table rows with `key={idx}`.
- Contract validator does not enforce determinism or uniqueness:
  - `scripts/check_live_candidates_contract.py:validate_payload` validates schema + banned language but does not validate stable ordering nor unique `token_id`.

## Plan review folds (2-vector)

Applied critical folds from plan reviewers:

- Identity contract is **token_id** (not `(chain, token_id)`); enforce uniqueness on `token_id` and use stable React keys derived from `token_id`.
- Timestamp contract is **ISO8601-or-null** (never “unparseable strings”). Backend must coerce unparseable `opened_at` / `price_updated_at` to `null` (while keeping risk reasons).
- Deterministic ordering must be a **true total order** with explicit `opened_at=null` ordering semantics; validator must check ordering using the same normalization as backend.

## Hermes-first analysis (§7b per AGENTS.md)

This delta is deterministic ordering + contract validation + UI key stability; it is not a Hermes domain.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Backend deterministic ordering | none found (not a Hermes responsibility) | build from scratch (keep custom) |
| Endpoint contract validator | none found (project-specific frozen contract + safety stance) | keep custom (extend existing validator) |
| React stable-key hygiene | none found (frontend implementation detail) | keep custom |

awesome-hermes-agent ecosystem check: blocked in this sandbox (no web access). This change is Hermes-irrelevant regardless; proceed as KEEP_CUSTOM.

## Plan steps

1. Pin deterministic ordering contract:
   - Sort rows by: `verdict_rank asc`, then `opened_at desc (opened_at=null last)`, then `token_id asc`.
2. Backend determinism:
   - Open-cohort scan: `ORDER BY datetime(opened_at) IS NULL ASC, datetime(opened_at) DESC, id DESC`.
   - Recent-context scan: `ORDER BY opened_at DESC, id DESC`, with emitted id lists sorted by id.
   - Final results sort: implement the pinned total-order tuple above (include `token_id` tie-break).
3. Contract validator hardening:
   - Add CRITICAL checks: unique `token_id` across rows; rows sorted by the pinned total order.
4. Tests:
   - Validator unit tests: duplicate `token_id` is CRITICAL; unsorted rows is CRITICAL.
   - Endpoint integration test: create an opened_at tie regime and assert stable ordering by `token_id`.
   - Endpoint integration test: create more dirty timestamp rows than the scan cap and assert a valid timestamp row is still selected first.
   - Endpoint integration test: run `validate_payload()` against the real `/api/live_candidates` response (so backend + validator stay aligned).
5. Frontend key stability:
   - Replace `key={idx}` with `key={token_id}` (identity is token_id).
6. Verification:
   - `uv run pytest -q tests/test_check_live_candidates_contract.py tests/test_live_candidates_endpoint.py`
   - `npm run build:codex` (refresh `dashboard/frontend/dist/` if required by repo policy).

## Acceptance criteria

- `/api/live_candidates` returns rows with a deterministic total order under opened_at ties.
- `scripts/check_live_candidates_contract.py` fails CRITICAL on duplicate `token_id` and unsorted row payloads.
- Frontend does not use index-based React keys for cockpit rows.
- Focused tests pass.
