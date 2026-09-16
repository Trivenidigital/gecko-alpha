**New primitives introduced:** `scripts/receipt_inventory_supervisor.py` (stdlib-only, Linux-only), `tests/receipt_supervisor_faults.py` (test-only fault runner that imports the supervisor module), a per-case subreaper runner and one shared ancestor-recovery routine inside `tests/test_receipt_inventory_timeout.py`, added harness cases, and a harness-only `--raw-wrapper` flag. No collector, reducer, dependency, CI secret or production command. Build waits on two independent parallel design reviews.

# Design: receipt inventory supervisor (revision 4)

> **Candidate for review, NO BUILD.** Supersedes revision 3 (6eb94b4f, rejected). Resolves the five retained proof gaps in `tasks/review_receipt_cleanup_replacement_2026_09_16.md` by removing every file-based identity dependency from recovery and replacing command-line identity with kernel-anchored parent/session facts. Plan e2790e8d guarantees are preserved unchanged.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Deployed VPS skills | Read-only 2026-09-16T03:32:34Z at revision 77751890: bounded first 100 installed SKILL.md paths across both Hermes homes show workflow, delegation and debugging names; no candidate verified | No verified candidate; not exhaustive |
| Public hub | Fetched 2026-09-16, catalog still loading: https://hermes-agent.nousresearch.com/docs/skills | No skill verified |
| Ecosystem | https://github.com/0xNyk/awesome-hermes-agent general orchestration; https://github.com/NousResearch/hermes-agent-self-evolution README describes DSPy/GEPA prompt optimization | Not a cleanup implementation |
| Test oracle | Helpers in `tests/test_receipt_inventory_timeout.py:24-73` (`guarded_group`, `assert_empty`, `reap_group`, `ready`, `wait_ready`) | Reuse unchanged as the independent detector |

Verdict unchanged from plan e2790e8d: no verified Hermes candidate supervises a host process group for this wrapper.

## 1. What changed from revision 3

1. **Identity for recovery comes from the kernel, not from files.** Any subreaper ancestor derives the inner group id as the session id of its own children that are outside its own session. The PGID file and readiness records remain, but only as cross-checked assertions. This closes gaps 2 and 3.
2. **Command-line identity validation is deleted.** A process is killable by pid only when `/proc/<pid>/stat` reports the caller as its parent. That is a kernel fact valid for live, zombie and just-exited children alike, and it is independent of how the supervisor was invoked. This closes gaps 1 and 5.
3. **One recovery routine, `recover_descendants`, is shared by the worker and the case runner.** The supervisor's own cleanup is the same anchored loop with a single child. There is no separate "parent fallback" with different rules.
4. **The worker's `supervisor.pid` publication is removed.** The case runner does not need it.
5. **The case runner is a new per-case subprocess** so the pytest process stays a non-subreaper with untouched state. pytest's only fallback is to kill the runner by pid and fail loudly.
6. **Recovery fixtures retain a stalled supervisor and a live workload by construction**, and assert a discriminator that natural completion cannot produce. This closes gap 4.
7. **Group escape is now a runtime check** in the recovery routine, not only a documented invariant.

Everything accepted in revision 3 (bounded capture, report failure handling, partitioned deadlines, status precedence, signal recording, killpg licensed by waitpid-zero) is retained with wording tightened where needed.

## 2. Process tree and roles

```
pytest (not a subreaper, never signals a group)
 └─ P  case runner      --case CASE DIR      subreaper; deadline, recovery, output relay
     └─ W worker        --worker CASE DIR    subreaper; spawns S, readiness, identity, assertions
         └─ S supervisor                     subreaper; the primitive under test
             └─ L  timeout -k 2 10 bash ... start_new_session=True; sid == pgid == L.pid == G
                 └─ bash → producer | head | reader (+ descendant child in one case)
```

