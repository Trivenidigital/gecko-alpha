# Findings and no-build decision: receipt inventory integration transport prerequisites, 2026-09-16

## Decision

NO BUILD. Design revision 3 is not approved and is not drafted. The reason is an unproved design, not an absence of safe engineering and not a need for new operator permission. Routine design work continues under standing engineering authority; production execution remains blocked by existing gates.

## Approvals recorded

| Artifact | Record |
|---|---|
| Plan `tasks/plan_receipt_inventory_integration_2026_09_16.md` | Two terminal APPROVE PLAN at 035cd914; authorizes design only |
| Plan amendment 1, revision 2 (`tasks/plan_receipt_integration_amendment_2026_09_16.md`) | Two terminal APPROVE at ccbbab34, **prerequisite-scoping only**; does not claim PREREQ-1 or PREREQ-2 resolved |
| Publication of the amendment | Two approvals at 45c7989f |
| Design PR593 (b3fe776a, revision 15216a1f) | REJECTED by two independent design reviews; unchanged |
| Design assessment (this session, Linux-only slice) | NOT accepted as design; findings below |

No build, staging, collection, cleanup or workflow change occurred. The coordinator committed and pushed documentation to existing draft PR593; the configured author only returned read-only artifacts. No execution primitives were implemented.

## Why the Linux-only slice does not satisfy the prerequisites

1. **PREREQ-1 requires that no descendant survives.** The proposed subreaper plus session-group teardown covers only descendants that stay in session G. A descendant that calls `setsid` leaves G and survives. "No known OpenSSH helper does this under the fixed options" is a heuristic about named helpers, not a proof, and the amendment already withdrew exhaustive helper claims. The proposal therefore does not meet the requirement as written and is not presented as meeting it.
2. **The standing operator SSH output rule is not negotiable at design level.** Output must be redirected to a file and read separately; no pipe-capture exception exists. The proposed driver-owned pipe copy would have changed that constraint silently. It is withdrawn and must not be implemented. PREREQ-2 therefore still lacks a mechanism: a hard, before-the-write bound on local storage for both streams that is compatible with file redirection.
3. **CI proof steps do not bypass branch protection.** A dedicated Linux job can prove synthetic behaviour, but `master` requires the full `test` job and `reviewer-clearances` green at exact head, with `strict` on. The full suite has timed out twice. No workflow change is proposed or authorized now; any later change is its own reviewed diff and does not substitute for full-suite green.

## What remains true and safe

- The amendment's settled bounds stand: ssh-only staging, sentinel rejection at any rc, IN_FLIGHT cleared only atomically with verdict, byte-exact provenance at `expected_head` and `MERGED_SHA`, two-level single-quoted quoting.
- Merged PR592 primitives (`scripts/receipt_inventory_supervisor.py`, reducer, META, `usable_envelope`) are unchanged and remain the only execution primitives in scope.
- Zero-porcelain gate retained: 11 entries at 20:39:46 and 23:42:36 UTC on HEAD 77751890. No cleanup, no `-uno`, no bypass authorized.
- Operator Linux host gate retained: no Linux machine is verified to run the driver; the VPS is not a candidate because it collapses the local/remote separation.

## Next engineering slice: one bounded, separately reviewed plan

Scope, one page, two independent plan reviews before any design:

1. **Linux OS-native containment for PREREQ-1.** Evaluate, then pick one: a transient cgroup v2 scope whose `cgroup.procs` is emptied by kernel-level kill and whose emptiness is the independent oracle; or a PID namespace (`unshare --pid --fork --kill-child`) whose teardown kills every process in the namespace regardless of session. Both make `setsid` irrelevant. The plan must name the oracle, its OS version assumptions, and what it refuses to claim.
2. **Hard file storage caps for PREREQ-2** that preserve redirect-to-file: an OS-enforced per-process file size limit (`RLIMIT_FSIZE` applied to the transport process on both redirected files) or a size-bounded filesystem for `RUNDIR`. The bound must be enforced by the kernel before the write, on stdout and stderr, with no pipe in the path.
3. **Declared primitives.** If either mechanism is machinery, the plan declares it under the `New primitives introduced` marker honestly.
4. **Unchanged gates.** Zero porcelain; operator Linux host; two design reviews; build only after both; two PR reviews; full-suite exact-head green; production only with recorded approval.

## Handoff

No active author or implementation owner remains after this findings record. The next owner starts from the amendment at ccbbab34 and this decision, drafts the bounded containment-and-caps plan above, and dispatches two plan reviewers. This closes the current slice with durable findings; it does not claim the six-hour window exhausted, and it abandons no gate.
## Closeout evidence and disposition

- Refreshed origin/master5281f047 in clean assigned worktree341b; recovered existing draftPR593b3fe776a. Root AGENTS absent from tracked checkout; read C:/projects/gecko-alpha/AGENTS.md, supplied rules, CLAUDE.md, lessons, todo, top historical Final Snapshot, current tracked queue and both automation memories.
- Drift: all six requested templates, role map and read-only status reporter exist; reporter exit0. Cockpit/trust bounded integrations remain recorded shipped; parents not reopened. Historical pool-selection retains bounded negative findings; paid probes/activation/trading gates unchanged.
- Freshly re-read first retained app work-loop task019e522b-4bad-7ae1-aa5c-f50b32739693: completed May23. Current startup guard failure is not evidence the loop never ran.
- Fresh23:42:36UTC read-only production:77751890,11porcelainentries, pipeline/dashboard/hermes-gateway active; worker latest prestart19:22:35 exit21, timeractive. No endpoint smoke/freshness inference, account changes or cleanup. Exact operator action: restore intended VPS worker login, then verify successful startup and completed run; disposition of production artifacts is separate, never inferred as deletion authorization.
- Independent ci_diagnostic: failed run35148554523 attempt2 timed out; bounded firewall118passed57.07s vs precedinggreen35143596029 16.50s on same Ubuntu24.04 image20260907.300.1/CPython3.12.14. Failure cause unproven. Reproduction target actual CImerge94b87a4e; no ready local Linux route. No blind third retry. Documentation push triggered new CI35164239282; at inspection threechecksSUCCESS/fullsuiteIN_PROGRESS. This is not final-head green and does not permit merge even if it passes.
- Publication approvals: plan_structure (structural) and plan_operations (operational) both terminal APPROVE45c7989f; subsequent findings require their own publication review. Approval labels never clear integration design.
- Commits: a5782662 draft, be8a0c21 review findings, ccbbab34 folded amendment,45c7989f approval ledger; this findings document is a later documentation commit. PR https://github.com/Trivenidigital/gecko-alpha/pull/593 remains draft. No merge, deploy, DB/config/policy/vendor/account/trading writes or messages.
- Verification: documentation diff check and clean status verified at45c7989f; no application tests locally because no executable changes. CI is recorded separately. Raw runtime, author and local-status artifacts retained under the overnight automation directory.
- Prompt adjustment recommended: use the tracked queue and both automation memories, check actual active ownership, distinguish plan/publication/design approvals, preserve transport constraints and zero-porcelain, and separate CI/environment/worker-account gates. Hourly schedule still conflicts with requested six-hour blocks; config unchanged.
- Done: reviewed prerequisite-scoping amendment, concrete design rejection findings, fresh state/first-run/drift checks. Blocked engineering: transport/caps/durability design and CI diagnosis; no safe-work-exhaustion claim. Parked: paid samples, activation, trading/sizing/pruning. No-action: rebuilding shipped templates/roles/cockpit parents or repeating closed historical-pool probes. This bounded handoff is not six elapsed hours or global backlog completion.
