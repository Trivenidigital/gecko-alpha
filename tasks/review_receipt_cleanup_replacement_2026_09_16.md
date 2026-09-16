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

Revision5 (38dd5cca): both reviewers confirmed prior scan completeness and
termination accounting folds; ops also accepted deadline partitions, startup
rendezvous and continuous drain/relay. Both still require an explicit W
identity-verified acknowledgment before live-identity faults fire. design_ops
also requires capture_fault to use the timeout deadline class because capture
failure leaves the TERM-ignoring workload running until the inner timeout.
A targeted amendment was requested from the same author; no build clearance.

## Revision7 design approval — 08:54 UTC run closeout

Revision6 candidate335acb94: both reviewers accepted identity acknowledgment
and capture timeout classification; both rejected the new stall_cleanup live
survivor discriminator because the supervisor had already sent SIGKILL.
The same author supplied the surgical post-kill reaping correction. Candidate
0e632d9c0339e3c423c085651edaebb0202f1254 received terminal APPROVE from both
independent reviewers, design_logic (ownership/races) and design_ops
(deadlines/fault validity). All retained design findings are closed.

The authoritative revision7 design comprises the revision5 parent document
plus design_receipt_cleanup_amendment_2026_09_16.md, which explicitly replaces
named paragraphs and rows. Consolidate them when implementing; this approval
is for their combined semantics. The approved replacement plan remains unchanged.
No implementation, cleanup proof, CI, merge or deployment clearance is implied.

This run advances PR590 with design evidence only. Four Markdown files differ
from its starting b1cf85d5; scripts/tests/.github/.reviewers are unchanged.
Whitespace validation passes and the worktree is clean after commits. No new
Linux test execution, production query, paid call, DB/config/account/trading
mutation or runtime re-attestation occurred. Existing Linux failures remain
valid evidence against the old wrapper.

Next engineering gate: the configured author implements the approved design,
retains original Linux leak falsifiers, and verifies every enumerated case.
Then two independent implementation/PR reviews and green final-head CI are
required before merge. Reducer/META and fresh runtime preflight remain later
gates before receipt inventory. No operator action is required to start build.

Draft documentation review of abeab8ef: design_logic approved; design_ops
required marking the older Active Work paragraph in todo as historical because
it still described the now-closed design findings as unresolved. That paragraph
is now explicitly superseded; implementation/Linux gates remain outstanding.
No design, source, test or runtime changes accompanied this evidence fold.

## 09:30 UTC run — partial implementation preserved

Current master b5daecfc refreshed before creating the isolated continuation
branch. PR590 started at3da5852f with timeout/test/clearances failed and frontend
parity passed. Existing plan and combined revision7 design approvals retained;
no new custom scope or runtime re-attestation.

Claude safe-mode session08d22646-ab3e-454d-9f07-28b2e3a8bf4b wrote the supervisor,
fault runner and expanded harness, then ended with HTTP429/session limit,
reported reset13:20 UTC. It did not complete verification, design consolidation
or commits. Codex preserved the three files as WIP candidate9db77514 and pushed
to existing draft PR590. No merge/deploy or production call.

Native Python syntax compilation passes; unittest discovers29 cases, all skip
on Windows. These skips provide no Linux cleanup proof. Initial uv environment
sync failed on PyPI UnknownIssuer; stdlib syntax/discovery used the created
Python executable without resolving project dependencies. git diff --check
passes. Existing .reviewers/590.toml remains empty. Independent ownership/logic
and deadline/falsifier reviews were dispatched in parallel on9db77514.
Linux CI run35081632057 was started for that exact code candidate.

### Candidate9db77514: two independent terminal reviews, NOT CLEARED

Ownership/logic reviewer and ops-safety/silent-failure reviewer both request
changes. No findings were fixed in code after the author limit; the disposition
is OPEN, not waived. Both permit preservation as an explicitly incomplete draft.

1. P1, harness632-636 (both reviewers): raw_path finally signals G after the
   leader and all group members have been reaped and emptiness proved. G is no
   longer reserved; unanchored kill_group can reach a reused foreign group.
   Replace that fallback with ownership-anchored cleanup, never after ECHILD.
2. P2, harness710-711/754-755 (logic): closed_reader releases clean_success
   before closing S's stdout reader. S can report and exit0 first. Close reader
   before releasing the fixture, preserving the intended exit11 discriminator.
3. P2, harness907-910 and560 (ops): W recovery obtains report deadlines but logs
   and emits through blocking print, bypassing its TR+9 bounded relay contract.
4. P2, supervisor343-345 and harness416-418 (ops): expired report budgets return
   without the approved single nonblocking diagnostic write attempt.
5. Design residual (ops): direct-child enumeration232-238 and per-child recovery
   loops310-338 lack deadline checks. This partially follows approved pseudocode;
   reconcile the absolute-bound requirement explicitly before claiming it.

Positive static checks: original five case names and survivor-oracle meanings
are retained; raw controls bypass S; supervised emptiness checks precede fallback;
capture timeout class and identity acknowledgment order match the amendment.
No reviewer executed Linux tests. No .reviewers clearances were written.
Author session is no longer active (CLI process inventory verified). Next:
resume same author after reported13:20 UTC reset, fold findings and CI results,
finish design consolidation, then obtain renewed two-vector review and green
exact-head Linux/full-suite checks. Reducer/META and fresh runtime preflight
remain separate downstream gates; D1-D4 UNKNOWN and D5-D6 untouched.