Spawn rules: pytest spawns only P; P spawns only W; W spawns only S (or the raw wrapper, or the negative-control producer) plus short-lived `pgrep`/`ps` via `subprocess.run`; S spawns only L. Only L is started in a new session. Nothing in the tree calls `setsid`, `setpgid`, `nohup`, `disown`, or a background job. All Python processes in the tree are single-threaded; the only pipes are one stdout pipe per level, so `communicate` never spawns reader threads.

All three of P, W and S set `PR_SET_CHILD_SUBREAPER` before their first spawn. Failure to set it is a harness error (P, W) or `SUPERVISOR_ERROR` (S). SIGCHLD stays `SIG_DFL` everywhere. Nobody ever calls `waitpid(-1, ...)`; reaping is by explicit pid or by `-G` only.

## 3. Ownership and identity proofs

These lemmas are the only licenses under which any process in this design sends a signal.

**L1, pid reservation.** A pid number is not reallocated while any process or unreaped zombie holds it as pid, pgid or sid. Consequently a child's pid is reserved until its parent reaps it, and a session id is reserved while any member remains.

**L2, session uniqueness.** In the subtree under P, exactly one `setsid` occurs per case: L's `start_new_session=True`. Therefore any descendant of P whose session differs from P's session is in L's session, whose id is `L.pid`. With no `setpgid` anywhere, every such descendant has `pgid == sid == L.pid`. Define **G** as that session id. A process outside our tree cannot join G because joining a group requires being in its session, and only L's descendants are.

**L3, killpg license.** In a process A that is the reaper of everything below it in G (no living non-G descendant of A has G-members under it), `waitpid(-G, WNOHANG)` returning 0 or a pid proves A currently has an unreaped child with pgid G. By L1 the number G is reserved at that instant, by L2 every holder is our descendant, and A is single-threaded and has not reaped between the call and the `killpg`. So `killpg(G, SIGKILL)` immediately after a zero return reaches only our processes. Never signal a group after ECHILD.

**L4, kill-by-pid license.** In a single-threaded process A, if `/proc/<X>/stat` reports `ppid == getpid()` then X is A's child, live or zombie. Only A can reap X; A has not reaped since reading, so X's number is reserved and `kill(X, SIGKILL)` reaches only X. Killing a zombie is a no-op. This is the only license for signaling an individual pid and it does not depend on cmdline, comm, or any file. Supervisor identity is therefore never needed for safety; it is not needed for classification either, because every child of A is ours by construction of the spawn rules in section 2.

**L5, ECHILD completeness precondition.** `waitpid(-G)` returning ECHILD in A means A has no child in G. That equals "G is empty" only if no living descendant of A outside G can still hold G-members as its children. The recovery routine establishes this by killing and reaping every non-G child of A first (licensed by L4). Children are reparented at their parent's exit, before reaping, so once every non-G child of A has exited, all surviving G-members are direct children of A and ECHILD proves G empty. Zombies in G are also A's children after that point, so the independent `pgrep -g G` oracle, which counts zombies, must read empty as well.

**L6, adoption path.** Linux reparents orphans to the nearest living ancestor that has the subreaper flag. S dead → L's descendants go to W if W is alive, else to P. W dead → S goes to P, whether live or zombie. Reparenting happens at exit, not at reap. This is why P's recovery begins by killing W: after that, S and all G-members are P's children.

**L7, publication correctness (the identity proof before publication).** S obtains `L.pid` from fork. S never waits on L before publishing, so by L1 the number is reserved as L's pid at publication time even if L has already exited. `start_new_session=True` executes `setsid()` in the child before `exec`, and `setsid` returns the caller's pid as the new session id, so `L.pid == sid(L) == pgid(L)` from L's first instruction. Every descendant of L inherits that session and group. Therefore the value S publishes is exactly G, with no window in which it could name any other group. Consumers verify it against L2 rather than trusting the file: the harness asserts that the published value equals the session id it derives from readiness records and `/proc`.

## 4. Supervisor S

