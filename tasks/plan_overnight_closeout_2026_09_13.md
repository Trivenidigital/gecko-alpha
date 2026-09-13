**New primitives introduced:** NONE

# Overnight closeout: evidence boundaries

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Scheduling / orchestration | Skills Hub catalog did not render; existing Hermes scheduling is documented in the ecosystem | Keep existing scheduler; no replacement or installation |
| Repo status / backlog provenance | No applicable replacement verified | Correct the existing local reporter; no new subsystem |

Checked https://hermes-agent.nousresearch.com/docs/skills and
https://github.com/0xNyk/awesome-hermes-agent on 2026-09-13.
Ecosystem verdict: reuse the existing Hermes/Codex roles; a bounded correction
to repository evidence does not justify a new orchestration dependency.

## Goal and drift evidence

Base: origin/master `6c56186e31db24ebf6fe769a798cc9cd65e73015`, freshly fetched.
All six templates, role map, reporter, cockpit and Signal Trust V1 exist.
The reporter's absence-of-runner inference is false for external schedulers.
The July reconciliation supersedes historical cockpit/trust parents to a
successor tracker missing from this checkout. Do not rebuild those parents.

## Plan

- [x] Refresh clean isolated worktree; read instructions, lessons, todo, backlog,
  prior automation memory; independently audit drift and Hermes ecosystem.
- [x] Two parallel plan reviews (evidence/logic and authority/safety), fold findings.
- [x] Write design; two parallel design reviews and fold findings.
- [ ] Update reporter text only: always distinguish candidate files from actual
  scheduler/run evidence, and qualify historical backlog anchors with the
  precedence of reconciliation/forward-tracker instructions. No new parser.
- [ ] Update status runbook and role map; file dated missing-tracker and runtime
  evidence report. Preserve historical backlog entries; add a dated pointer.
- [ ] Regression test both candidate/no-candidate output to prevent claims of
  execution from file presence/absence. Verify reporter tests and node syntax.
- [ ] Commit, open PR; two parallel PR reviews covering logic/concurrency and
  ops-safety/silent-failure; fold all findings and record exact reviewed SHA.
- [ ] Merge only after focused tests, independent terminal reviews and exact-head
  CI pass. Docs/script change requires no production service deployment.
- [ ] Record final commit/PR/tests, blockers, approvals and automation memory.

## Runtime assumptions to verify before any further build

1. Production host/revision and active services: read-only SSH, no restarts.
2. Successor backlog present in production or tracked history: test exact path.
3. External runner: saved automation configuration is distinct from observed
   invocation and historical merge evidence; check Hermes matching job metadata.
4. Dashboard availability: existing read-only GETs, bounded timeout; status and
   freshness are point-in-time, not a signal-ranking or dispatch authorization.

No paid calls, policy/config changes, DB writes, migrations or live actions.
Current production-push instruction authorizes this bounded implementation,
PR and conditional merge; retain its record in the closeout approvals table.
If successor scope cannot be recovered, close that track with exact no-build
blocker rather than inventing requirements. Rollback: revert this PR; no state
restore or service restart needed.

## Scope recovery after design (2026-09-13)

Successor recovered in the main checkout as an untracked, August-3-edited file.
The absence-from-git finding remains; missing requirements is no longer a
blocker. Independent drift audit maps DASH-08 to downgraded post-quarantine,
DASH-11 to existing suppression rollup, and DASH-09 to a residual requiring
runtime stop-gap findings. Keep implementation unchanged; extend findings with
those read-only checks and report remaining work honestly.
