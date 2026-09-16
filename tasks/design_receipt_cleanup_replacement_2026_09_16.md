**New primitives introduced:** `scripts/receipt_inventory_supervisor.py` (stdlib-only, Linux-only), `tests/receipt_supervisor_faults.py` (test-only fault runner that imports the supervisor module), a per-case subreaper runner and one shared ancestor-recovery routine inside `tests/test_receipt_inventory_timeout.py`, added harness cases, and a harness-only `--raw-wrapper` flag. No collector, reducer, dependency, CI secret or production command. Build waits on two independent parallel design reviews.

# Design: receipt inventory supervisor (revision 5)

> **Candidate for review, NO BUILD.** Supersedes revision 4 (7baeae95, rejected on six minimal folds). Kernel ownership proofs, existing Linux falsifiers and fail-closed boundaries are unchanged. Plan e2790e8d guarantees are preserved.

## Hermes-first analysis (historical, not re-run)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Deployed VPS skills | Read-only 2026-09-16T03:32:34Z at revision 77751890: bounded first 100 installed SKILL.md paths across both Hermes homes show workflow, delegation and debugging names; no candidate verified | No verified candidate; not exhaustive |
| Public hub | Fetched 2026-09-16, catalog still loading: https://hermes-agent.nousresearch.com/docs/skills | No skill verified |
| Ecosystem | https://github.com/0xNyk/awesome-hermes-agent general orchestration; https://github.com/NousResearch/hermes-agent-self-evolution README describes DSPy/GEPA prompt optimization | Not a cleanup implementation |
| Test oracle | Helpers in `tests/test_receipt_inventory_timeout.py:24-73` (`guarded_group`, `assert_empty`, `reap_group`, `ready`, `wait_ready`) | Reuse; `assert_empty` gains an optional absolute deadline (section 5) |

Verdict unchanged from plan e2790e8d. No new runtime or hub check was performed for this revision.

## 1. What changed

**Revision 4 (retained).** Identity for recovery comes from kernel facts, not files. Command-line identity validation is deleted. One recovery routine is shared by the worker and the case runner. `supervisor.pid` publication is removed. A per-case subreaper runner keeps pytest untouched. Recovery fixtures retain a stalled supervisor and live workload by construction. Group escape is a runtime check.

**Revision 5 folds.**

1. `scan_children` returns an explicit completeness result. Primary enumeration is the caller's direct-children list from `/proc/<me>/task/<me>/children`; the full-`/proc` scan is a bounded fallback. Emptiness is never proved from an incomplete scan; incompleteness is a named failure.
2. Recovery uses absolute partition deadlines from the trigger instant: stop, recover, oracle, report. Oracle subprocess timeouts are the remaining partition time. Outer deadlines are derived from these partitions, not from a shared six-second pool.
3. The recovery record is seeded by the routine itself: stopping the direct child is phase 0 of the routine and is the first `pid_kills` entry, so `parent_*` expectations include W consistently.
4. `capture_fault` gates its injected failure on the producer readiness handshake and writes `fault.fired` first; W has a fault-specific identity flow per fault.
5. The supervisor calls an explicit startup rendezvous `tick("startup")` after handler installation and before any spawn, then checks recorded signals; `injected_startup` is reachable.
6. P and W drain their child's stdout pipe continuously and non-blockingly every tick with per-tick caps, and the final relay is bounded, so pipe backpressure cannot stall any level.

## 2. Process tree and roles

```
pytest (not a subreaper, never signals a group)
 └─ P  case runner      --case CASE DIR      subreaper; deadline, recovery, bounded drain and relay
     └─ W worker        --worker CASE DIR    subreaper; spawns S, readiness, identity, assertions, bounded drain
         └─ S supervisor                     subreaper; the primitive under test
             └─ L  timeout -k 2 10 bash ... start_new_session=True; sid == pgid == L.pid == G
                 └─ bash → producer | head | reader (+ descendant child in one case)
```

Spawn rules: pytest spawns only P; P spawns only W; W spawns only S (or the raw wrapper, or the negative-control producer) plus short-lived `pgrep`/`ps` via `subprocess.run`; S spawns only L. Only L is started in a new session. Nothing in the tree calls `setsid`, `setpgid`, `nohup`, `disown`, or a background job. All Python processes in the tree are single-threaded; each level holds exactly one read pipe from its child, drained in its own loop, so `communicate` is never used with two pipes and no reader threads exist.

