# Design: receipt inventory supervisor (revision 2)

**New primitives introduced:** `scripts/receipt_inventory_supervisor.py`, stdlib-only, Linux-only, plus harness cases and a harness-only raw-wrapper flag in `tests/test_receipt_inventory_timeout.py`. No collector, reducer, dependency or production command. Build waits on two independent design reviews.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Deployed VPS skills | Read-only 2026-09-16T03:32:34Z at revision 77751890: bounded first 100 installed SKILL.md paths across both Hermes homes show workflow, delegation and debugging names; no candidate verified | No verified candidate; not exhaustive |
| Public hub | Fetched 2026-09-16, catalog still loading: https://hermes-agent.nousresearch.com/docs/skills | No skill verified |
| Ecosystem | https://github.com/0xNyk/awesome-hermes-agent general orchestration; https://github.com/NousResearch/hermes-agent-self-evolution README describes DSPy/GEPA prompt optimization | Not a cleanup implementation |
| Test oracle | Helpers in tests/test_receipt_inventory_timeout.py:24-73 | Reuse; fallback re-ordered below |

Verdict unchanged from plan e2790e8d.

## Supervisor

**Invocation.** `python3 -I -S scripts/receipt_inventory_supervisor.py --pgid-file PATH [--wait 13] [--cleanup 2] [--report 0.5] -- ARGV...`. ARGV is the unchanged raw wrapper `timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c PIPELINE`, never inspected. No sweep-disable switch exists.

**One absolute deadline.** `T0` is monotonic at spawn. Partitions: wait ends at `T0+13`, cleanup at `T0+15`, report at `T0+15.5`. Each phase checks the current partition end at the top of every loop iteration before any read, wait, signal, sleep or continue. Overrun of a phase consumes the next; the report phase always keeps at least the time remaining to `T0+15.5`, and if nothing remains the report is attempted once non-blocking. The original four timeout cases therefore complete, measured externally by the harness from supervisor launch to exit, within 9 to 16 seconds.

**Startup, in order.** Install HUP, TERM and INT handlers that only record the first signal name, its phase, and a counter. Set SIGCHLD to SIG_DFL. Call `prctl(PR_SET_CHILD_SUBREAPER, 1)`; failure exits SUPERVISOR_ERROR before spawn. If a signal is already recorded, exit INTERRUPTED without spawning. Create a pipe with the read end `O_NONBLOCK`. Spawn L with `start_new_session=True`, stdout to the pipe write end, stderr and stdin DEVNULL. Close the write end in the supervisor. Enter a `try/finally` whose `finally` runs teardown exactly once and closes the capture read end unconditionally. Publish `L.pid` to PATH by temp-write and rename; any publication failure is recorded as error `publish` and falls into the same `finally`.

**Invariants.** Subreaper before spawn; single reaper, so `Popen.poll`, `wait` and `communicate` are never called and `returncode` is set manually; SIGCHLD default; no escape, meaning PIPELINE has no setsid, setpgid, nohup, disown or background job, so every member of group `L.pid` is a descendant of L and only descendants can join L's session; a PID number is reserved while any process or unreaped zombie holds it as pid, pgid or sid.

**Capture.** Each tick in wait and cleanup reads the pipe until EAGAIN or EOF, up to 65,536 bytes per read call. Retained bytes are capped at 65,536; excess is counted in `dropped` and discarded, and reading continues so no writer ever blocks on a full pipe. Orphans holding the write end never produce EOF; the read end is closed in `finally` regardless. Capture never waits.

**Wait phase.** Tick every 20 ms: check deadline; drain capture; if a signal is recorded, mark INTERRUPTED and leave the phase; `waitpid(L.pid, WNOHANG)`; a positive return reaps L, records `inner_exit` and `inner_elapsed`, and leaves the phase. At `T0+13` with L live, mark OUTER_TIMEOUT and leave.

