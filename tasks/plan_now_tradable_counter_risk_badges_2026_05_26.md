# Plan — Now Tradable counter-risk badges (BL-NEW-LIVE-DECISION-COCKPIT) — 2026-05-26

## Goal

Make the "Now Tradable (V1)" table safer and faster to scan by surfacing narrative/counter-risk warnings already present in the `/api/live_candidates` payload:

- `counter_risk_score`
- `narrative_fit_score`
- `counter_flags`

This is a **read-only UI-only** delta: no scoring changes, no backend changes, no ranking changes, no writes, no execution, no pruning.

## Non-goals

- Do not change `/api/live_candidates` schema, ordering, bucketing, or verdict logic.
- Do not introduce a new "trader readiness score" yet (separate child backlog item).
- Do not consume TG/X source context for ranking (context-only remains invariant).

## New primitives introduced

- None (UI-only changes to existing cockpit tab + CSS).

## Drift-check (AGENTS.md §7a)

The backend already exports the fields but the cockpit does not render them:

- `dashboard/models.py:109` includes `narrative_fit_score`, `counter_risk_score`, and `counter_flags` in `LiveCandidateResponse`.
- `dashboard/frontend/components/NowTradableTab.jsx` renders only inclusion/risk reasons and does not reference those fields.

Residual gap: the operator sees "candidate_review/watch/blocked" but not the counter-risk context (e.g., `narrative_mismatch`, `dead_project`) inside the same cockpit row.

## Hermes-first analysis (AGENTS.md §7b)

This is frontend-only rendering of already-computed project-local fields; there is no Hermes surface to reuse.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Counter-risk badge rendering in dashboard | none found | KEEP_CUSTOM (UI concern; Hermes not involved) |

awesome-hermes-agent ecosystem check: N/A for UI-only rendering.

## Runtime-state verification (required)

This is UI-only, but we still verify the runtime assumptions that would otherwise fail silently ("badges always blank"):

- Tests: ensure at least one synthetic `/api/live_candidates` row carries `counter_risk_score` and `counter_flags` (fixtures already exercise predictions enrichment).
- Manual smoke (real DB snapshot): confirm at least one row shows CR/flags; if not, treat as a data plumbing gap and stop (do not ship a "blank badge" feature).

## Plan steps

1. Define a minimal display contract in UI:
   - Render a small `CR <counter_risk_score>` badge when present (display-only; does not override verdict).
   - Render `Fit <narrative_fit_score>` badge when present.
   - Render up to 2 `counter_flags` chips with `+N` overflow indicator; full details in a tooltip.
   - Defensive: `counter_flags` may contain `string` or `{flag,severity,detail}` dicts; never stringify whole dicts inline.
   - Unify counter-risk thresholds across the dashboard: `<30` (low/green), `30–60` (mid/amber), `>60` (high/red).
2. Implement UI rendering in `dashboard/frontend/components/NowTradableTab.jsx`:
   - Add a compact badges line inside the existing Reasons cell (avoid widening the table).
   - Be defensive to `counter_flags` being a mix of strings and dicts.
   - Once CR/flags are displayed, filter the redundant reason `counter_risk_present_display_only_v1` from the reasons list to reduce noise.
3. Add small CSS primitives in `dashboard/frontend/style.css`:
   - `.risk-badge`, `.risk-badge.low|mid|high`
   - `.flag-badge`, `.flag-badge.medium|high` (default = neutral)
4. Verification:
   - `npm.cmd --prefix dashboard/frontend run build:codex` and commit updated `dashboard/frontend/dist/` per repo policy.

## Acceptance criteria

- Now Tradable rows surface counter-risk context without requiring cross-tab navigation.
- UI does not throw on mixed `counter_flags` element shapes.
- Counter-risk thresholds are consistent with other dashboard surfaces.
- Vite build succeeds and `dashboard/frontend/dist/` is updated accordingly.

## Rollback

Revert the UI/CSS commit(s) touching:

- `dashboard/frontend/components/NowTradableTab.jsx`
- `dashboard/frontend/style.css`
- `dashboard/frontend/dist/`

