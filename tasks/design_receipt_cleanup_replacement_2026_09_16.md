> **NOT APPROVED — NO BUILD.** Both independent reviewers rejected design
> candidate6eb94b4f. Parent fallback can skip adopted descendants when the
> supervisor is dead/absent, and publication-gap recovery remains unproven.
> The text below is the reviewed proposal, not an implementation contract.
> See tasks/review_receipt_cleanup_replacement_2026_09_16.md for disposition.
# Design: receipt inventory supervisor (revision 3)

**New primitives introduced:** `scripts/receipt_inventory_supervisor.py`, stdlib-only, Linux-only, plus harness cases and a harness-only raw-wrapper flag in `tests/test_receipt_inventory_timeout.py`. No collector, reducer, dependency or production command. Build waits on two independent design reviews.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Deployed VPS skills | Read-only 2026-09-16T03:32:34Z at revision 77751890: bounded first 100 installed SKILL.md paths across both Hermes homes show workflow, delegation and debugging names; no candidate verified | No verified candidate; not exhaustive |
| Public hub | Fetched 2026-09-16, catalog still loading: https://hermes-agent.nousresearch.com/docs/skills | No skill verified |
| Ecosystem | https://github.com/0xNyk/awesome-hermes-agent general orchestration; https://github.com/NousResearch/hermes-agent-self-evolution README describes DSPy/GEPA prompt optimization | Not a cleanup implementation |
| Test oracle | Helpers in tests/test_receipt_inventory_timeout.py:24-73 | Reuse; fallbacks re-ordered below |

Verdict unchanged from plan e2790e8d.

## Supervisor

**Invocation and deadline.** `python3 -I -S scripts/receipt_inventory_supervisor.py --pgid-file PATH [--wait 13] [--cleanup 2] [--report 0.5] -- ARGV...`, ARGV being the unchanged raw wrapper `timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c PIPELINE`, never inspected, no sweep-disable switch. `T0` is monotonic at spawn; wait ends `T0+13`, cleanup `T0+15`, report `T0+15.5`. Every loop in every phase checks its partition end at the top of each iteration before any read, wait, signal, sleep or continue. Overrun consumes the next partition; the report gets whatever remains, attempted once non-blocking if nothing remains. The original four timeout cases complete within 9 to 16 s measured externally by the harness from launch to exit.

**Startup.** Install HUP, TERM, INT handlers that only record first signal name, phase and count. SIGCHLD to SIG_DFL. `prctl(PR_SET_CHILD_SUBREAPER, 1)`, failure exits SUPERVISOR_ERROR before spawn. Signal already recorded: exit INTERRUPTED with `teardowns` 0, no child, no PATH written. Create pipe, read end `O_NONBLOCK`. Spawn L with `start_new_session=True`, stdout to pipe, stderr and stdin DEVNULL; close write end. Enter one `try/finally`; the `finally` calls a single guarded teardown function exactly once and closes the capture read end unconditionally. Publication of `L.pid` to PATH by temp-write and rename happens inside the try through one module-level callable `publish`; any exception records `error` publish and falls to `finally`.

**Invariants.** Subreaper before spawn; single reaper, so `Popen.poll`, `wait`, `communicate` are never called; SIGCHLD default; no escape in PIPELINE; a PID number is reserved while any process or unreaped zombie holds it as pid, pgid or sid.

**Capture.** Per tick, through one module-level callable `read_capture`: at most 4 read calls or 65,536 aggregate bytes, whichever first, with a partition deadline check between reads, then return to lifecycle checks. Retained bytes cap at 65,536; excess counts in `dropped` and is discarded so writers never block. Any exception from `read_capture` disables capture: read end closed, `error` capture recorded, `dropped` frozen, lifecycle continues unchanged. Capture never waits.

**Wait phase.** Tick 20 ms: deadline check; capture; signal recorded, mark INTERRUPTED and leave; `waitpid(L.pid, WNOHANG)` positive reaps L, records `inner_exit`, `inner_elapsed`, leave; at `T0+13` with L live, mark OUTER_TIMEOUT, leave.

