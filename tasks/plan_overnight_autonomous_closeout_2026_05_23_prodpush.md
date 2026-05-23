# Plan — Gecko overnight autonomous closeout (production-push) — 2026-05-23

**Branch:** `codex/overnight-autonomous-closeout-20260523` (clean local clone)

**New primitives introduced:**
- `docs/superpowers/templates/*` + `docs/superpowers/templates/README.md` — reusable Gecko session templates
- `docs/runbooks/gecko-autonomous-operating-model.md` — durable role map + operator gates + runtime truth sources
- `scripts/report_autonomous_status.mjs` — read-only local status report generator (repo + docs + backlog state)
- `docs/runbooks/autonomous-status-report.md` — operator-facing “how to run + interpret” for the status report
- (Optional, read-only, docs-only) `docs/superpowers/registries/signal_trust_registry.v1.json` + `scripts/validate_signal_trust_registry.mjs` + `docs/runbooks/signal-trust-roadmap-v1.md`
- (Deferred by sandbox constraints) Dashboard “Now Tradable” panel for `/api/live_candidates` — requires `npm ci && npm run build` and committing updated `dashboard/frontend/dist/` from a credentialed environment

## Drift-check (in-tree reality)

- No `docs/superpowers/templates/` directory exists; current structure is `docs/superpowers/{plans,reviews,specs}/` plus `docs/superpowers/backlog.md`.
- No runbook exists for “Hermes orchestrator ↔ Codex worker” operating model; `docs/runbooks/` currently contains only `live-trading-deploy.md`.
- No in-tree “overnight autonomous closeout loop” runner/artifact is present (no scheduler config, no persisted run artifact). Prior “autonomous build blocks” exist as docs/history in `tasks/todo.md`.
- `/api/live_candidates` exists and is shipped (see `BL-NEW-LIVE-DECISION-COCKPIT`). Frontend “Now Tradable” panel is not present in `dashboard/frontend`.

## Hermes-first analysis (drift → Hermes hub → custom)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Session templates (plan/design/findings/review) | No Gecko-Alpha-specific template pack found in the public Hermes Skills Hub (`https://hermes-agent.nousresearch.com/docs/skills`). Built-in “codex/claude-code” delegation skills exist but are generic. | **build from scratch** (project-specific format + operator gates) |
| Autonomous-work status report surface | No drop-in “repo closeout status report” skill found in the Skills Hub; Hermes has dashboards/status APIs, but not a Gecko-Alpha repo-specific closeout surface. | **build from scratch** (repo-specific: backlog + tasks + PR state) |
| Agent/role map + operator gates | No skill found that matches Gecko-Alpha’s operator-gated trading boundaries and runtime truth sources. | **build from scratch** (project-specific governance + truth sources) |
| Signal trust registry + scorecards | No skill found that provides a Gecko-Alpha-compatible, read-only “not-for-pruning” signal trust registry surface. | **build from scratch** (truth stays in gecko-alpha DB; Hermes is enrichment-only) |

Awesome-hermes-agent ecosystem check: reviewed the public index (`https://github.com/0xNyk/awesome-hermes-agent`) for a drop-in Gecko-like closeout/status/template surface; none found. Verdict: proceed with minimal custom docs/scripts; keep everything read-only.

## Runtime-state verification requirements (before any scoring/dashboard semantics change)

If any work proposes new scoring/labels/registry fields that depend on runtime state outside source:
- Verify current DB schema + row counts for any referenced tables/columns.
- Verify any endpoint used by a new UI exists on the deployed dashboard build.
- Verify no unauthenticated write endpoints are made more accessible by the change (dashboard contains write routes; new UI must remain read-only).

## Execution checklist (6-hour autonomous block)

- [ ] Write findings doc: autonomous closeout work-loop state + first-run behavior (or “not yet run”) with exact paths searched.
- [ ] Add `docs/superpowers/templates/` pack + `docs/superpowers/templates/README.md` index.
- [ ] Add `docs/runbooks/gecko-autonomous-operating-model.md` (role map + gates + truth sources).
- [ ] Add `scripts/report_autonomous_status.mjs` + `docs/runbooks/autonomous-status-report.md`.
- [ ] Add **docs-only** cockpit UI shipping guidance (no UI build in this sandbox run).
- [ ] (Optional) Signal trust V1: add a **read-only** registry skeleton + validator, with explicit “EXPERIMENTAL / not-for-pruning” warnings.
- [ ] Run focused verification: Node script smoke + validator passes.
- [ ] Write closeout report: shipped vs blocked vs parked; exact next operator actions; include verification evidence.

## Required parallel reviews (per operator directive)

- Plan review by 2 parallel agents (Critical/Important folds applied) before implementation.
- Design review by 2 parallel agents (Critical/Important folds applied) before any non-trivial build.
- PR review by 2 parallel agents before merge (if a PR is opened).