All three of P, W and S set `PR_SET_CHILD_SUBREAPER` before their first spawn. Failure to set it is a harness error (P, W) or `SUPERVISOR_ERROR` (S). SIGCHLD stays `SIG_DFL` everywhere. Nobody ever calls `waitpid(-1, ...)`; reaping is by explicit pid or by `-G` only.

## 3. Ownership and identity proofs

These lemmas are the only licenses under which any process in this design sends a signal.

**L1, pid reservation.** A pid number is not reallocated while any process or unreaped zombie holds it as pid, pgid or sid. A child's pid is reserved until its parent reaps it, and a session id is reserved while any member remains.

**L2, session uniqueness.** In the subtree under P, exactly one `setsid` occurs per case: L's `start_new_session=True`. Any descendant of P whose session differs from P's session is in L's session, whose id is `L.pid`. With no `setpgid` anywhere, every such descendant has `pgid == sid == L.pid`. Define **G** as that session id. A process outside our tree cannot join G because joining a group requires being in its session, and only L's descendants are.

**L3, killpg license.** In a process A that is the reaper of everything below it in G, `waitpid(-G, WNOHANG)` returning 0 or a pid proves A currently has an unreaped child with pgid G. By L1 the number G is reserved at that instant, by L2 every holder is our descendant, and A is single-threaded and has not reaped between the call and the `killpg`. So `killpg(G, SIGKILL)` immediately after a zero return reaches only our processes. Never signal a group after ECHILD.

**L4, kill-by-pid license.** In a single-threaded process A, if `/proc/<X>/stat` reports `ppid == getpid()` then X is A's child, live or zombie. Only A can reap X; A has not reaped since reading, so X's number is reserved and `kill(X, SIGKILL)` reaches only X. Killing a zombie is a no-op. A's own `Popen` child qualifies identically before any reap. This is the only license for signaling an individual pid; it does not depend on cmdline, comm, or any file.

**L5, ECHILD completeness precondition.** `waitpid(-G)` returning ECHILD in A means A has no child in G. That equals "G is empty" only if no living descendant of A outside G can still hold G-members. The recovery routine establishes this by killing and reaping every non-G child of A first (L4). Children are reparented at their parent's exit, before reaping, so once every non-G child of A has exited, all surviving G-members are direct children of A and ECHILD proves G empty. Zombies in G are also A's children after that point, so the independent `pgrep -g G` oracle, which counts zombies, must read empty too.

**L5a, complete enumeration.** L5 additionally requires that "every non-G child" was actually enumerated. A direct-children listing is complete by construction of the kernel's per-task children list; a full-`/proc` scan is complete only if no entry failed to read and the deadline was not crossed. A scan that cannot certify completeness cannot license the claim "no non-G child remains" and therefore cannot license ECHILD as emptiness. Section 5 makes this explicit.

**L6, adoption path.** Linux reparents orphans to the nearest living ancestor with the subreaper flag. S dead → L's descendants go to W if alive, else P. W dead → S goes to P, live or zombie. Reparenting happens at exit, not at reap. P's recovery therefore begins by stopping W; afterwards S and all G-members are P's children.

**L7, publication correctness.** S obtains `L.pid` from fork and never waits on L before publishing, so by L1 the number is reserved as L's pid at publication time. `start_new_session=True` executes `setsid()` in the child before `exec`, and `setsid` returns the caller's pid as the session id, so `L.pid == sid(L) == pgid(L)` from L's first instruction and every descendant inherits it. The published value is exactly G. Consumers verify it against L2 rather than trusting the file.

## 4. Supervisor S

**Invocation.** `python3 -I -S scripts/receipt_inventory_supervisor.py --pgid-file PATH [--wait 13] [--cleanup 2] [--report 0.5] -- ARGV...`. ARGV is the unchanged raw wrapper `timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c PIPELINE`, never inspected, no sweep-disable switch. `T0` is monotonic at spawn; wait ends `T0+13`, cleanup `T0+15`, report `T0+15.5`. Every loop in every phase checks its partition end at the top of each iteration before any read, wait, signal, sleep or continue. Overrun consumes the next partition; the report gets whatever remains, attempted once non-blocking if nothing remains.

**Module shape (load-bearing for fault injection).** `main(argv)` calls three module-level callables through module globals, never through local bindings: `publish(path, pgid)`, `read_capture(fd, state)`, `tick(phase, state)`. `tick` is the only place fault runners hang or perturb the supervisor. Production `tick` sleeps 20 ms for phases `wait` and `cleanup` and returns immediately for phase `startup`. `if __name__ == "__main__": sys.exit(main(sys.argv[1:]))`.

