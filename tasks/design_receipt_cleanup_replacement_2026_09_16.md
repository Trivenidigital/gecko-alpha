# Design: receipt inventory supervisor

**New primitives introduced:** `scripts/receipt_inventory_supervisor.py`, a stdlib-only process-group supervisor, plus harness cases and one harness-only raw-wrapper flag in `tests/test_receipt_inventory_timeout.py`. No collector, no reducer, no dependency, no production command. Build is not authorized until two independent design reviews clear.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Deployed VPS skills | Read-only 2026-09-16T03:32:34Z at revision 77751890: bounded first 100 installed SKILL.md paths across both Hermes homes show workflow, delegation and debugging names; no candidate verified | No verified candidate; not exhaustive |
| Public hub | Fetched 2026-09-16, catalog still loading: https://hermes-agent.nousresearch.com/docs/skills | No skill verified |
| Ecosystem | https://github.com/0xNyk/awesome-hermes-agent general orchestration; https://github.com/NousResearch/hermes-agent-self-evolution README describes DSPy/GEPA prompt optimization | Not a cleanup implementation |
| Test oracle | Helpers in tests/test_receipt_inventory_timeout.py:24-73 | Reuse, with the fallback re-ordered as below |

Verdict unchanged from plan e2790e8d: bounded, no absence claim, custom supervisor pending review.

## Supervisor

**Invocation.** `python3 -I -S scripts/receipt_inventory_supervisor.py --pgid-file PATH [--outer-deadline 16] [--teardown-budget 4] -- ARGV...`. ARGV is the unchanged raw wrapper `timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c PIPELINE`. The supervisor never inspects or rewrites ARGV and has no sweep-disable switch.

**Startup, in order.** Install HUP, TERM and INT handlers that only record the first signal name and increment a counter. Set SIGCHLD to SIG_DFL explicitly. Call `prctl(PR_SET_CHILD_SUBREAPER, 1)`; failure is SUPERVISOR_ERROR before any spawn. If a signal is already recorded, exit INTERRUPTED without spawning. Open an unlinked temporary file for inner stdout. Spawn L with `start_new_session=True`, stdout to that file, stderr to DEVNULL, stdin DEVNULL. Record `spawn_at` monotonic. Write `L.pid` to a temp file beside PATH and rename onto PATH. From setsid, `L.pid` equals the group ID and session ID.

**Invariants relied on, all stated in the module docstring and enforced by tests.**

1. Subreaper set before spawn, so every orphaned descendant of L becomes a child of the supervisor.
2. Single reaper: nothing else in the supervisor calls wait on any pid. `Popen.poll`, `wait` and `communicate` are never called; `returncode` is set manually after reaping.
3. SIGCHLD default, so no kernel autoreap.
4. No escape: PIPELINE contains no setsid, setpgid, nohup, disown or background job. Every process in group `L.pid` is a descendant of L, and any process joining that group must already be in L's session, which only descendants are.
5. A PID number cannot be reused while any process, including an unreaped zombie, holds it as pid, pgid or sid.

**Main wait.** Every 20 ms: if a signal was recorded, go to teardown with INTERRUPTED. Else `waitpid(L.pid, WNOHANG)`. A positive return reaps L: record `inner_exit` via `waitstatus_to_exitcode` and `inner_elapsed`, then go to teardown. If monotonic exceeds `spawn_at + outer_deadline`, go to teardown with OUTER_TIMEOUT and L still live.

**Teardown: the anchored group loop.** Start `budget_end = now + teardown_budget`. Loop:

- `r = waitpid(-L.pid, WNOHANG)`.
- ECHILD: no child of the supervisor remains in the group. Exit the loop as clean.
- `r > 0`: one dead child reaped. If `r == L.pid`, record inner exit now. Otherwise increment `swept`. Continue immediately.
- `r == 0`: an unreaped child of the supervisor exists in the group. Call `killpg(L.pid, SIGKILL)`, increment `signals`, sleep 20 ms, continue.
- If `now >= budget_end` with no ECHILD, exit the loop as CLEANUP_FAILED.