**Cleanup phase: anchored group loop.** Each iteration, in this order: check `T0+15`, and on overrun leave as CLEANUP_FAILED; drain capture; `r = waitpid(-L.pid, WNOHANG)`. ECHILD leaves the loop clean. `r > 0` reaps one dead child; if `r == L.pid` record inner exit, else increment `reaped`. `r == 0` means an unreaped child of the supervisor with pgid `L.pid` exists; call `killpg(L.pid, SIGKILL)`, increment `kills`, sleep 20 ms. `reaped` and `kills` are reported separately; `reaped` counts bodies, `kills` counts interventions.

**Identity safety.** A signal is sent only immediately after a zero return. That child is unreaped, only the supervisor can reap it, and the supervisor has not, so `L.pid` stays reserved as its pgid through the `killpg` call. By no-escape, every process with that pgid is a descendant of L. After ECHILD no signal is ever sent. The reviewers accepted this proof under the invariants; it is unchanged.

**Completeness.** If ECHILD returned while descendant M remained, M's parent is alive and in the group, and the topmost living ancestor in M's chain is a live child of the supervisor with that pgid, so the wait would have returned zero. Fork-during-kill is caught next iteration when the parent dies and the child re-parents. Uninterruptible members yield repeated zeros until `T0+15`, reported as CLEANUP_FAILED. After ECHILD, anything pgrep lists is not a descendant, so the supervisor performs no pgrep and never signals on that evidence.

**Report phase.** Check `T0+15.5` at entry. If status is CLEANUP_FAILED, list survivors by scanning `/proc/*/stat` for pgid `L.pid`, capped at 20 entries, aborting the scan when the deadline is reached; no `ps` subprocess. Build the JSON line. Serialized escaping cap: if the line exceeds 131,072 bytes, drop `output` to null, set `overflow` true, and if the status would have been OK it becomes OUTPUT_OVERFLOW. `output` is included only for OK and only if `dropped` is zero. Set supervisor stdout `O_NONBLOCK` and write the line in a loop, checking the deadline at the top of each iteration, retrying on EAGAIN with 5 ms sleeps. If the full line plus newline is not written by `T0+15.5`, exit code becomes 11 regardless of status. BrokenPipe is swallowed. Supervisor stderr is never written. Consumer rule: a status line without a terminating newline, or not parseable as JSON with the fixed keys, is a failure, never silent success.

**Statuses and precedence.** Lower number wins.

| Status | Exit | Condition | Precedence |
|---|---|---|---|
| CLEANUP_FAILED | 9 | cleanup partition ended without ECHILD | 1 |
| SUPERVISOR_ERROR | 10 | prctl, publication, read or wait exception; `error` names the step | 2 |
| INTERRUPTED | 8 | signal recorded in any phase, `signal_phase` names it | 3 |
| OUTER_TIMEOUT | 7 | L live at `T0+13` | 4 |
| INNER_TIMEOUT | 6 | inner exit 124 or killed by KILL | 5 |
| ORPHANS_SWEPT | 5 | inner exit 0 and `reaped` or `kills` above zero | 6 |
| COMMAND_FAILED | 4 | inner exit nonzero, not a timeout | 7 |
| OUTPUT_OVERFLOW | 3 | inner exit 0, clean, `dropped` nonzero or serialized cap hit | 8 |
| OK | 0 | inner exit 0, `reaped` 0, `kills` 0, ECHILD, output within caps | 9 |

Exit 11 overrides the exit code when the report is incomplete. Cleanup failure overrides internal error because live survivors are the worse outcome. Fixed keys: `status`, `exit_code`, `inner_exit`, `inner_elapsed`, `total_elapsed`, `reaped`, `kills`, `signals_received`, `signal_phase`, `teardowns`, `dropped`, `overflow`, `error`, `survivors`, `output`.

**Signals.** Handlers do no work. A signal first recorded during cleanup still sets INTERRUPTED unless CLEANUP_FAILED wins; `signals_received` counts every delivery. `teardowns` is always 0 or 1. Not guaranteed: signals before handler installation, SIGKILL, host failure, or SSH disconnect that delivers no HUP. Survivors in those cases re-parent to PID 1 with no sweeper. This design does not close that boundary.