**Startup, in this order.** (1) Install HUP, TERM, INT handlers that only record first signal name, phase and count. (2) `prctl(PR_SET_CHILD_SUBREAPER, 1)` via ctypes; failure exits `SUPERVISOR_ERROR` with `error` `prctl` before any spawn. (3) **Startup rendezvous:** call `tick("startup", state)` exactly once. (4) Check recorded signals: if any, exit `INTERRUPTED` with `teardowns` 0, no child, no PATH written. (5) Create the capture pipe with the read end `O_NONBLOCK`. (6) Spawn L with `start_new_session=True`, stdout to the pipe, stderr and stdin `DEVNULL`; close the write end. (7) Enter one `try/finally`; the `finally` calls the guarded teardown exactly once and closes the capture read end unconditionally. Inside the `try`, call `publish(PATH, L.pid)`, which writes `PATH.tmp` and renames; any exception records `error` `publish` and falls to `finally`. The status line always carries `pgid` regardless of publication success.

**Invariants.** Subreaper before spawn; single reaper, so `Popen.poll`, `wait`, `communicate` are never called; SIGCHLD default; no escape in PIPELINE; L1 through L3 and L7.

**Capture.** Per tick, through `read_capture`: at most 4 reads or 65,536 aggregate bytes, whichever first, with a partition deadline check between reads. Retained bytes cap at 65,536; excess counts in `dropped` and is discarded so writers never block. Any exception from `read_capture` disables capture: read end closed, `error` `capture` recorded, `dropped` frozen, lifecycle continues unchanged. Capture never waits.

**Wait phase.** Each iteration: deadline check; capture; if a signal is recorded, mark `INTERRUPTED` and leave; `waitpid(L.pid, WNOHANG)` positive reaps L, records `inner_exit` and `inner_elapsed`, leave; at `T0+13` with L live, mark `OUTER_TIMEOUT`, leave; `tick("wait")`.

**Cleanup phase, anchored group loop.** Each iteration in order: check `T0+15`, on overrun leave as `CLEANUP_FAILED`; capture if enabled; `r = waitpid(-L.pid, WNOHANG)`. ECHILD leaves clean and records `cleanup_proof` true. `r > 0` reaps one child; `r == L.pid` records inner exit, else increments `reaped`. `r == 0` calls `killpg(L.pid, SIGKILL)` under L3, increments `kills`, then `tick("cleanup")`. ESRCH from `killpg` continues. Any other exception records `error` `cleanup`, stops the loop, and sets `CLEANUP_FAILED`. Teardown has its own `try/except` and is never re-entered. Missing `cleanup_proof` for any reason yields `CLEANUP_FAILED`. Fork-during-kill is caught next iteration; uninterruptible members yield `CLEANUP_FAILED`.

**Report phase.** Check `T0+15.5` at entry. On `CLEANUP_FAILED`, scan `/proc/*/stat` for session G, cap 20 entries, abort at deadline, no subprocess; diagnostic only. Serialize; if the line exceeds 131,072 bytes, drop `output`, set `overflow`, and OK becomes `OUTPUT_OVERFLOW`. `output` only on OK with `dropped` 0. Set stdout `O_NONBLOCK`; write loop with deadline check each iteration, EAGAIN retried after 5 ms. EPIPE, any OSError, or an incomplete line at deadline sets exit code 11 regardless of status. Stderr is never written. Consumer rule: a line lacking a terminating newline or failing fixed-key JSON parsing is a failure.

**Statuses, lower precedence wins.**

| Status | Exit | Condition |
|---|---|---|
| CLEANUP_FAILED | 9 | no `cleanup_proof` |
| SUPERVISOR_ERROR | 10 | prctl, publish, capture, read or wait error; `error` names the step |
| INTERRUPTED | 8 | signal in any phase including startup; `signal_phase` set |
| OUTER_TIMEOUT | 7 | L live at `T0+13` |
| INNER_TIMEOUT | 6 | inner exit 124 or KILL |
| ORPHANS_SWEPT | 5 | inner exit 0 and `reaped` or `kills` above 0 |
| COMMAND_FAILED | 4 | inner nonzero, not timeout |
| OUTPUT_OVERFLOW | 3 | inner 0, clean, `dropped` or `overflow` |
| OK | 0 | inner 0, `reaped` 0, `kills` 0, proof, within caps |

Exit 11 overrides on an incomplete report. Fixed keys: `status`, `exit_code`, `pgid`, `inner_exit`, `inner_elapsed`, `total_elapsed`, `reaped`, `kills`, `signals_received`, `signal_phase`, `teardowns`, `cleanup_proof`, `dropped`, `overflow`, `error`, `survivors`, `output`.