**Identity safety proof.** A signal is sent only immediately after `waitpid(-L.pid, WNOHANG)` returned 0. That return means a child of the supervisor with pgid `L.pid` exists and is unreaped. By invariant 3 and 2, only the supervisor can reap it, and it has not, so by invariant 5 the number `L.pid` stays reserved as that child's pgid until the supervisor's next wait call. Therefore `killpg(L.pid)` during that interval can reach only processes whose pgid is `L.pid`, which by invariant 4 are descendants of L. No held leader zombie is needed, and group-scoped wait may reap L freely, because every iteration re-establishes its own anchor before signaling. After ECHILD no signal is ever sent again.

**Completeness proof.** Suppose ECHILD is returned while some descendant M remains in the group. M is not a child of the supervisor, so its parent Q is alive; Q is in the group by invariant 4. Walk up from Q: every dead ancestor has re-parented the next link to the supervisor by invariant 1, so the topmost living ancestor in the chain is a live child of the supervisor with pgid `L.pid`, and the wait would have returned 0, not ECHILD. Contradiction. So ECHILD proves no descendants remain. A process forked concurrently with a KILL is caught on the next iteration, since its parent dies and it re-parents. Uninterruptible members produce repeated zeros until the budget ends, reported honestly as CLEANUP_FAILED.

**Foreign-group question, resolved.** After ECHILD, any member that `pgrep -g L.pid` still lists is provably not a descendant, so it is a reused number belonging to another session. The supervisor therefore performs no pgrep and never signals after ECHILD; pgrep remains a harness-side oracle only.

**Output ownership.** Inner stdout goes to the unlinked file, never a pipe, so no reader can delay the wait or teardown. Inner stderr is discarded. The file is read only after teardown and only when the provisional status is OK: read at most 65,537 bytes; more than 65,536 yields OUTPUT_OVERFLOW and nothing is retained. On any other status the file is closed unread. Exactly one JSON line is written to supervisor stdout, then flushed; BrokenPipe on that write is swallowed and the exit code still carries the status. Supervisor stderr is never written.

**Status line.** Fixed keys: `status`, `exit_code`, `inner_exit`, `inner_elapsed`, `total_elapsed`, `swept`, `signals`, `signals_received`, `teardowns`, `survivors`, `output`. `teardowns` is always 0 or 1. `survivors` is set only on CLEANUP_FAILED, from `ps -o pid,ppid,pgid,stat,comm -g L.pid` with a one-second run bound, capped at 4,096 bytes; that helper child is in the supervisor's own group and is reaped by pid. `output` is the decoded inner stdout only on OK, else null.

| Status | Exit | Condition | Precedence |
|---|---|---|---|
| SUPERVISOR_ERROR | 10 | unexpected exception or prctl failure | 1 |
| CLEANUP_FAILED | 9 | budget ended without ECHILD | 2 |
| INTERRUPTED | 8 | signal recorded before or during main wait | 3 |
| OUTER_TIMEOUT | 7 | L live at outer deadline | 4 |
| INNER_TIMEOUT | 6 | inner exit 124, or signalled by KILL | 5 |
| ORPHANS_SWEPT | 5 | inner exit 0 and `swept` or `signals` above zero | 6 |
| COMMAND_FAILED | 4 | inner exit nonzero, not a timeout | 7 |
| OUTPUT_OVERFLOW | 3 | inner exit 0, clean group, output too large | 8 |
| OK | 0 | inner exit 0, `swept` 0, `signals` 0, ECHILD, output within cap | 9 |

Lower precedence number wins. Cleanup failure therefore overrides everything except an internal error, and any nonzero inner result never yields exit 0.