## Harness and gates

**PGID validation.** After readiness records appear, the worker reads `/proc/<pgid>/stat` and asserts pgrp and session fields equal the published pgid, and that each fixture record's pgid matches. `Popen.pid` of the supervisor is never used as a group ID.

**Early-exit orphan fixture.** The producer forks a TERM-ignoring child with stdout DEVNULL, still in the group, writes readiness, then blocks on a `release` file. The worker writes `release` only after PGID validation, so the pipeline cannot finish early. Expect ORPHANS_SWEPT with `reaped` 1.

**Emergency fallback, worker.** The worker is a subreaper. On its deadline it kills the supervisor by that child's pid, waits and reaps it by pid with a 2 s bound. Only after reaping, when the supervisor's children have re-parented to the worker, does it run the anchored loop on the published pgid with a 2 s budget: signal only after a zero return, never after ECHILD. Then `assert_empty` via pgrep as the independent oracle. The unconditional `kill_group` is removed from every path.

**Emergency fallback, parent.** The test-case process sets itself subreaper before launching a worker. On a stalled worker it kills the worker by pid, reaps it with a bound, then runs the same anchored loop on the pgid file if present, then `assert_empty`. No stale PGID is ever signalled without a zero-return anchor.

**Signal synchronization.** Real HUP and TERM mid-run are sent to the supervisor after readiness, which implies handlers were installed because installation precedes spawn. Repeated-handler and startup injection use test-side import of the supervisor module with the tick function patched to invoke the handler at chosen points: before spawn, twice during wait, and twice during cleanup. No production hook is added.

**Cases.** Existing assertion meanings are preserved; the raw negative control stays independent and unchanged.

| Case | Expected | Extra assertions |
|---|---|---|
| silent, stderr_flood, ignore_term, descendant | INNER_TIMEOUT | `inner_exit` in 124, negative nine or 137; external supervisor completion 9 to 16 s; group empty before fallback; counts recorded, not constrained |
| clean_success | OK, exit 0 | `output` equals fixed bytes |
| command_failed | COMMAND_FAILED | `output` null |
| early_exit_orphan | ORPHANS_SWEPT | `reaped` 1 |
| output_flood | OUTPUT_OVERFLOW | producer writes 4 MiB then exits 0; completes within wait partition, `dropped` nonzero |
| stalled_reader | exit 11 | harness pre-fills the supervisor stdout pipe to capacity and never reads; supervisor exits by `T0+15.5`; group empty |
| publish_failure | SUPERVISOR_ERROR, `error` publish | PATH in a read-only directory; group empty |
| hup_mid_run, term_mid_run | INTERRUPTED | `signals_received` 1, `teardowns` 1 |
| injected_startup, injected_repeat | INTERRUPTED | `signal_phase` matches; `teardowns` 1; later signals counted |
| outer_deadline | OUTER_TIMEOUT | `--wait 3`, ignore_term; `inner_exit` negative nine |
| foreign_group_refusal | fallback refuses | parent spawns a setsid process outside the worker tree; anchored loop returns ECHILD, signals nothing; parent kills it by pid |
| raw_wrapper_leak | detector fires | harness `--raw-wrapper`, ignore_term; `assert_empty` raises, anchored fallback, then passes |
| negative_control | unchanged | raw path |

**Unresolved, stated plainly.** Pipe writes above PIPE_BUF are not atomic, so a partial status line is possible under a stalled reader; exit 11 plus the consumer rule covers it, not a proof of atomicity. The `/proc` survivor scan is diagnostic, not an oracle. Python signal delivery can lag during a long C call; caps bound that lag but no exact figure is proven. The raw wrapper's own `-k` escalation timing on the runner is observed, not modelled.

**Gates.** Two independent parallel design reviews before any change under `scripts/` or `tests/`. Implementation updates draft PR590 only. Collection stays blocked behind reducer and META tests and a separately approved identity preflight.