**Cleanup phase, anchored group loop.** Each iteration in order: check `T0+15`, on overrun leave as CLEANUP_FAILED; capture if enabled; `r = waitpid(-L.pid, WNOHANG)`. ECHILD leaves clean, recording `cleanup_proof` true. `r > 0` reaps one child; `r == L.pid` records inner exit, else increments `reaped`. `r == 0` calls `killpg(L.pid, SIGKILL)`, increments `kills`, sleeps 20 ms. ESRCH from killpg is treated as continue. Any other exception inside the loop records `error` cleanup, stops the loop, and sets CLEANUP_FAILED. The teardown function has its own try/except and is never re-entered; no recursive finally. Missing `cleanup_proof` for any reason yields CLEANUP_FAILED.

**Ownership proof, accepted, unchanged.** Signal only immediately after a zero return: an unreaped child of the supervisor holds pgid `L.pid`, only the supervisor can reap it and has not, so the number stays reserved through `killpg`; by no escape every holder is a descendant of L. ECHILD proves no descendant remains because any remaining member's topmost living ancestor would be a live child with that pgid. Never signal after ECHILD. Fork-during-kill is caught next iteration; uninterruptible members yield CLEANUP_FAILED.

**Report phase.** Check `T0+15.5` at entry. On CLEANUP_FAILED, scan `/proc/*/stat` for the pgid, cap 20 entries, abort at deadline, no subprocess. Serialize; if the line exceeds 131,072 bytes, drop `output`, set `overflow`, and OK becomes OUTPUT_OVERFLOW. `output` only on OK with `dropped` 0. Set stdout `O_NONBLOCK`; write loop with deadline check each iteration, EAGAIN retried after 5 ms. EPIPE, any OSError, or an incomplete line at deadline sets exit code 11 regardless of status. Stderr never written. Consumer rule: a line lacking a terminating newline or failing fixed-key JSON parsing is a failure.

**Statuses, lower precedence wins.**

| Status | Exit | Condition |
|---|---|---|
| CLEANUP_FAILED | 9 | no `cleanup_proof` |
| SUPERVISOR_ERROR | 10 | prctl, publish, capture, read or wait error; `error` names the step |
| INTERRUPTED | 8 | signal in any phase; `signal_phase` set |
| OUTER_TIMEOUT | 7 | L live at `T0+13` |
| INNER_TIMEOUT | 6 | inner exit 124 or KILL |
| ORPHANS_SWEPT | 5 | inner exit 0 and `reaped` or `kills` above 0 |
| COMMAND_FAILED | 4 | inner nonzero, not timeout |
| OUTPUT_OVERFLOW | 3 | inner 0, clean, `dropped` or `overflow` |
| OK | 0 | inner 0, `reaped` 0, `kills` 0, proof, within caps |

Exit 11 overrides on incomplete report. Fixed keys: `status`, `exit_code`, `inner_exit`, `inner_elapsed`, `total_elapsed`, `reaped`, `kills`, `signals_received`, `signal_phase`, `teardowns`, `cleanup_proof`, `dropped`, `overflow`, `error`, `survivors`, `output`.

**Signals.** Handlers do no work. Signals during cleanup still set INTERRUPTED unless CLEANUP_FAILED. `teardowns` is 0 only when no spawn occurred, else 1. Not guaranteed: signals before handler installation, SIGKILL, host failure, SSH disconnect without HUP.

## Harness and gates

**Identity acquisition.** After readiness records appear, the worker reads `/proc/<pgid>/stat` and asserts pgrp and session equal the published pgid and each fixture pgid matches. Supervisor `Popen.pid` is never a group ID.

**Release handshake, every fast fixture.** clean_success, command_failed, early_exit_orphan, output_flood and any fixture that can finish quickly block after readiness on a `release` file the worker writes only after identity acquisition.