**Invocation.** `python3 -I -S scripts/receipt_inventory_supervisor.py --pgid-file PATH [--wait 13] [--cleanup 2] [--report 0.5] -- ARGV...`. ARGV is the unchanged raw wrapper `timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c PIPELINE`, never inspected, no sweep-disable switch. `T0` is monotonic at spawn; wait ends `T0+13`, cleanup `T0+15`, report `T0+15.5`. Every loop in every phase checks its partition end at the top of each iteration before any read, wait, signal, sleep or continue. Overrun consumes the next partition; the report gets whatever remains, attempted once non-blocking if nothing remains.

**Module shape (load-bearing for fault injection).** `main(argv)` calls three module-level callables through module globals, never through local bindings: `publish(path, pgid)`, `read_capture(fd, state)`, `tick(phase, state)`. `tick` is the per-iteration sleep and is the only place fault runners hang the supervisor. `if __name__ == "__main__": sys.exit(main(sys.argv[1:]))`.

**Startup.** Install HUP, TERM, INT handlers that only record first signal name, phase and count. `prctl(PR_SET_CHILD_SUBREAPER, 1)` via ctypes; failure exits `SUPERVISOR_ERROR` with `error` `prctl` before any spawn. If a signal is already recorded, exit `INTERRUPTED` with `teardowns` 0, no child, no PATH written. Create the capture pipe with the read end `O_NONBLOCK`. Spawn L with `start_new_session=True`, stdout to the pipe, stderr and stdin `DEVNULL`; close the write end. Enter one `try/finally`; the `finally` calls the guarded teardown exactly once and closes the capture read end unconditionally. Inside the `try`, call `publish(PATH, L.pid)`, which writes to `PATH.tmp` and renames; any exception records `error` `publish` and falls to `finally`. The status line always carries `pgid` regardless of publication success.

**Invariants.** Subreaper before spawn; single reaper, so `Popen.poll`, `wait`, `communicate` are never called; SIGCHLD default; no escape in PIPELINE; L1 through L3 and L7 above.

**Capture.** Per tick, through `read_capture`: at most 4 reads or 65,536 aggregate bytes, whichever first, with a partition deadline check between reads. Retained bytes cap at 65,536; excess counts in `dropped` and is discarded so writers never block. Any exception from `read_capture` disables capture: read end closed, `error` `capture` recorded, `dropped` frozen, lifecycle continues unchanged. Capture never waits.

**Wait phase.** Tick 20 ms: deadline check; capture; if a signal is recorded, mark `INTERRUPTED` and leave; `waitpid(L.pid, WNOHANG)` positive reaps L, records `inner_exit` and `inner_elapsed`, leave; at `T0+13` with L live, mark `OUTER_TIMEOUT`, leave.

**Cleanup phase, anchored group loop.** Each iteration in order: check `T0+15`, on overrun leave as `CLEANUP_FAILED`; capture if enabled; `r = waitpid(-L.pid, WNOHANG)`. ECHILD leaves clean and records `cleanup_proof` true. `r > 0` reaps one child; `r == L.pid` records inner exit, else increments `reaped`. `r == 0` calls `killpg(L.pid, SIGKILL)` under L3, increments `kills`, then `tick`. ESRCH from `killpg` continues. Any other exception records `error` `cleanup`, stops the loop, and sets `CLEANUP_FAILED`. Teardown has its own `try/except` and is never re-entered. Missing `cleanup_proof` for any reason yields `CLEANUP_FAILED`. Fork-during-kill is caught on the next iteration; uninterruptible members yield `CLEANUP_FAILED`.