**Signals.** Handlers do no work. Signals during cleanup still set `INTERRUPTED` unless `CLEANUP_FAILED`. `teardowns` is 0 only when no spawn occurred, else 1. Not guaranteed: signals before handler installation, SIGKILL of S, host failure, SSH disconnect without HUP.

## 5. Shared ancestor recovery routine

### 5.1 Partitions and deadlines

Every recovery runs against absolute monotonic deadlines measured from its trigger instant `TR`. No phase inherits leftover time from a shared pool, and every subprocess timeout is the remaining time of the current partition.

| Partition | Ends at | Contents |
|---|---|---|
| stop | `TR+2` | phase 0: kill and reap the direct child by pid |
| recover | `TR+6` | phases 1 and 2: enumerate, kill non-G children, anchored group loop |
| oracle | `TR+8` | `assert_empty(G, deadline=TR+8)` per group; `pgrep` and `ps` timeouts equal remaining time; remaining below 0.05 s is `ORACLE_SKIPPED`, a failure |
| report | `TR+9` | final drain and bounded relay of buffered output plus the recovery record |

Overrun consumes the next partition; the report is attempted once non-blocking if nothing remains. Leaving any partition on deadline is a named failure, never a pass.

Outer deadlines derive from these partitions. `TW0` is W's spawn of S; `TP0` is P's spawn of W. W's trigger `TRW` is the earliest of: S exit observed (pipe EOF), `DIR/supervisor.stalled` present, or `TW0 + D_S`. P's trigger `TRP` is the earliest of: W exit, `DIR/worker.stalled` present, or `TP0 + D_W`.

| Class | `D_S` (W waits for S) | W exits by | `D_W = D_S + 9 + 1` | P exits by | pytest deadline `D_W + 9 + 2` |
|---|---|---|---|---|---|
| timeout classes | 17 s | `TW0+26` | 27 s | `TP0+36` | 38 s |
| fast classes | 8 s | `TW0+17` | 18 s | `TP0+27` | 29 s |
| recovery classes | marker or 8 s | `TW0+17` | 18 s | `TP0+27` | 29 s |

Green-path timing is unchanged: S reports by `T0+15.5`, W asserts and exits near `TW0+16.5`, and the existing external 9 to 16 s check is measured by W exactly as today. The worst-case columns apply only on failure paths. Green-path job time is roughly 150 s across all cases; the CI job's five-minute limit stays as the last backstop and would only truncate a run in which several cases fail at their worst-case bound, which unittest `-v` per-case output still attributes.

On the normal path (no trigger), W's `assert_empty` keeps its existing two-second behaviour through `deadline=now+2`; `reap_group`'s internal two-second bound is unchanged and applies only on the raw path.

### 5.2 Enumeration with explicit completeness

```
def scan_children(me, until):
    # returns (kids, complete, method, reason)
    path = f"/proc/{me}/task/{me}/children"
    try:
        listed = open(path).read().split(); method = "proc_children"
    except FileNotFoundError:
        listed = None; method = "proc_scan"          # kernel without CONFIG_PROC_CHILDREN
    if listed is not None:
        kids = []
        for pid in listed:
            st = read_stat(pid)                       # parse after last ')' : state, ppid, pgrp, session
            if st is None:                            # a listed child cannot vanish unless WE reaped it
                return kids, False, method, "CHILD_STAT_UNREADABLE"
            kids.append(st)
        return kids, True, method, None
    for attempt in range(3):                          # bounded fallback
        kids, incomplete = [], False
        for entry in os.listdir("/proc"):
            if monotonic() >= until:
                return kids, False, method, "SCAN_DEADLINE"
            if not entry.isdigit(): continue
            st = read_stat(entry)
            if st is None: incomplete = True; continue   # some process exited mid-scan; ours or foreign is unknowable
            if st.ppid == me: kids.append(st)
        if not incomplete:
            return kids, True, method, None
    return kids, False, method, "SCAN_INCOMPLETE"
```

Why the direct listing is sound: our child set changes only by our own reap (we are single-threaded and do not reap during the read) or by adoption. Adoption requires a descendant's parent to exit; that parent is either listed live or, if it exited before the read reached it, listed as our unreaped zombie. So a complete listing with zero children proves no living descendant exists and no later adoption is possible. The full-`/proc` fallback is complete only if every entry read succeeded and the deadline held; otherwise it is `SCAN_INCOMPLETE` and cannot prove anything. The recovery record carries `scan.method`, `scan.complete` and `scan.reason` so CI output shows which path ran.

### 5.3 Routine

