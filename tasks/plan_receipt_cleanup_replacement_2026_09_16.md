I have what I need. The plan file could not be created because the Write tool is not available in this session, so the plan is delivered inline below. This is read-only: nothing was edited, run, or committed.

## Replacement supervision plan

**Outcome first.** The PR589 wrapper fails because GNU timeout supervises one process, bash, not the process group. When TERM ends bash at ten seconds, timeout exits 124 and never sends the delayed KILL. Anything that ignored TERM stays alive in the group with no supervisor left. The confirmed CI evidence shows exactly this: the TERM-ignoring producer and the pipe-holding descendant survived, both in sleeping state, while the silent and stderr-flood producers died because they honored TERM. Exit 124 is therefore not cleanup proof. It is positive evidence that the KILL phase never ran.

**New primitives introduced:** one stdlib-only host supervisor script, proposed and not yet written. Two added synthetic falsifier cases and one harness flag for a raw-wrapper negative control. No collector, no reducer, no dependency, no CI secret.

**Smallest residual fix.** Supervise the group, not the child. Keep the exact PR589 argv unchanged as the inner time bound and wrap it in a small Python supervisor that does the following.

- Enable child-subreaper on itself before spawning, then launch the unchanged wrapper argv with a new session so the wrapper PID is the group ID. This mirrors the existing worker in the harness.
- Wait for the wrapper with an outer deadline. On exit for any reason, including early normal exit, sweep the group: KILL, reap with group-scoped waitpid, repeat until no child of ours remains, all bounded.
- Emit one fixed JSON status line with the exit code, elapsed time, sweep count, and a status of OK, OUTER_TIMEOUT, SURVIVORS, or FOREIGN_GROUP. A nonzero sweep count on a normal exit is an anomaly, not a success.
- Handle HUP and TERM to the supervisor itself by running the same sweep before exiting, so an SSH disconnect cannot recreate the orphan class.

The harness helpers already encode the oracle. The `guarded_group`, `assert_empty`, `kill_group` and `reap_group` functions in tests/test_receipt_inventory_timeout.py stay as the independent detector, and the supervisor is what they now judge.

**Alternatives considered and not recommended.**

- Adding a TERM trap inside the bash command so timeout keeps supervising into the KILL phase. One line, but it does not cover a shell that exits early with a detached grandchild alive, and bash exec-optimization behavior would need verification.
- Sending KILL directly at ten seconds with no grace. Closes the tested cases but adds no post-exit verification and leaves the fork-versus-group-signal window open with no retry.
- A transient systemd scope or service with a runtime maximum. Cgroup kill is the only truly atomic primitive, but it creates a transient unit on the host, diverges from the CI runner shape, and is unverified here.
- A shell-only outer loop using a background job and group kill. No subreaper, so zombies and reaping depend on PID 1, and the design explicitly forbade background jobs.

**Process group identity and reuse risks.**

- Identity is the wrapper PID in both CI and production, but by different mechanisms: Python setsid in the harness, timeout's own setpgid in production. Keep the existing assertion that fixture PGIDs equal the wrapper PID.
- A group outlives its leader while any member lives, and the kernel cannot reuse that PID number while the group is non-empty. Reuse becomes possible only once the group is empty.
- Rule for the supervisor: signal the group only immediately after a positive live-child observation from group-scoped waitpid. Never signal after observing no children. This closes the reuse hazard.
- Foreign-group detector: members visible to pgrep but no children of ours means a reused PID from another session. Report it, do not kill.
- Reap before checking, because pgrep counts zombies. Accept negative nine and one-thirty-seven as timeout outcomes, since timeout's group KILL also kills timeout itself.
- Uninterruptible members and the fork race are bounded by the repeat-until-empty loop and reported honestly as SURVIVORS.

**Falsifiers.** All five existing cases keep their assertions unchanged and run against the supervisor: silent producer, TERM-ignoring producer, pipe-holding TERM-ignoring descendant, stderr flood, and the detector negative control. Two additions, neither weakening anything:

- Early-exit orphan: the producer forks a TERM-ignoring child with stdout detached, then exits successfully so the pipeline finishes well before the bound. The raw wrapper leaves that child alive. The supervisor must sweep it and report a nonzero sweep count with a non-OK status.
- Raw-wrapper leak control: run the original wrapper with the sweep disabled against the TERM-ignoring cases and assert the detector still fires, with cleanup afterward. This encodes the delete-the-guard lesson permanently and preserves the failing characterization as a positive assertion rather than an xfail.

**Acceptance gates, in order.**

1. Commit this plan as a gated file under tasks with the primitives marker and Hermes-first section, then two independent parallel plan reviews: logic and test validity, and ops safety and concurrency.
2. Separate design document, then two independent parallel design reviews. Design must settle script placement, the exact status schema, timing constants, and the exec-optimization question.
3. Implementation on this branch, stacked on PR590. Open a new PR that supersedes PR590; keep PR590 as the draft red record until the replacement merges.
4. Linux CI job green on all seven cases with survivor assertions before fallback cleanup, exit and elapsed bounds intact, plus full pytest and the test-count baseline on the exact head.
5. All four reviewer vectors recorded on the final SHA. The current clearance table for PR590 is deliberately empty.
6. Even after green, collection stays blocked until reducer and META adversarial tests pass and a fresh source identity preflight is separately approved. No production commands are part of this plan.

**Hermes-first analysis.**

| Surface | Status | Result |
|---|---|---|
| Hermes skills hub | Checked by prior session on 2026-09-16, not re-checked here | Catalog stayed loading, no skill verified |
| awesome-hermes-agent | Checked by prior session on 2026-09-16, not re-checked here | No orchestration listing replaces host process supervision |
| Deployed VPS Hermes surface | Last snapshot 2026-05-23, not re-checked, requires production access | Twenty-four bundled skill categories, none load-bearing for process control |
| hermes-agent-self-evolution | Not checked in any session on record | Required fold before design approval |
| In-repo primitives | Checked this session | Harness oracle helpers and backup-script killpg pattern exist; no group verification exists anywhere |

Verdict: group supervision is a kernel mechanism a skill cannot substitute. The two unchecked ecosystem items are cheap fetches the plan reviewers should complete rather than assume.

## Exact file evidence

| Claim | Location |
|---|---|
| Two failed cases, PIDs, sleeping state, coreutils version | tasks/review_receipt_timeout_ci_2026_09_16.md:20, 25-35 |
| Mechanism and disposition, no skips or xfails | tasks/review_receipt_timeout_ci_2026_09_16.md:37-41 |
| Wrapper declared failed, criteria unmet | tasks/design_receipt_timeout_ci_2026_09_16.md:5 |
| Harness worker, subreaper, exact argv | tests/test_receipt_inventory_timeout.py:109-123 |
| Assertion before fallback cleanup | tests/test_receipt_inventory_timeout.py:146-159 |
| Oracle helpers to reuse | tests/test_receipt_inventory_timeout.py:24-73 |
| Five current cases | tests/test_receipt_inventory_timeout.py:202-215 |
| Disproved single-group assumption | tasks/design_suppression_receipt_inventory_2026_09_14.md:5, 18-23 |
| Session-escape fold that produced that assumption | tasks/findings_suppression_receipt_inventory_2026_09_14.md:42-43 |
| Dedicated CI job | .github/workflows/test.yml:10-23 |
| Empty clearances on PR590 | .reviewers/590.toml:3-7 |
| Existing killpg pattern without verification | tests/test_backup_create_script.py:448-473 |
| Review barrier and primitives marker rules | CLAUDE.md:50-74, 76-94 |
| Hermes-first required shape | docs/gecko-alpha-alignment.md:172-197 |
| Hermes-first scope includes self-evolution repo | tasks/lessons.md:164-166 |
| Discriminating evidence and delete-the-guard lessons | tasks/lessons.md:383-439 |
| Deployed Hermes surface snapshot | docs/hermes_deployed_surface_2026_05_23.md:47-56 |
| Active todo entry | tasks/todo.md:1-8 |

## What is left

Nothing was written to disk. The next action is to commit this plan as a gated tasks file and dispatch the two parallel plan reviews. Design, implementation, and any production step remain behind their own gates.