**Report phase.** Check `T0+15.5` at entry. On `CLEANUP_FAILED`, scan `/proc/*/stat` for session G, cap 20 entries, abort at deadline, no subprocess; this is diagnostic only. Serialize; if the line exceeds 131,072 bytes, drop `output`, set `overflow`, and OK becomes `OUTPUT_OVERFLOW`. `output` only on OK with `dropped` 0. Set stdout `O_NONBLOCK`; write loop with deadline check each iteration, EAGAIN retried after 5 ms. EPIPE, any OSError, or an incomplete line at deadline sets exit code 11 regardless of status. Stderr is never written. Consumer rule: a line lacking a terminating newline or failing fixed-key JSON parsing is a failure.

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

Exit 11 overrides on an incomplete report. Fixed keys: `status`, `exit_code`, `pgid`, `inner_exit`, `inner_elapsed`, `total_elapsed`, `reaped`, `kills`, `signals_received`, `signal_phase`, `teardowns`, `cleanup_proof`, `dropped`, `overflow`, `error`, `survivors`, `output`.

**Signals.** Handlers do no work. Signals during cleanup still set `INTERRUPTED` unless `CLEANUP_FAILED`. `teardowns` is 0 only when no spawn occurred, else 1. Not guaranteed: signals before handler installation, SIGKILL of S, host failure, SSH disconnect without HUP. Recovery from SIGKILL of S is the harness ancestor's job in tests and the operator's job in production; the design does not claim otherwise.

## 5. Shared ancestor recovery routine

`recover_descendants(budget_s, expect_groups)` runs in W or P after that process has already killed and reaped its own direct spawn (S for W, W for P) by Popen pid. It is the only path by which a harness process signals anything. Total budget 6 s: 2 s for the child phase, 2 s for the group phase, 2 s for the oracle.

```
def recover_descendants(budget, expect_groups):
    me, my_sid = os.getpid(), os.getsid(0)
    end = monotonic() + budget
    record = {"groups": {}, "pid_kills": [], "escape": [], "live_before_signal": 0}
    while monotonic() < end:                        # phase 1+2 interleaved
        kids = scan_children(me)                    # /proc/[0-9]*/stat, ppid == me, at most 4096 entries
        foreign = [k for k in kids if k.session != my_sid]
        for k in foreign:
            if k.pgrp != k.session: record["escape"].append(k)      # runtime no-escape check
            record["groups"].setdefault(k.session, {"kills": 0, "reaped": 0, "proof": False})
        for k in kids:
            if k.session == my_sid or k.pgrp != k.session:          # S, W leftovers, escapees
                os.kill(k.pid, SIGKILL)  (ESRCH ignored)            # L4
                record["pid_kills"].append(k.pid)
                reap_by_pid(k.pid, until=end)                       # waitpid(pid, WNOHANG) polling 20 ms
        progress = False
        for G, g in record["groups"].items():
            if g["proof"]: continue
            if not g["kills"] and any(k.state in "SRDT" for k in foreign if k.session == G):
                record["live_before_signal"] += 1                   # discriminator, counted once per group
            try:
                r = os.waitpid(-guarded_group(G), os.WNOHANG)
            except ChildProcessError:
                g["proof"] = True; continue                         # L5: valid only because loop killed non-G kids first
            if r[0] > 0: g["reaped"] += 1
            else: os.killpg(G, SIGKILL) (ESRCH ignored); g["kills"] += 1  # L3
            progress = True
        if not scan_children(me) and all(g["proof"] for g in record["groups"].values()):
            break
        if not progress: time.sleep(0.02)
    for G in record["groups"]: assert_empty(G)      # independent pgrep oracle, existing helper
    return record
```

Correctness notes reviewers should check: the loop repeats the scan because killing S can reparent new G-members to the caller mid-loop (L6), and a non-G child may itself have just forked L. Termination: each iteration either makes progress (a reap or a kill) or sleeps 20 ms; the budget bounds the total. Exit conditions: zero children remaining and proof for every group. Leaving the loop on budget with children remaining is a harness failure named `RECOVERY_BUDGET_EXCEEDED`, never a pass. A second foreign session or any `escape` entry fails the case as `TREE_INVARIANT_VIOLATED` after recovery completes, because those processes are still our descendants and still safe to kill under L3 and L4. The routine never reads the PGID file or readiness records; the caller compares the returned group ids with those files afterwards and fails on disagreement.

