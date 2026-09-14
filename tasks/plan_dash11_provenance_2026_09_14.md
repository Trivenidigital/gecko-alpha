# DASH-11 provenance readiness audit plan — 2026-09-14

**New primitives introduced:** none; findings only, existing read-only analyzer and SQLite inspection.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Gecko ledger provenance audit | none found in accessible catalog | Reuse repository labeler/analyzer and read-only SQL; no replacement runtime |
| Dashboard cohort health | none verified for Gecko contracts | Record residual contract, no UI implementation in this audit |

Checked https://hermes-agent.nousresearch.com/docs/skills/ on September 14; catalog rendered loading, so discovery is limited.
Awesome ecosystem checked: https://github.com/0xNyk/awesome-hermes-agent . Verdict: orchestration tools cannot reconstruct absent per-label evidence; Hermes remains orchestration/memory.

## Scope

Base e6a55d7a, production d2f0d61e. Prior #571 fixed population-ratio reporting; do not redo it. New question: can the earliest-token matured cohort's stored r7d be audited against retained input observations, and what may DASH-11 honestly display? Missing shared successor tracker is not a blocker. DASH-08 is dated downgraded work. Historical paid/negative probes stay parked.

## Assumptions to verify

- Production pipeline/dashboard units are active; revision and read paths known.
- Ledger schema records enough evidence to attribute horizon source/time (verify, do not assume).
- Earliest-token matured anchors fall within retained historical observations (verify timestamps).
- Labeler reaches historical/cache inputs; evaluate upstream/downstream overrides without asserting observed corruption from code alone.
- Event rate can sustain future observation; elapsed time alone is no promotion gate.

## Sequence

- [x] Refresh master; read memory/rules/lessons/todo/top snapshot; drift and Hermes checks.
- [ ] Two parallel plan reviews; fold issues before design.
- [ ] Write audit design; two parallel design reviews; fold issues.
- [ ] Run bounded pinned SQLite mode=ro/query_only snapshot; aggregate evidence only, no secrets or vendor calls.
- [ ] Write findings with exact source citations, snapshot times, observed/inferred/unproven distinctions and health-only versus cost gates.
- [ ] Verify existing analyzer tests and document checks; commit findings and open PR.
- [ ] Two parallel PR reviews; fold findings. Leave PR for CI if merge prerequisites are incomplete.

## Boundaries

No code, DB/config/service changes, deployment, messages, price backfill or policy recommendation. Output is a findings PR advancing DASH-11 prerequisites, not a claim that safe health UI is operator-blocked. Revert docs commit for rollback.

Plan review complete: dash_drift and audit_safety approved with cohort/tie, snapshot/deadline/index and attribution folds captured in design.
