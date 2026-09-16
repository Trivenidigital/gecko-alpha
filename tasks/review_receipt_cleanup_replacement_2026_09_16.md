# Receipt cleanup replacement: design findings, 2026-09-16

## Outcome

NO BUILD. Advance retained in draft PR590: approved replacement plan and reviewed,
unapproved supervision design. No scripts, tests, CI configuration, clearances,
production data, policy or deployment changed in this run. Original Linux
falsifiers remain deliberately failing; this change grants no merge clearance.

## State and selection

Fetched and refreshed the assigned clean worktree to origin/master b5daecfc.
Read supplied AGENTS (repo file absent), CLAUDE, lessons, todo, top Current Final
Backlog Snapshot and current closeout queue. Fresh GitHub check found PR590 OPEN
DRAFT at479dfbb8, cleanup/test/clearances failed, frontend parity passed.
DASH-08/11 bounded visibility already shipped; historical pool work stays parked.
Receipt cleanup remains a prerequisite to the residual DASH-11 evidence inventory.

Runtime check2026-09-16T03:32:34Z read only revision77751890 and the first100
SKILL.md paths across the two known Hermes homes. This is bounded identity and
skill-name evidence, not service health, receipt coverage, configuration, DB or
price provenance evidence. No receipt inventory or journal collection ran.

The installed safe-mode Claude worker completed the routine plan/design authoring.
Codex performed independent scope/review coordination and evidence synthesis.
Public Hermes hub remained loading; awesome-hermes-agent and self-evolution
checks did not verify a replacement cleanup implementation. Exact bounded verdict
and links are retained in the plan/design; no ecosystem-wide absence is claimed.

## Review ledger

| Candidate | Logic / test validity | Operations / concurrency | Disposition |
|---|---|---|---|
| Plan031a943f | plan_logic: changes required | plan_ops: changes required | Ownership semantics, inner PGID oracle, output/status and interruption tests folded |
| Plane2790e8d | approved | approved | Authorize separate design only |
| Designdc98efa6 | changes required | changes required | Unbounded capture/report, unreachable deadline, exception/fallback and fixture races folded |
| Design038b7688 | changes required | changes required | Continuous drain, parent adoption, publication identity, capture exceptions and report failures folded |
| Design6eb94b4f | NOT APPROVED | NOT APPROVED | Residual recovery/fixture proof gaps below; no implementation |

## Residual proof gaps

1. Parent recovery validates a supervisor command line before continuing. An
   adopted zombie has an empty command line; an already reaped supervisor has
   none. Its workload children can nevertheless be adopted by the case parent.
   Returning without recovery on missing/failed identity skips these children.
   Refuse to signal unverified PIDs, but separately clean ownership-anchored
   children already adopted into the inner group. A live surviving supervisor
   must first be identified, stopped and reaped before group ECHILD is meaningful.
2. A worker can die after spawning its supervisor but before publishing its PID;
   the supervisor can fail after spawning the wrapper but before PGID publication.
   Recovery through these publication gaps has not been established.
3. The proposed publication-failure fixture successfully publishes identity before
   raising. It tests a later error, not failure before publication. Do not cite it
   as proving the latter. Independent identity acquisition must remain possible.
4. A stalled-worker test alone may let the supervisor finish before the parent
   deadline. It must deliberately retain a stalled supervisor and live workload
   to distinguish successful recovery from natural completion. Also exercise an
   exited supervisor leaving descendants while its worker stalls.
5. Recovery's command-line identity check must match the actual import-based
   fault-runner invocation. A hypothetical script path is not identity evidence.

The per-group waitpid-zero ownership argument was accepted under the documented
single-reaper, default-SIGCHLD and no-escape invariants. That does not prove the
parent's different ancestry or identity handoff. Bounded per-tick capture,
report-failure handling and capture-error cleanup closed corresponding review
objections; neither code nor runtime proof exists yet.

## Verification and next gate

Only documentation changed relative to PR590 head479dfbb8. Git diff/check and
independent evidence reviews apply to this documentation increment. No new Linux
execution is claimed; the existing failing Linux evidence remains in
review_receipt_timeout_ci_2026_09_16.md. Empty .reviewers/590.toml remains empty.

Next: simplify/prove the parent recovery and prepublication identity handoff,
replace the non-discriminating fixtures, obtain two design approvals, then build
and run retained Linux falsifiers. Update PR590, obtain two PR reviews, and pass
final-head CI before considering merge. Reducer/META tests and a fresh bounded
runtime preflight remain separate gates before any receipt inventory. No operator
account action, Docker repair, paid call or trading approval is needed for the
next engineering design step. D1-D4 remain UNKNOWN; D5-D6 untouched.

## Draft evidence review closeout

Candidatec18741ae: plan_logic and plan_ops independently approved draft evidence
retention only. Both verified the four-document diff and clean whitespace check;
no factual corrections. Neither grants design, implementation, runtime cleanup,
merge or production approval. This final note records their terminal verdicts;
.reviewers/590.toml stays unchanged and empty.

## 08:30 UTC run — revision 4 review

Refreshed origin/master b5daecfc and verified PR590 remains draft at b1cf85d5,
with timeout/test/clearances failed and frontend parity passed. No fresh
production query or runtime re-attestation. Existing plan approvals retained.
Claude safe-mode author completed session171d929a-1615-437d-abdb-fb6088a30a7f;
revision4 was committed as7baeae95. No implementation was authorized.

Both independent parallel design reviewers returned CHANGES REQUIRED:
- design_logic accepts core single-reaper ownership proof but requires explicit
  scan completeness before emptiness certification and consistent accounting
  for the direct child killed before recovery starts.
- design_ops requires absolute recovery/oracle/report budgets, readiness-gated
  capture fault injection, consistent parent termination records and a reachable
  pre-spawn startup-signal injection point.

These findings were sent to the same author for revision5, together with a
bounded continuous pipe-drain/relay clarification. Revision4 is not build
clearance. scripts/tests/CI and reviewer clearance records remain unchanged.