**Case runner P.** `--case CASE DIR`: prctl subreaper; spawn W with `stdout=PIPE, stderr=STDOUT`; loop at 20 ms until W exits, the per-case W deadline passes, or `DIR/worker.stalled` exists (test-only accelerator written by stalled-worker fixtures). On normal W exit: relay output, then `scan_children(P)` must be empty, otherwise run `recover_descendants` and fail the case as `LEAK_AFTER_WORKER_EXIT`. On deadline or marker: `kill(W.pid)` and `waitpid(W.pid)` polling within 2 s (W is P's own Popen child), then `recover_descendants(6, expect_groups)`, then relay W's partial output and the recovery record, exit nonzero unless the case is a recovery case whose expected record matches.

**pytest.** `run_case` spawns P with `communicate(timeout=pytest_deadline)`. On timeout it kills P by Popen pid, prints P's partial output and fails the case as `CASE_RUNNER_STALLED_RECOVERY_NOT_PERFORMED`. pytest never signals a group; it may run `pgrep -g` on a published pgid for diagnostics only. The CI job's five-minute limit remains the last backstop.

## 6. Worker W and fixtures

**Deadlines.** Measured from W's spawn of S; P's deadline for W is the W deadline plus 1 s; the pytest deadline is P's plus 8 s.

| Case class | W deadline | pytest deadline |
|---|---|---|
| timeout classes (13 s outer, 15.5 s report) | 18 s | 27 s |
| fast classes (clean, failed, orphan, flood, faults, signals) | 8 s | 17 s |
| recovery classes (marker-triggered) | 8 s | 17 s |

**Identity acquisition, normal path.** After readiness records appear, W reads `DIR/wrapper.pgid` (retry until present, 4 s bound), then asserts `/proc/<G>/stat` has `pid == pgrp == session == G` and `ppid == S.pid`, and that every readiness record's `pgid == G`. S's `Popen.pid` is never a group id. Only then does W write `DIR/release`.

**Release handshake.** Every fixture that can finish quickly (`clean_success`, `command_failed`, `early_exit_orphan`, `output_flood`) blocks after readiness until `DIR/release` exists.

**Fault runner.** `python3 -I -S tests/receipt_supervisor_faults.py --fault NAME --pgid-file DIR/wrapper.pgid -- ARGV`. The runner inserts the repository root into `sys.path` explicitly because `-I` drops the script directory, imports `scripts.receipt_inventory_supervisor` as a module, replaces `publish`, `read_capture` or `tick` on the module, and calls `main`. It derives `DIR` from `--pgid-file`. No production hooks exist; the production script has no fault switch. Because recovery uses L4, the fault-runner invocation shape has no bearing on identity, which is what gap 5 required.

Fault names and behavior, all unconditional once triggered:

| Fault | Behavior |
|---|---|
| `stall_wait` | `tick` waits for `DIR/wrapper.pgid`, writes `DIR/supervisor.stalled`, then sleeps forever |
| `stall_cleanup` | `tick` in cleanup phase sleeps forever after the first killpg |
| `publish_error` | `publish` waits for `producer.json` (4 s), then raises `OSError` without writing |
| `die_before_publish` | `publish` waits for `producer.json`, then `os.kill(os.getpid(), SIGKILL)` without writing |
| `die_after_ready` | `tick` in wait phase, after publication, waits for `producer.json`, then self-SIGKILL |
| `capture_error` | `read_capture` raises on first call |
| `inject_signal:<phase>:<n>` | `tick` raises the real signal in-process at the named phase, n times |

**Worker behaviors for recovery cases.** These are case names, not supervisor faults:

| Worker case | Worker behavior after spawning S | State P must recover |
|---|---|---|
| `parent_live_supervisor` | S with `stall_wait`; identity acquisition; write `worker.stalled`; hang | live S, live workload |
| `parent_zombie_supervisor` | S with `die_after_ready`; poll `/proc/<S>/stat` until state `Z` (4 s); do not reap; write marker; hang | zombie S, workload adopted by W |
| `parent_reaped_supervisor` | S with `die_after_ready`; poll to `Z`; `waitpid(S.pid)`; write marker; hang | no S, workload adopted by W |
| `parent_missing_supervisor` | write marker before spawning anything; hang | W only, no G |

W-level recovery cases (`stalled_supervisor`, `die_before_publish`) use the same routine in W: W triggers on `supervisor.stalled`, on S exit without a status line, or on deadline; W kills S by Popen pid, reaps by pid, then `recover_descendants`.

## 7. Cases

Existing assertion meanings preserved. Existing cases keep their test names. The negative control stays on its raw independent path and never runs through S.

| Case | Expected | Discriminator that reads differently if the mechanism were absent |
|---|---|---|
| silent, stderr_flood, ignore_term, descendant | INNER_TIMEOUT | inner exit 124, -9 or 137; external 9 to 16 s; `assert_empty` before any harness cleanup; `cleanup_proof` true; `kills` recorded (≥1 for ignore_term and descendant) |
| negative_control | unchanged raw path | detector raises `survivors in group`, then explicit cleanup proves empty |
| raw_wrapper_leak_ignore_term, raw_wrapper_leak_descendant | detector fires | harness `--raw-wrapper`; `assert_empty` raises after `reap_group`; then `recover_descendants` in W reports `live_before_signal ≥ 1` and `kills ≥ 1`; then empty. Not xfail, not skip |
| clean_success | OK, exit 0 | `output` equals fixed bytes; `kills` 0; P has zero children after W exit |
| command_failed | COMMAND_FAILED | `output` null; `kills` 0 |
| early_exit_orphan | ORPHANS_SWEPT | TERM-ignoring detached child; `reaped` or `kills` ≥ 1 |
| output_flood | OUTPUT_OVERFLOW | 4 MiB then exit 0; `dropped` nonzero |
| continuous_flood | INNER_TIMEOUT | floods stdout, ignores TERM; external 9 to 16 s; `dropped` nonzero |
| stalled_reader | exit 11 | pre-filled unread stdout pipe; exits by `T0+15.5`; empty |
| closed_reader | exit 11 | read end closed before report; empty |
| publish_error | SUPERVISOR_ERROR, `error` publish | `wrapper.pgid` absent; status `pgid` equals readiness `pgid`; `cleanup_proof` true; `kills ≥ 1` (ignore_term fixture); empty |
| die_before_publish | W recovery | S exits -9 with no status line; `wrapper.pgid` absent; W derives G from kernel; G equals readiness `pgid`; `live_before_signal ≥ 1`; `kills ≥ 1`; empty |
| capture_fault | SUPERVISOR_ERROR, `error` capture | ignore_term fixture; `kills ≥ 1`; `cleanup_proof` true; empty |
| injected_startup | INTERRUPTED, exit 8 | `teardowns` 0; no `wrapper.pgid`; no child; P has zero children |
| injected_cleanup_repeat | INTERRUPTED | `teardowns` 1; `signals_received` 2; empty |
| hup_mid_run, term_mid_run | INTERRUPTED | real signal from W after identity acquisition; `teardowns` 1; empty |
| outer_deadline | OUTER_TIMEOUT | `--wait 3`, ignore_term; inner exit -9; empty |
| stalled_supervisor | W recovery | `stall_wait`; W kills S by pid; `live_before_signal ≥ 1`; `kills ≥ 1`; G equals published and readiness pgid; empty |
| parent_live_supervisor | P recovery | `pid_kills` contains W and S; `live_before_signal ≥ 1`; `kills ≥ 1`; one group; empty |
| parent_zombie_supervisor | P recovery | `pid_kills` contains W and S (S state `Z` at scan, recorded); `kills ≥ 1`; empty |
| parent_reaped_supervisor | P recovery | `pid_kills` contains only W; `kills ≥ 1`; one group; empty |
| parent_missing_supervisor | P recovery | `pid_kills` contains only W; zero groups; no killpg issued |
| foreign_group_refusal | refusal | pytest itself spawns a setsid decoy and writes its pgid into `DIR/wrapper.pgid` before P starts; P and W see no child in that session; no killpg; case fails as `PUBLISHED_PGID_NOT_OWNED`; pytest asserts decoy `poll()` is None, then kills the decoy by its own pid |

Every fixture synchronizes on readiness or marker files written by atomic rename, never on sleeps. Every fault is an unconditional hang or self-SIGKILL. Every expected outcome carries a discriminator, so a fixture that passes without the mechanism under test is itself a defect.

## 8. Gap resolution map

| Retained gap | Resolution |
|---|---|
| 1. Parent recovery skips adopted descendants when supervisor identity is unverifiable | Supervisor identity is no longer required. P kills every non-G child by L4 (live, zombie, or already gone), reaps them, then runs the anchored group loop on kernel-derived G under L3 and L5. Section 5, cases `parent_*` |
| 2. Publication gaps (worker dies before publishing S pid; S dies before publishing G) | `supervisor.pid` is deleted; P discovers S by `ppid`. G is derived from L2 by any subreaper ancestor without a file. Cases `die_before_publish`, `parent_missing_supervisor` |
| 3. Publication-failure fixture published before raising | `publish_error` never writes; the case asserts the file is absent and identity is acquired from the readiness record and status `pgid`. `die_before_publish` covers death before publication |
| 4. Stalled-worker test could pass by natural completion | Recovery cases hold S in `stall_wait` or kill S after readiness, keep the workload live by TERM-ignoring fixtures, and assert `live_before_signal ≥ 1` and `kills ≥ 1`. Zombie and reaped supervisor variants are separate cases |
| 5. Cmdline identity check did not match the fault-runner invocation | Cmdline checks are removed; identity is `ppid` and session facts under L4 and L2. The fault runner's invocation shape is documented but not load-bearing |

## 9. Residual boundary, stated plainly

- Pipe writes above `PIPE_BUF` are not atomic; exit 11 and the consumer rule cover partial lines without proving atomicity.
- The `/proc` survivor scan in S is diagnostic only.
- Python signal latency during long C calls is bounded by caps, not measured.
- A member in uninterruptible sleep survives SIGKILL until it wakes; S reports `CLEANUP_FAILED` and the harness reports `RECOVERY_BUDGET_EXCEEDED`. Neither converts to success.
- If P itself stalls, pytest kills P by pid and fails without recovery; orphans then reparent to the runner's init. This is reported, not hidden, and the CI job timeout is the last backstop.
- L2 and L4 rely on the spawn rules in section 2 being true of our own code. The runtime escape check and the zero-children-after-exit check turn those rules into contracts the tests enforce, but they do not protect against code outside this repository.
- The supervisor in production has no ancestor recovery; SIGKILL of the supervisor or host failure leaves cleanup to the operator.

## 10. Gates, CI and rollback

Two independent parallel design reviews before any change under `scripts/` or `tests/`. Implementation updates draft PR590 only, no second PR. The existing `receipt-inventory-timeout` job in `.github/workflows/test.yml` runs the harness unchanged in shape; every enumerated case must run green on Linux with survivor assertions before any harness cleanup, plus full pytest and the test-count baseline on the exact head. All four reviewer vectors are recorded on the final SHA; `.reviewers/590.toml` stays empty until then. Collection remains blocked behind reducer and META adversarial tests and a separately approved identity preflight. Rollback is reverting the script, fault runner and test file; no deployment.
