# Plan — Dashboard “Now Tradable” + Signal Trust (V1, read-only) — 2026-05-23

**Branch:** `codex/overnight-autonomous-closeout-20260523`

**New primitives introduced:**
- `dashboard/api.py` — `GET /api/signal_trust_registry` (read-only)
- `dashboard/frontend/components/NowTradableTab.jsx` (read-only UI over existing `GET /api/live_candidates`)
- `dashboard/frontend/components/SignalTrustTab.jsx` (read-only UI over new `GET /api/signal_trust_registry`)
- `dashboard/frontend/App.jsx` — add tabs (read-only)
- `dashboard/frontend/package.json` — make `npm run build` use `vite build --configLoader native` (sandbox filesystem constraint)

## Goal

Ship **read-only** trader-facing visibility for:
1) `/api/live_candidates` (already shipped) via a minimal “Now Tradable” panel, and
2) the V1 signal trust registry (`docs/superpowers/registries/signal_trust_registry.v1.json`) via a minimal “Signal Trust” panel.

No scoring changes. No DB writes. No enable/disable actions. No “pruning” affordances.

## Drift-check (does it already exist in-tree?)

- Frontend references to `GET /api/live_candidates`: **none found** (backend endpoint exists only).
- Frontend trust registry / “Signal Trust” panel: **none found** (registry + validator exist only).
- Backend endpoint exporting the trust registry: **none found**.

## Hermes-first analysis (drift → Hermes hub → custom)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Read-only dashboard UI for Gecko “Now Tradable” (`/api/live_candidates`) | none found (no Gecko-specific dashboard surface in Hermes skills hub) | **build from scratch** (thin UI over an in-tree endpoint) |
| Read-only signal trust registry surface | none found (no Gecko “visibility-only/not-for-pruning” registry UI in Hermes skills hub) | **build from scratch** (thin UI over an in-tree registry file) |

Awesome-hermes-agent ecosystem check: none found that provides a drop-in Gecko dashboard surface (verdict: proceed with minimal custom UI).

## Operator-only gates (must remain explicit)

Do not cross without explicit operator approval:
- paid APIs / paid vendor sample calls
- live trades / order execution; sizing / capital allocation
- source/KOL deletion, pruning, suppression
- signal auto-enable/disable or threshold changes affecting live/paper dispatch
- destructive DB writes, data deletion, irreversible migrations
- production secrets / external account state changes

## Runtime-state verification (required before trusting what the UI says)

This work is **read-only**, but the panels can mislead if runtime state is missing or stale. Before treating the panels as truth, the operator must verify on the deployed dashboard host:

1) `/api/live_candidates` returns in <2s and includes `meta` + `rows` envelope (contract checker exists: `scripts/check_live_candidates_contract.py`).
2) The dashboard build being served matches the merged `dashboard/frontend/dist/` contents.
3) The trust registry file exists on disk at `docs/superpowers/registries/signal_trust_registry.v1.json` and validates:
   - `node scripts/validate_signal_trust_registry.mjs --path docs/superpowers/registries/signal_trust_registry.v1.json`

## Implementation plan

1) Backend:
   - Add `GET /api/signal_trust_registry` that reads the registry path resolved relative to the repo root (do NOT depend on process CWD).
   - Define a frozen response envelope: `{ meta: {...}, registry: {...} }`.
     - `meta` must include `generated_at` and `registry_path`.
     - Avoid returning a raw file hash unless it is clearly internal-only (fingerprinting risk); prefer `updated_at` / `mtime` for operator debugging.
   - Hard-assert V1 invariants exactly matching `scripts/validate_signal_trust_registry.mjs`:
     - `experimental=true`, `visibility_only=true`, `not_for_pruning=true`, `not_for_auto_disable=true`
     - Fail closed with explicit structured error and unambiguous status:
       - `503` for missing/invalid registry (operator-visible “unavailable”)
       - `500` only for unexpected server errors.

2) Frontend:
   - Add a “Now Tradable” tab that calls `GET /api/live_candidates` and renders:
     - an explicit **EXPERIMENTAL / read-only** disclaimer
     - top-level counts computed client-side from `rows[*].verdict` (avoid backend contract changes)
     - a table of rows (token, chain, market_cap, entry_quality, verdict, reasons, staleness)
     - use server-provided disclaimer/meta flags when present (avoid copy drift)
   - Add a “Signal Trust” tab that calls `GET /api/signal_trust_registry` and renders:
     - top-level invariants badges (visibility-only / not-for-pruning)
     - a table of entries (signal_type, maturity_state, data_quality warnings, next_gate)
   - Keep `npm run build` unchanged and add a `build:codex` script using `vite build --configLoader native` (sandbox filesystem constraint: config bundling can traverse disallowed parent dirs).

3) Verification:
   - `node scripts/validate_signal_trust_registry.mjs --path ...` (already exists)
   - `npm ci` (already possible in sandbox) + `npm run build` with `--configLoader native`
   - Optional: smoke-run dashboard server locally and fetch both endpoints (best-effort).

## Acceptance

- UI ships as **read-only** panels with explicit “not for pruning / not for auto-disable / not trading advice” disclaimers.
- No changes to existing verdict/scoring logic; only visualization.
- `npm run build:codex` succeeds in the Codex sandbox (no manual flags).
- If the trust registry invariants are violated, the endpoint fails closed (no ambiguous “partially valid” behavior).