```
def recover_descendants(direct, TR, expect_groups):
    me, my_sid = os.getpid(), os.getsid(0)
    stop_end, recover_end, oracle_end, report_end = TR+2, TR+6, TR+8, TR+9
    record = {"pid_kills": [], "groups": {}, "escape": [], "live_before_signal": 0,
              "scan": {"method": None, "complete": False, "reason": None}, "failures": []}

    # phase 0: stop the direct child (own unreaped Popen child, L4). Seeds the record.
    if direct is not None:
        st = read_stat(direct.pid)
        if st is None: record["failures"].append("DIRECT_CHILD_VANISHED")     # only we could have reaped it
        else:
            os.kill(direct.pid, SIGKILL)  (ESRCH ignored)
            record["pid_kills"].append({"pid": direct.pid, "role": "direct", "state_at_scan": st.state})
            if not reap_by_pid(direct.pid, until=stop_end):                  # waitpid(pid, WNOHANG) each 20 ms
                record["failures"].append("DIRECT_STOP_TIMEOUT")

    # phases 1+2, interleaved until a complete empty scan and proof for every group
    while monotonic() < recover_end:
        kids, complete, method, reason = scan_children(me, recover_end)
        record["scan"] = {"method": method, "complete": complete, "reason": reason}
        foreign = [k for k in kids if k.session != my_sid]
        for k in foreign:
            if k.pgrp != k.session: record["escape"].append(k.pid)            # runtime no-escape check
            record["groups"].setdefault(k.session, {"kills": 0, "reaped": 0, "proof": False})
        for k in kids:
            if k.session == my_sid or k.pgrp != k.session:                    # S, W leftovers, escapees
                os.kill(k.pid, SIGKILL)  (ESRCH ignored)                       # L4
                record["pid_kills"].append({"pid": k.pid, "role": "adopted", "state_at_scan": k.state})
                reap_by_pid(k.pid, until=recover_end)
        progress = False
        for G, g in record["groups"].items():
            if g["proof"]: continue
            if not g["kills"] and any(k.state in "SRDT" for k in foreign if k.session == G):
                record["live_before_signal"] += 1                             # once per group
            try:
                r = os.waitpid(-guarded_group(G), os.WNOHANG)
            except ChildProcessError:
                if complete: g["proof"] = True                                # L5 + L5a
                continue
            if r[0] > 0: g["reaped"] += 1
            else: os.killpg(G, SIGKILL) (ESRCH ignored); g["kills"] += 1      # L3
            progress = True
        if complete and not kids and all(g["proof"] for g in record["groups"].values()):
            break
        if not progress: time.sleep(0.02)
    else:
        record["failures"].append("RECOVERY_BUDGET_EXCEEDED")
    if not record["scan"]["complete"]: record["failures"].append("SCAN_INCOMPLETE")

    # phase 3: independent oracle
    for G in record["groups"]:
        assert_empty(G, deadline=oracle_end)          # existing helper; timeouts = remaining partition time
    if record["escape"] or len(record["groups"]) > max(expect_groups, 1):
        record["failures"].append("TREE_INVARIANT_VIOLATED")
    return record                                     # caller fails the case if failures is non-empty
```

Notes for reviewers. ECHILD marks proof only on a complete scan (L5a); an incomplete scan never proves emptiness. The loop repeats the scan because stopping S can reparent new G-members to the caller mid-loop (L6). Termination: each iteration makes progress or sleeps 20 ms; the recover partition bounds the total. Escapees and extra sessions are still our descendants and still safe to kill under L3 and L4; they fail the case after recovery completes. The routine never reads the PGID file or readiness records; the caller compares returned group ids with those files afterwards and fails on disagreement.

### 5.4 Bounded drain and relay

