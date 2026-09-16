# Plan: receipt wrapper replacement supervision (final)

**New primitives introduced:** one proposed stdlib-only host supervisor script, added synthetic falsifier cases, one harness flag for the raw-wrapper leak control. Implementation not authorized. No production collector, reducer, dependency or CI secret.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Deployed VPS skills | Read-only 2026-09-16T03:32:34Z at revision 77751890: bounded first 100 installed SKILL.md paths across both Hermes homes show workflow, delegation and debugging skill names. Filenames cannot prove none addresses cleanup; no candidate verified | No verified candidate; not an exhaustive audit |
| Public hub | Fetched 2026-09-16, catalog still loading: https://hermes-agent.nousresearch.com/docs/skills | No skill verified |
| Ecosystem | https://github.com/0xNyk/awesome-hermes-agent lists general orchestration; https://github.com/NousResearch/hermes-agent-self-evolution README describes DSPy/GEPA prompt optimization | Neither is a verified cleanup implementation |
| Test oracle | In-repo helpers in tests/test_receipt_inventory_timeout.py and killpg pattern in tests/test_backup_create_script.py | Reuse as independent detector |

Verdict: bounded, no global absence claim. No verified Hermes candidate supervises a host process group for this wrapper. Custom implementation remains unauthorized until plan and design reviews clear.

**Failure being fixed.** GNU timeout 9.4 waits only on bash. In the failed CI cases, observed timing near ten seconds plus live sleeping survivors in the leaderless group show TERM ended bash and no escalation reached the survivors. Exit 124 alone proves nothing about escalation; the evidence is timing and survivors.

## Design obligations

The plan sets obligations. The design must choose and prove algorithms; none below is settled.

**Supervision scope.** Supervise the group, not the child. Keep the exact PR589 inner argv as the inner bound. A stdlib-only supervisor sets child-subreaper before spawning, launches the wrapper in a new session, publishes the inner PGID to a file immediately, waits under an outer deadline, and on wrapper exit for any reason performs one teardown: signal, reap, verify, within one monotonic shared teardown budget.

**Identity lifetime.** The design must prove the inner PGID identity remains valid from first signal through final reaping, so no signal can reach a reused PID. It must not assume a held leader zombie, which conflicts with an empty pgrep result and may be reaped by group-scoped waitpid. Any anchor mechanism must be demonstrated, not asserted.

**Reaper invariants.** Single reaper: only the supervisor waits on members. SIGCHLD keeps default disposition. waitpid semantics stated exactly: positive return reaps one dead child; zero with WNOHANG means a child exists but is not yet waitable; ECHILD means no children in scope. Reap before checking, since pgrep counts zombies. Whether a members-present-but-no-children observation means a foreign group is a design question to prove, not a fact.

**No group escape.** The pipeline contains no setsid, setpgid, nohup, disown or background job. A member leaving the group is a design defect.

**Output ownership.** The supervisor owns all inner stdout and stderr with bounded capture. Incomplete output on any non-success status is discarded. Exactly one bounded status line is emitted. Inner failure never yields success.

**Status contract.** Exit zero only for verified complete normal output: inner exit zero, no sweep, group verified empty. Distinct nonzero statuses, exact names a design choice: INNER_TIMEOUT, OUTER_TIMEOUT, COMMAND_FAILED, ORPHANS_SWEPT, INTERRUPTED, and cleanup failure. ORPHANS_SWEPT is an anomaly, never success.

**Signal handling.** HUP and TERM handlers set a flag; the main loop runs teardown once under the shared budget. Repeated signals during startup or teardown are recorded, not re-entered. Catchable handling does not guarantee cleanup on SIGKILL, host failure, or every SSH disconnect. The design must state that residual boundary explicitly.

## Harness, falsifiers and gates

**Harness.** The harness launches the supervisor and reads the inner PGID from the published file, confirming it independently against fixture readiness records and ps output for the timeout process. Supervisor Popen.pid is never the group ID. It asserts separately on supervisor exit and inner exit from the status line. Existing checks keep their meanings: inner exit in 124, negative nine or 137 for timeout cases, elapsed within nine to sixteen seconds, assert_empty before fallback cleanup, guarded helpers as fallback only.

**Falsifiers.** Assertion meanings of all five existing cases are preserved. The negative control keeps its independent raw path and does not run through the supervisor. Added cases, count set by design:

- Clean early success: pipeline finishes early, expect exit zero, no sweep, empty group.
- Ordinary nonzero exit: inner command fails normally, expect COMMAND_FAILED, output discarded, empty group.
- Early-exit orphan: TERM-ignoring detached child, expect ORPHANS_SWEPT.
- HUP and TERM mid-run, plus repeated signals during startup and teardown, expect INTERRUPTED and one teardown.
- Outer deadline: inner wrapper forced past the supervisor deadline, expect OUTER_TIMEOUT, empty group.
- Raw-wrapper leak control: harness flag runs the original wrapper without sweep, asserts survivors detected as an explicit positive characterization, then fallback cleanup proves empty. Not xfail, not skip.

**Gates.**

1. Commit plan under tasks with the marker; two independent parallel plan reviews.
2. Separate design proving identity lifetime, reaper invariants, output ownership and status contract; two independent parallel design reviews.
3. Implementation only after authorized review, updating existing draft PR590. No second PR.
4. Linux CI green for every enumerated case with survivor assertions before fallback cleanup; full pytest and test-count baseline on the exact head.
5. All four reviewer vectors recorded on the final SHA; .reviewers/590.toml clearances currently empty by design.
6. Collection stays blocked until reducer and META adversarial tests pass and a separately approved identity preflight runs. No production commands here.

| Claim | Location |
|---|---|
| Failed cases, timing, survivors, disposition | tasks/review_receipt_timeout_ci_2026_09_16.md:20, 25-41 |
| Wrapper declared failed | tasks/design_receipt_timeout_ci_2026_09_16.md:5 |
| Harness worker and argv | tests/test_receipt_inventory_timeout.py:109-123 |
| Assertion before fallback cleanup | tests/test_receipt_inventory_timeout.py:146-159 |
| Oracle helpers | tests/test_receipt_inventory_timeout.py:24-73 |
| Existing cases | tests/test_receipt_inventory_timeout.py:202-215 |
| Disproved group assumption | tasks/design_suppression_receipt_inventory_2026_09_14.md:5, 18-23 |
| CI job | .github/workflows/test.yml:10-23 |
| Empty clearances | .reviewers/590.toml:3-7 |
| Review barrier, marker | CLAUDE.md:50-94 |
| Hermes-first shape and scope | docs/gecko-alpha-alignment.md:172-197; tasks/lessons.md:164-166 |