**Signal handling.** Handlers do no work. The main wait and teardown loop check the flag; PEP 475 retries interrupted syscalls, so latency is one 20 ms tick. Teardown runs exactly once; later signals only increment `signals_received`. Residual boundary, stated plainly: SIGKILL to the supervisor, host failure, or an SSH disconnect that does not deliver HUP leaves any survivors re-parented to PID 1 with no sweeper. This design does not close that; it only guarantees a sweep whenever the supervisor gets to run its loop.

**Timing.** `outer_deadline` 16 s and `teardown_budget` 4 s by default. Expected inner elapsed in timeout cases stays 10 to 12 s from the unchanged wrapper. Worst case supervisor wall time is about 21 s, inside the harness's 25 s worker deadline.

## Harness and gates

**Fixture protocol.** Fixtures keep the existing readiness records with pid and pgid. The harness's `wait_ready` additionally reads `/proc/<pgid>/stat` and asserts fields five and six both equal the published pgid, proving that pid is a live session and group leader, never `Popen.pid` of the supervisor. Detached orphan means stdout redirected to DEVNULL so the pipeline can finish, while the process stays in the group; its readiness record must show the inner pgid.

**Harness fallback, re-ordered for identity safety.** The worker keeps its subreaper. If the supervisor is still alive after the worker's deadline, the worker kills it by its own child pid, after which the supervisor's adopted children re-parent to the worker. The fallback then runs the same anchored loop as the supervisor with a 2 s budget: signal only after a zero return from `waitpid(-pgid, WNOHANG)`, never after ECHILD. The existing `kill_group` unconditional call is removed from every path. `assert_empty` via pgrep runs last, after the loop, as the independent oracle. If pgrep lists a member after ECHILD the case fails with the ps diagnostic and nothing is signalled; that failure direction is safe.

**Cases.** Existing assertion meanings are preserved. The four timeout cases now launch the supervisor and assert from the status line: `inner_exit` in 124, negative nine or 137; `inner_elapsed` between 9 and 16; `assert_empty` before fallback. The negative control keeps its raw path untouched.

| Case | Path | Expected status | Additional assertions |
|---|---|---|---|
| silent, stderr_flood | supervisor | INNER_TIMEOUT | `swept` 0 |
| ignore_term | supervisor | INNER_TIMEOUT | `swept` at least 1 |
| descendant | supervisor | INNER_TIMEOUT | `swept` at least 2 |
| clean_success | supervisor | OK, exit 0 | `output` equals fixed producer bytes |
| command_failed | supervisor | COMMAND_FAILED | `output` null |
| early_exit_orphan | supervisor | ORPHANS_SWEPT | inner exit 0, `swept` 1 |
| hup_mid_run, term_mid_run | supervisor | INTERRUPTED | three signals sent 50 ms apart after readiness; `signals_received` 3, `teardowns` 1 |
| startup_signal | supervisor | INTERRUPTED, exit 8 | HUP sent immediately after launch; group asserted empty if pgid file exists |
| outer_deadline | supervisor, `--outer-deadline 3`, ignore_term fixture | OUTER_TIMEOUT | `inner_exit` negative nine, group empty |
| raw_wrapper_leak | harness `--raw-wrapper`, ignore_term fixture | detector fires | `assert_empty` raises survivors, then anchored fallback, then `assert_empty` passes |
| negative_control | raw, unchanged | detector fires | unchanged |

**CI.** The existing `receipt-inventory-timeout` job in `.github/workflows/test.yml:10-23` runs unchanged; the file now enumerates more cases. Full pytest and the test-count baseline run on the exact head.

**Unresolved items for reviewers, not hidden.** First, `/proc` and `prctl` make the supervisor Linux-only by design; Windows discovery skips remain non-evidence. Second, the startup_signal case is inherently racy on where the signal lands; it asserts status and exit code, and group emptiness only when the pgid file exists. Third, the residual SIGKILL and host-failure boundary is documented, not closed.

**Gates.** Two independent parallel design reviews, logic and test validity plus ops safety and concurrency, must approve before any file under `scripts/` or `tests/` changes. Implementation then updates draft PR590 only, and collection stays blocked behind reducer and META tests and a separately approved identity preflight.