P and W each hold one `O_NONBLOCK` read pipe from their child. Every 20 ms loop iteration drains it: at most 4 reads or 65,536 bytes per tick. W retains up to 131,073 bytes (the supervisor's maximum line plus newline); more is `STATUS_LINE_OVERSIZE`, a failure under the consumer rule. P retains up to 1 MiB of W's output and counts dropped bytes beyond that. After the child exits, the holder drains to EOF within the current partition (normal path: 1 s; recovery path: the report partition). P's final relay writes retained bytes and the recovery record to its own `O_NONBLOCK` stdout with a deadline of `report_end`, EAGAIN retried after 5 ms; an incomplete relay exits 12 (`RELAY_INCOMPLETE`). pytest reads P through `communicate`, which drains continuously. W's own diagnostic prints are bounded to 64 KiB. Because every level drains every tick, no writer in the tree can block on a full pipe, and a stall observed at any level is a real stall.

### 5.5 Case runner P

`--case CASE DIR`: prctl subreaper; spawn W with `stdout=PIPE, stderr=STDOUT`, read end `O_NONBLOCK`. Loop at 20 ms: drain; `waitpid(W.pid, WNOHANG)`; check `DIR/worker.stalled`; check `TP0 + D_W`. On normal W exit: drain to EOF within 1 s, then `scan_children(P, now+1)` must be complete and empty, otherwise run `recover_descendants(None, now, 0)` and fail the case as `LEAK_AFTER_WORKER_EXIT`. On marker or deadline: `TR = now`; `recover_descendants(W, TR, expect_groups)`; relay; exit nonzero unless the case is a recovery case whose expected record matches.

### 5.6 pytest

`run_case` spawns P with `communicate(timeout=pytest_deadline)`. On timeout it kills P by Popen pid, prints P's partial output and fails the case as `CASE_RUNNER_STALLED_RECOVERY_NOT_PERFORMED`. pytest never signals a group; it may run `pgrep -g` on a published pgid for diagnostics only.

## 6. Worker W and fixtures

**Identity acquisition, normal path.** After readiness records appear, W reads `DIR/wrapper.pgid` (retry until present, 4 s bound), asserts `/proc/<G>/stat` has `pid == pgrp == session == G` and `ppid == S.pid`, and that every readiness record's `pgid == G`. S's `Popen.pid` is never a group id. Only then does W write `DIR/release`.

**Release handshake.** Every fixture that can finish quickly (`clean_success`, `command_failed`, `early_exit_orphan`, `output_flood`) blocks after readiness until `DIR/release` exists.

**Fault runner.** `python3 -I -S tests/receipt_supervisor_faults.py --fault NAME --pgid-file DIR/wrapper.pgid -- ARGV`. The runner inserts the repository root into `sys.path` explicitly because `-I` drops the script directory, imports `scripts.receipt_inventory_supervisor` as a module, replaces `publish`, `read_capture` or `tick` on the module, and calls `main`. It derives `DIR` from `--pgid-file`. Every fault writes `DIR/fault.fired` by atomic rename immediately before performing its fault. No production hooks exist; the production script has no fault switch. Recovery uses L4, so the invocation shape has no bearing on identity.

Faults, all unconditional once their gate is met:

| Fault | Gate | Behavior |
|---|---|---|
| `stall_wait` | `wrapper.pgid` present | `tick("wait")` writes `supervisor.stalled`, then sleeps forever |
| `stall_cleanup` | first killpg done | `tick("cleanup")` sleeps forever |
| `publish_error` | `producer.json` present (4 s) | `publish` raises `OSError` without writing |
| `die_before_publish` | `producer.json` present (4 s) | `publish` self-SIGKILLs without writing |
| `die_after_ready` | published and `producer.json` present (4 s) | `tick("wait")` self-SIGKILLs |
| `capture_error` | `producer.json` present (4 s) | first `read_capture` call raises; no later call occurs because capture is disabled |
| `inject_signal:<phase>:<n>` | phase reached | `tick(<phase>)` raises the real signal in-process, n times; `startup` uses the rendezvous |

Gate timeouts are failures of the fault runner (exit 13), never silent fallthrough.

**Fault-specific identity flow in W.**

| Faults | W identity flow |
|---|---|
| `stall_wait`, `stall_cleanup`, `die_after_ready`, `capture_error`, `inject_signal:wait`, `inject_signal:cleanup` | normal path: `wrapper.pgid`, `/proc` fields, readiness records; then assert `fault.fired` exists and its `st_mtime_ns` is not earlier than `producer.json`'s |
| `publish_error`, `die_before_publish` | readiness record is the identity source; after `fault.fired` exists assert `wrapper.pgid` is absent; on `publish_error` also assert status `pgid` equals the readiness `pgid`; on `die_before_publish` assert the kernel-derived group from recovery equals it |
| `inject_signal:startup` | no identity: assert `wrapper.pgid` absent, `fault.fired` present, no readiness record, status `teardowns` 0 |

**Worker behaviors for recovery cases.**

| Worker case | Worker behavior after spawning S | State P must recover |
|---|---|---|
| `parent_live_supervisor` | S with `stall_wait`; identity acquisition; write `worker.stalled`; hang | live S, live workload |
| `parent_zombie_supervisor` | S with `die_after_ready`; poll `/proc/<S>/stat` until state `Z` (4 s); do not reap; write marker; hang | zombie S, workload adopted by W |
| `parent_reaped_supervisor` | S with `die_after_ready`; poll to `Z`; `waitpid(S.pid)`; write marker; hang | no S, workload adopted by W |
| `parent_missing_supervisor` | write marker before spawning anything; hang | W only, no G |

W-level recovery cases use the same routine with `direct=S`: W triggers on `supervisor.stalled`, on pipe EOF without a complete status line, or on `D_S`.

## 7. Cases

Existing assertion meanings preserved. Existing cases keep their test names. The negative control stays on its raw independent path and never runs through S. `pid_kills` expectations list roles in order.

| Case | Expected | Discriminator that reads differently if the mechanism were absent |
|---|---|---|
| silent, stderr_flood, ignore_term, descendant | INNER_TIMEOUT | inner exit 124, -9 or 137; external 9 to 16 s; `assert_empty` before any harness cleanup; `cleanup_proof` true; `kills` recorded (≥1 for ignore_term and descendant); P scan complete and empty after W exit |
| negative_control | unchanged raw path | detector raises `survivors in group`, then explicit cleanup proves empty |
| raw_wrapper_leak_ignore_term, raw_wrapper_leak_descendant | detector fires | harness `--raw-wrapper`; `assert_empty` raises after `reap_group`; then `recover_descendants(None, ...)` in W reports `pid_kills == []`, `live_before_signal ≥ 1`, `kills ≥ 1`, `scan.complete` true; then empty. Not xfail, not skip |
| clean_success | OK, exit 0 | `output` equals fixed bytes; `kills` 0; P scan complete and empty |
| command_failed | COMMAND_FAILED | `output` null; `kills` 0 |
| early_exit_orphan | ORPHANS_SWEPT | TERM-ignoring detached child; `reaped` or `kills` ≥ 1 |
| output_flood | OUTPUT_OVERFLOW | 4 MiB then exit 0; `dropped` nonzero; W drain never stalls S (S report completes, exit not 11) |
| continuous_flood | INNER_TIMEOUT | floods stdout, ignores TERM; external 9 to 16 s; `dropped` nonzero |
| stalled_reader | exit 11 | W deliberately stops draining and pre-fills the pipe; S exits by `T0+15.5`; empty |
| closed_reader | exit 11 | W closes its read end before report; empty |
| publish_error | SUPERVISOR_ERROR, `error` publish | `fault.fired` present; `wrapper.pgid` absent; status `pgid` equals readiness `pgid`; `cleanup_proof` true; `kills ≥ 1` (ignore_term fixture); empty |
| die_before_publish | W recovery | S exits -9, no status line; `wrapper.pgid` absent; `pid_kills == [direct S, state Z]`; kernel-derived G equals readiness `pgid`; `live_before_signal ≥ 1`; `kills ≥ 1`; empty |
| capture_fault | SUPERVISOR_ERROR, `error` capture | ignore_term fixture; `fault.fired` not earlier than `producer.json`; `dropped` 0 and frozen; `output` null; inner exit in timeout set; `kills ≥ 1`; `cleanup_proof` true; empty |
| injected_startup | INTERRUPTED, exit 8 | `inject_signal:startup:1` fires inside the startup rendezvous; `signal_phase` startup; `teardowns` 0; no `wrapper.pgid`; no readiness record; P scan complete and empty |
| injected_cleanup_repeat | INTERRUPTED | `teardowns` 1; `signals_received` 2; empty |
| hup_mid_run, term_mid_run | INTERRUPTED | real signal from W after identity acquisition; `teardowns` 1; empty |
| outer_deadline | OUTER_TIMEOUT | `--wait 3`, ignore_term; inner exit -9; empty |
| stalled_supervisor | W recovery | `stall_wait`; `pid_kills == [direct S, state S or R]`; `live_before_signal ≥ 1`; `kills ≥ 1`; G equals published and readiness pgid; empty |
| parent_live_supervisor | P recovery | `pid_kills == [direct W, adopted S with state S or R]`; `live_before_signal ≥ 1`; `kills ≥ 1`; one group; `scan.complete` true; empty |
| parent_zombie_supervisor | P recovery | `pid_kills == [direct W, adopted S with state Z]`; `kills ≥ 1`; one group; empty |
| parent_reaped_supervisor | P recovery | `pid_kills == [direct W]`; `kills ≥ 1`; one group; empty |
| parent_missing_supervisor | P recovery | `pid_kills == [direct W]`; zero groups; no killpg issued; `scan.complete` true |
| foreign_group_refusal | refusal | pytest spawns a setsid decoy and writes its pgid into `DIR/wrapper.pgid` before P starts; P and W see no child in that session; no killpg; case fails as `PUBLISHED_PGID_NOT_OWNED`; pytest asserts decoy `poll()` is None, then kills the decoy by its own pid |

Every fixture synchronizes on readiness or marker files written by atomic rename, never on sleeps. Every fault is gated on a bounded readiness handshake and is then an unconditional hang, raise or self-SIGKILL. Every expected outcome carries a discriminator.

## 8. Gap and fold maps

**Retained gaps from revision 3 (unchanged resolutions).**

| Gap | Resolution |
|---|---|
| 1. Parent recovery skipped adopted descendants | P kills every non-G child by L4, reaps, then anchored loop under L3, L5, L5a. Cases `parent_*` |
| 2. Publication gaps | `supervisor.pid` deleted; G derived from L2 by any subreaper ancestor. Cases `die_before_publish`, `parent_missing_supervisor` |
| 3. Non-discriminating publication fixture | `publish_error` never writes; `die_before_publish` covers death before publication |
| 4. Stalled-worker natural completion | recovery cases hold S and keep workload live; assert `live_before_signal ≥ 1`, `kills ≥ 1` |
| 5. Cmdline identity | removed; identity is `ppid` and session under L4 and L2 |

**Revision 5 folds.**

| Finding | Fold | Where |
|---|---|---|
| 1. `scan_children` cap 4096, unreadable entries | Cap removed; direct-children listing primary, bounded `/proc` fallback; explicit `complete` result; `CHILD_STAT_UNREADABLE`, `SCAN_DEADLINE`, `SCAN_INCOMPLETE` named; ECHILD proves emptiness only on a complete scan | L5a; 5.2; 5.3 |
| 2. Recovery budget overlapped oracle and stop, ate pytest margin | Absolute partitions `TR+2/+6/+8/+9`; oracle timeouts equal remaining time; `ORACLE_SKIPPED`; outer deadlines derived per class with explicit margins | 5.1 |
| 3. Record initialized after direct kill | Direct-child stop is phase 0 inside the routine and seeds `pid_kills` with role `direct` and `state_at_scan`; `parent_*` expectations include W | 5.3; section 7 |
| 4. `capture_fault` raced readiness | `capture_error` gated on `producer.json`, writes `fault.fired` first; W fault-specific identity flow table; `fault.fired` ordering asserted | section 6; section 7 |
| 5. `injected_startup` unreachable | Explicit `tick("startup")` rendezvous after handlers and prctl, before pipe and spawn, followed by the signal check | section 4 Startup |
| 6. Pipe backpressure at P and W | Continuous non-blocking per-tick drain with caps at both levels; bounded final relay with exit 12; W single pipe only | 2; 5.4; 5.5 |

## 9. Residual boundary, stated plainly

- Pipe writes above `PIPE_BUF` are not atomic; exit 11 and the consumer rule cover partial lines without proving atomicity.
- The `/proc` survivor scan in S is diagnostic only.
- Python signal latency during long C calls is bounded by caps, not measured.
- A member in uninterruptible sleep survives SIGKILL until it wakes; S reports `CLEANUP_FAILED` and the harness reports `RECOVERY_BUDGET_EXCEEDED`. Neither converts to success.
- On a kernel without `CONFIG_PROC_CHILDREN` the fallback scan may report `SCAN_INCOMPLETE` under heavy process churn; the case then fails closed and the record names the reason. Ubuntu runner kernels ship the option; the harness prints `scan.method` so the path taken is visible.
- If P itself stalls, pytest kills P by pid and fails without recovery; orphans then reparent to the runner's init. This is reported, not hidden, and the CI job timeout is the last backstop.
- L2 and L4 rely on the spawn rules in section 2 being true of our own code. The runtime escape check and the complete-and-empty scan after W's exit turn those rules into enforced contracts within this repository only.
- The supervisor in production has no ancestor recovery; SIGKILL of the supervisor or host failure leaves cleanup to the operator.

## 10. Gates, CI and rollback

Two independent parallel design reviews before any change under `scripts/` or `tests/`. Implementation updates draft PR590 only, no second PR. The existing `receipt-inventory-timeout` job in `.github/workflows/test.yml` runs the harness unchanged in shape; every enumerated case must run green on Linux with survivor assertions before any harness cleanup, plus full pytest and the test-count baseline on the exact head. All four reviewer vectors are recorded on the final SHA; `.reviewers/590.toml` stays empty until then. Collection remains blocked behind reducer and META adversarial tests and a separately approved identity preflight. Rollback is reverting the script, fault runner and test file; no deployment.
