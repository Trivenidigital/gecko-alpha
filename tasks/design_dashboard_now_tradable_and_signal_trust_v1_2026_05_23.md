# Design — Dashboard “Now Tradable” + Signal Trust (V1, read-only) — 2026-05-23

**New primitives introduced:**
- `GET /api/signal_trust_registry` (read-only) exporting the V1 trust registry file
- Read-only frontend tabs over `/api/live_candidates` and `/api/signal_trust_registry`
- `npm run build:codex` (sandbox-only build path; production build remains unchanged)

## Goal

Add two visibility-only dashboard surfaces:
1) **Now Tradable (V1)**: a thin UI over the already-shipped `GET /api/live_candidates` endpoint.
2) **Signal Trust (V1)**: a thin UI over the existing registry file `docs/superpowers/registries/signal_trust_registry.v1.json`.

No trading behavior changes, no DB writes, no scoring changes, no ranking/pruning affordances.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Trader-facing “Now Tradable” dashboard UI | none found | build from scratch (thin UI over in-tree endpoint) |
| Signal trust registry visibility surface | none found | build from scratch (thin UI over in-tree registry file) |

Awesome-hermes-agent ecosystem check: none found that provides a drop-in Gecko dashboard panel surface; proceed with minimal custom UI.

## Operator-only gates (explicit)

This change MUST NOT cross:
- paid APIs/vendor calls
- live trades/sizing
- source/KOL pruning/suppression
- auto-enable/disable or threshold changes
- destructive DB writes/migrations
- secrets/external account changes

## Runtime-state verification (operator)

Before trusting either panel on a deployed host:
- `/api/live_candidates` contract passes: `python3 scripts/check_live_candidates_contract.py --base-url http://127.0.0.1:8000`
- Registry validates: `node scripts/validate_signal_trust_registry.mjs --path docs/superpowers/registries/signal_trust_registry.v1.json`
- Dashboard is serving the intended `dashboard/frontend/dist/` build (not a stale dist).

## Backend: `/api/signal_trust_registry` contract

### Success (200)

```json
{
  "meta": {
    "ok": true,
    "generated_at": "2026-05-23T21:00:00Z",
    "registry_path": "docs/superpowers/registries/signal_trust_registry.v1.json",
    "registry_mtime": "2026-05-23T18:55:26Z",
    "experimental": true,
    "visibility_only": true,
    "not_for_pruning": true,
    "not_for_auto_disable": true
  },
  "registry": { "...": "raw registry json document" }
}
```

### Unavailable (503)

```json
{
  "meta": {
    "ok": false,
    "generated_at": "...",
    "registry_path": "docs/superpowers/registries/signal_trust_registry.v1.json",
    "experimental": true,
    "visibility_only": true,
    "not_for_pruning": true,
    "not_for_auto_disable": true
  },
  "error": {
    "code": "registry_missing|registry_invalid",
    "message": "human-readable"
  }
}
```

### Safety / invariants

- Resolve `registry_path` relative to repo root via `Path(__file__).resolve()`; never depend on process CWD.
- Hard-assert invariants **matching** `scripts/validate_signal_trust_registry.mjs` (not just the 4 booleans):
  - top-level booleans: `experimental === true`, `visibility_only === true`, `not_for_pruning === true`, `not_for_auto_disable === true`
  - structural requirements: required top-level keys and entry shape checks as enforced by the validator script
- Do not return a raw file hash by default (avoid unnecessary fingerprinting); expose only relative path + mtime + generated_at.
- Add `Cache-Control: no-store` so intermediaries don’t cache stale truth.

## Frontend: “Now Tradable” tab

### Data source

- `GET /api/live_candidates?limit=<n>&window_hours=<h>`
- Use server-provided `meta.read_only`, `meta.not_trade_advice`, `meta.experimental`, and per-row `disclaimer` in the UI.
- Compute verdict counts client-side from `rows[*].verdict` (do not extend the backend contract).

### UI rendering

- Top banner (always visible):
  - “EXPERIMENTAL — read-only labels; not trading advice; not for execution.”
  - Include key meta booleans plus `generated_at`, and echo request params (`limit`, `window_hours`).
  - Include a small freshness/provenance line derived from the payload (e.g., max/min `price_updated_at` across returned rows; “row stale badge uses server `price_is_stale`”).
- Table:
  - Token (symbol/name), chain, market cap, pct from entry, entry_quality, verdict, inclusion reasons, risk reasons, `price_updated_at` + stale badge.
- Error states:
  - Explicitly show the read-only disclaimers even on error/empty results (avoid “0 rows ⇒ safe” inference).

## Frontend: “Signal Trust” tab

### Data source

- `GET /api/signal_trust_registry`

### UI rendering

- Top banner with invariants badges (visibility-only, not-for-pruning, not-for-auto-disable).
- Registry table:
  - signal_type, maturity_state, `data_quality.warning` (if present), and `next_gate` summary (data-bound threshold text).
- Error states:
  - Show `error.code`/`error.message` and keep the “not-for-pruning / not-for-auto-disable” banner visible.

## Deploy / smoke / rollback (required)

- Smoke:
  - `GET /api/signal_trust_registry` returns 200 (valid registry) and 503 (missing/invalid registry)
  - “Now Tradable” tab renders meta flags + disclaimers even when rows are empty or endpoint errors
- Rollback: `git revert <merge_commit>` and redeploy the previous dashboard build artifact.

## Build tooling (sandbox constraint)

Problem: in the Codex sandbox filesystem model, Vite’s default config bundling can traverse disallowed parent dirs and fail with “Access is denied”.

Solution:
- Keep `npm run build` unchanged for normal environments.
- Add `npm run build:codex` = `vite build --configLoader native` so sandbox builds do not require privilege escalation.

## Tests

- Add backend unit tests for `/api/signal_trust_registry`:
  - 200 with invariants ok and expected envelope keys
  - 503 with `registry_missing`
  - 503 with `registry_invalid` when invariants violated
- Frontend: no unit tests in this change (keep scope small); rely on build + smoke.