**Worker fallback.** Worker is subreaper and pre-publishes the supervisor pid to `supervisor.pid` immediately after spawning it. On deadline: kill supervisor by that child pid, reap by pid within 2 s, then anchored loop on the published pgid with 2 s budget, signal only after zero return, never after ECHILD, then `assert_empty` by pgrep as independent oracle.

**Parent fallback.** The case process is subreaper. On stalled worker: kill worker by pid, reap by pid within 2 s. Then read `supervisor.pid` as P and validate identity: `/proc/P/stat` ppid equals the parent pid, and `/proc/P/cmdline` contains the supervisor script path and this case's unique temp directory path. A process satisfying all three is ours: only the parent's children have that ppid, the parent launched no other process with that cmdline, and if the supervisor died unreaped its zombie reserves P. If P is missing or validation fails, the parent fails the case and signals nothing. Otherwise kill P by pid, reap by pid within 2 s, then anchored loop on the pgid file, then `assert_empty`. Unresolved: if the worker reaped a dead supervisor before stalling and P was reused by an unrelated process that is also a child of the parent with matching cmdline, validation would misfire; the parent launches no such process, so this is excluded by construction, not by kernel guarantee.

**Signal synchronization.** Real HUP and TERM after readiness. Injection uses test-side import of the module with the tick callable patched: before spawn, twice during wait, twice during cleanup. No production hooks.

**Cases.** Existing assertion meanings preserved; raw negative control independent and unchanged.

| Case | Expected | Extra |
|---|---|---|
| silent, stderr_flood, ignore_term, descendant | INNER_TIMEOUT | inner exit 124, negative nine or 137; external 9 to 16 s; empty before fallback; counts recorded, unconstrained |
| clean_success | OK, exit 0 | `output` fixed bytes |
| command_failed | COMMAND_FAILED | `output` null |
| early_exit_orphan | ORPHANS_SWEPT | `reaped` 1 |
| output_flood | OUTPUT_OVERFLOW | 4 MiB then exit 0, `dropped` nonzero |
| continuous_flood | INNER_TIMEOUT | floods stdout, ignores TERM; external 9 to 16 s, `dropped` nonzero |
| stalled_reader | exit 11 | pre-filled unread stdout pipe; exits by `T0+15.5`; empty |
| closed_reader | exit 11 | read end closed before report; empty |
| publish_failure | SUPERVISOR_ERROR publish | patched `publish` performs real publication, waits for the worker's identity-acquired file, then raises OSError; empty |
| capture_fault | SUPERVISOR_ERROR capture | patched `read_capture` raises; ignore_term fixture; `kills` at least 1, `cleanup_proof` true, empty |
| injected_startup | INTERRUPTED exit 8 | `teardowns` 0, no PATH, no child |
| injected_cleanup_repeat | INTERRUPTED | `teardowns` 1, `signals_received` 2 |
| hup_mid_run, term_mid_run | INTERRUPTED | `teardowns` 1 |
| outer_deadline | OUTER_TIMEOUT | `--wait 3`, ignore_term; inner exit negative nine |
| stalled_supervisor | worker fallback | tick patched to hang after publication; worker chain runs; empty |
| stalled_worker | parent fallback | worker patched to hang after publishing `supervisor.pid`; parent chain runs; empty |
| foreign_group_refusal | refusal | parent spawns a setsid process outside the worker tree; anchored loop sees ECHILD, signals nothing; killed by own pid |
| raw_wrapper_leak | detector fires | harness `--raw-wrapper`, ignore_term; `assert_empty` raises, anchored fallback, then passes |
| negative_control | unchanged | raw path |

**Unresolved, stated plainly.** Pipe writes above PIPE_BUF are not atomic; exit 11 and the consumer rule cover partial lines without proving atomicity. The `/proc` survivor scan is diagnostic. Python signal latency during long C calls is bounded by caps, not measured. Parent identity validation rests on the construction noted above.

**Gates.** Two independent parallel design reviews before any change under `scripts/` or `tests/`. Implementation updates draft PR590 only. Collection stays blocked behind reducer and META tests and a separately approved identity preflight.

