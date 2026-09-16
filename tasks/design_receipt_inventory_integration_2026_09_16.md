**New primitives introduced:** two disposable artifacts, both already named in the approved plan and not yet built: `scripts/receipt_inventory_sequence.py`, a fixed-sequence driver that spawns exactly one native `ssh` or `scp` process per operation with stdout and stderr written to files (never a pipe), enforces a local wall clock and local capture-size bounds by polling, persists the native return code atomically before anything is parsed, and evaluates each captured file against fixed gates; and `tests/test_receipt_inventory_sequence.py`, a synthetic end-to-end exercise that runs the identical remote strings through a substitute argv builder on Linux plus cross-platform tests of the CLI, manifest, state machine and grammar. Plus one findings document after the run. No collector, supervisor change, API, generic transport framework, dependency, schema, secret, config or repository writer. Reuses `scripts/receipt_inventory_supervisor.py`, PR592's `scripts/receipt_inventory_reducer.py` and `scripts/receipt_inventory_meta.py`, and loads `usable_envelope` from `tests/test_receipt_inventory_envelope.py` rather than duplicating it. DESIGN ONLY, revision 2 (folds both reviews of 0821bad3): no build until two design reviews; no execution until PR592 is merged on exact-head green CI and the synthetic exercise is green.

# Design: production read-only receipt inventory integration, revision 2

## Hermes-first analysis

Carried from the approved plan (`plan_receipt_inventory_integration_2026_09_16.md`, coordinator checks 2026-09-16 19:30 UTC, approved at 035cd914). No fresh check was made for this revision and none is claimed.

| Domain | Checked (plan evidence, 2026-09-16) | Limited verdict |
|---|---|---|
| Journal reduction, host metadata validation | https://hermes-agent.nousresearch.com/docs/skills : catalog still Loading; no verified matching skill | No skill verified; not an exhaustive absence claim |
| Ecosystem | https://github.com/0xNyk/awesome-hermes-agent : general orchestration tools; no verified Gecko receipt-inventory replacement | Not a replacement |
| Remote execution and staging | Stock OpenSSH `ssh`/`scp`, host coreutils, `pgrep` | Reuse; no custom protocol |
| Process ownership, reduction, validation, usability | In-repo primitives above | Reuse unchanged |

## 1. Decision change: the plan-scoped driver, not a hand-run checklist

Revision 1 chose a hand-run checklist with a spawn-nothing renderer. Both reviewers showed it cannot meet the plan: a typed `ssh ... > file` line does not persist the native return code, cannot enforce a local wall clock or a capture-size bound, and PowerShell redirection re-encodes native stdout (UTF-16 with CRLF under Windows PowerShell 5.1), which breaks byte-exact envelope gates. Rather than invent guarantees for the checklist, this revision selects the plan's other permitted candidate: the fixed-sequence driver. The two-step SSH discipline is preserved in its substantive form: stdout goes to a file handle, never a pipe, and is read in a separate step after the process has ended and its return code has been persisted. The driver is not a transport framework: it knows exactly 20 operations, one argv shape for `ssh`, one for `scp`, and nothing else.

## 2. Run manifest and durable state

`init` creates `RUNDIR` outside the repository (`%LOCALAPPDATA%\receipt_inventory\<run-id>\` on Windows, `$XDG_RUNTIME_DIR` or `/tmp` equivalent on Linux) and writes `manifest.json` once. The manifest is immutable: `state.json` stores its sha256, and every later subcommand recomputes and compares it before doing anything; a mismatch is `MANIFEST_CHANGED`, fatal, no operation runs. Manifest fields, all validated against the grammar in section 4 at `init`:

| Field | Value at run time | How it is verified on the host |
|---|---|---|
| `host`, `user` | coordinator-supplied; candidate hostname per `tasks/lessons.md:202-219`, user `root` | op 1 must echo both |
| `repo`, `unit` | candidates `/root/gecko-alpha`, `gecko-pipeline.service` (`design_suppression_receipt_inventory_2026_09_14.md:50`) | op 2 must succeed against them |
| `expected_head` | `77751890c9f1f51ed348c365d4e7a5985ea2827d` (`current_closeout_queue_2026_09_14.md:15`) | op 2 line 1 and op 9 META `head`; mismatch fatal |
| `window_start`, `window_end` | `2026-09-14T20:45:00Z`, `2026-09-14T21:45:00Z` | `start_s`, `end_s`, `start_us`, `end_us` derived by `init` with `datetime`; a unit test pins the derivation |
| `script_sha256` (3) | computed by `init` from the local files that will be staged | op 8 and op 11 |
| `source_sha256` (5) | supplied in a pins file produced by the documented local procedure `git show <expected_head>:<path> \| sha256sum` for the five source paths; `init` records the pins file digest | op 9 five META slots |
| `merged_sha` | declared merged PR592 master SHA the local checkout is at | recorded in findings; not host-verified |
| `tmp` | absent until op 4 | must fullmatch `^/tmp/receipt_inventory\.[A-Za-z0-9]{10}$` |

`state.json` holds `manifest_sha256`, `next` (an operation id or `END`), `connections_used`, per-operation records (`rc`, verdict, sha256 of `opNN.out`, `opNN.err`, `opNN.rc`), `tmp`, and the cleanup ledger. Every transition is written to `state.json.tmp` and `os.replace`d; a crash leaves the previous state intact. `run N` is accepted only when `next == N`; evaluating the same operation twice, running out of order, or running after `END` is `REPLAY_REJECTED`. Observation attempts are appended to `attempts.jsonl` with fsync **before** the process is spawned; a second attempt of the same observation is refused. Connection accounting is `connections_used`, incremented before spawn; the 21st is refused. The budget is exactly 20: 18 sequence operations, one recovery `R`, and one post-recovery removal.

## 3. Operation execution contract (`run N`)

1. Verify manifest digest and `next == N`. For observation ops, append the attempt record and fsync.
2. Build argv as a list (no shell): `["ssh", "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=yes", "USER@HOST", REMOTE]` or `["scp", <same -o options>, LOCAL, "USER@HOST:TMP/<name>"]`. Argv building is the module-level function `build_argv(op, manifest)`; the synthetic test replaces it.
3. Open `opNN.out` and `opNN.err` for binary writing; `subprocess.Popen(argv, stdin=DEVNULL, stdout=out_fh, stderr=err_fh)`. Increment `connections_used` and persist state before this call.
4. Poll every 100 ms until the process ends, the local wall clock expires, or either file exceeds its cap. On expiry or overflow: `kill()`, then `wait(timeout=5)`; if the wait itself times out, record `KILL_UNCONFIRMED`.
5. Persist `opNN.rc` atomically: `{"rc": <int or null>, "killed": null | "WALLCLOCK" | "OVERSIZE_OUT" | "OVERSIZE_ERR" | "KILL_UNCONFIRMED", "wall_ms": <int>, "out_bytes": <int>, "err_bytes": <int>}`. This happens before any parsing.
6. Evaluate: read `opNN.out` with `read(cap + 1)`; never read `opNN.err` beyond its size. A missing or malformed `opNN.rc` fails closed: control ops `FATAL RC_MISSING`; observation ops `UNPROVEN`.
7. Persist the verdict and `next` atomically.

Limits per operation kind:

| Kind | Remote bound | Local wall clock | `opNN.out` cap | `opNN.err` cap |
|---|---|---|---|---|
| control (1, 2, 3, 4, 8, checks, 17, R, 18) | `timeout -k 2 10` around the remote body, stdout capped by `head -c 4097` under `set -o pipefail` | 25 s | 4,097 | 65,536 |
| scp (5, 6, 7) | none | 30 s | 4,097 | 65,536 |
| observation (9, 11, 13, 15) | none: the supervisor's own report deadline is T0 plus 15.5 s plus interpreter start; the wrapper inside it is `timeout -k 2 10` | 40 s, consistent with the plan | 131,073 | 65,536 |

A remote `head -c` that truncates makes the producer exit 141 under `pipefail`; the evaluator treats rc 141 on a control op as `FATAL REMOTE_OVERSIZE`. Observation ops are not wrapped in a remote `timeout` or `head`, so the supervisor's single line and native exit code arrive unchanged; the local 40 s clock is the only outer bound. If it fires, the local `ssh` dies, the remote session receives `SIGHUP`, the supervisor's handler records `INTERRUPTED` and runs its cleanup and report, but the report is not captured locally, so the attempt is `UNPROVEN` and only recovery may follow.

## 4. Parameter grammar and quoting

The remote strings are fixed templates; parameters are substituted only after `re.fullmatch` against these patterns, and any failure is `FATAL PARAM_GRAMMAR` at `init`:

| Parameter | Pattern | Extra rule |
|---|---|---|
| `host` | `[A-Za-z0-9][A-Za-z0-9.-]{0,252}` | never starts with `-`; no `@` or `:` |
| `user` | `[a-z_][a-z0-9_-]{0,31}` | |
| `repo` | `/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*` | no `..` segment, length at most 200 |
| `unit` | `[A-Za-z0-9_@.-]{1,56}\.service` | |
| `expected_head`, `merged_sha` | `[0-9a-f]{40}` | |
| `window_*` | `[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z` | start before end after derivation |
| `tmp` | `/tmp/receipt_inventory\.[A-Za-z0-9]{10}` | only ever taken from op 4's own output |
| local paths (`RUNDIR`, staged scripts) | `[A-Za-z]:\\[A-Za-z0-9_.\\-]+` on Windows, `/[A-Za-z0-9_./-]+` on Linux | never starts with `-` |

Because argv is a list, no local shell parses anything. The remote login shell parses the remote string once; every parameter admitted by the grammar is free of whitespace, quotes, `$`, backtick, `;`, `&`, `|`, `<`, `>`, `(`, `)`, `{`, `}`, `*`, `?`, `[`, `]`, `!`, `#`, `~`, `\` and newline, so substitution cannot open a new token. Inside the observation strings the wrapper payload is double-quoted for `bash -c`; the payload contains no `$` or backtick, so the login shell passes it verbatim. Control strings deliberately use `$(...)`, `$f`, `$g` and `$?` outside any parameter position. Unit tests assert, for every rendered string: no single quote; every parameter occurrence equals the admitted value; and the string equals the template with placeholders replaced and nothing else changed. Injection tests feed `-oProxyCommand=x`, `x;id`, `$(id)`, a quote, a space, a newline and a `..` segment to each parameter and assert `PARAM_GRAMMAR` before any file is written.

## 5. The operations: remote strings and gates

`C(body)` below means `set -o pipefail; { body; } 2>&1 | head -c 4097` wrapped as `timeout -k 2 10 bash -c "..."`; op 1 is unwrapped because it establishes that the login shell is bash.

| Op | Remote string (after substitution) | Gate from `opNN.out` plus `opNN.rc` |
|---|---|---|
| 1 | `hostname; id -un; uname -s; echo BASH=$BASH_VERSION` | rc 0; lines equal `host`, `user`, `Linux`, and `BASH=` followed by a version; else `FATAL HOST_IDENTITY` or `FATAL SHELL_NOT_BASH` |
| 2 | `C(cd REPO && git rev-parse HEAD && git status --porcelain \| wc -l && systemctl show UNIT -p ActiveState -p StandardOutput -p ExecMainPID -p ExecMainStartTimestamp && git reflog show --date=iso --format=%H%x20%gd -n 10)` | rc 0; line 1 equals `expected_head` (`FATAL HEAD_MISMATCH`); line 2 is `0` (`FATAL DIRTY_CHECKOUT`); `ActiveState=active`, `StandardOutput=journal` (`FATAL UNIT_STATE`); `ExecMainPID` integer; reflog lines match `[0-9a-f]{40} HEAD@\{[^}]{1,40}\}`; export count and pairs newer than `ExecMainStartTimestamp`, at most ten |
| 3 | `C(python3 --version; timeout --version \| head -n 1; bash --version \| head -n 1; for t in head cat sha256sum stat mktemp journalctl systemctl git pgrep ls rm; do if command -v $t >/dev/null; then echo TOOL $t ok; else echo TOOL $t missing; fi; done; journalctl --disk-usage; stat -c %a:%U:%F /tmp)` | rc 0; `Python 3\.(1[0-9]\|[2-9][0-9])`; `GNU coreutils`; every `TOOL` line `ok` (a missing `pgrep` is `FATAL NO_PGREP`, the plan's stated residual); one disk-usage line exported verbatim; `/tmp` line `(1777\|755):root:directory` |
| 4 | `C(d=$(mktemp -d /tmp/receipt_inventory.XXXXXXXXXX) && stat -c %n:%a:%U:%F $d)` | rc 0; exactly one line `TMP:700:USER:directory` with `TMP` matching the pattern; `tmp` stored; anything else `FATAL TMP_PATTERN` |
| 5, 6, 7 | `scp` of supervisor, reducer, META to `TMP/<name>` | rc 0 else `FATAL SCP_FAILED` |
| 8 | `C(cd TMP && sha256sum receipt_inventory_supervisor.py receipt_inventory_reducer.py receipt_inventory_meta.py && ls -Ap \| wc -l)` | rc 0; three digests equal `script_sha256` in order; count `3`; else `FATAL HASH_MISMATCH`. **No staged file executes before this gate passes.** |
| 9 | `python3 -I -S TMP/receipt_inventory_supervisor.py --pgid-file TMP/wrapper.pgid.1 -- timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c "{ git -C REPO rev-parse HEAD; systemctl show UNIT -p ActiveState -p StandardOutput; } \| head -c 4097 \| python3 -I -S TMP/receipt_inventory_meta.py REPO scout/main.py scout/outcome_ledger.py scout/trading/decision_events.py scout/trading/signals.py scout/trading/engine.py"` | envelope captured or recorded missing; rc persisted |
| 10 | `C(f=TMP/wrapper.pgid.1; if [ -s $f ]; then g=$(cat $f); echo PGID $g; pgrep -g $g; echo PGREP_EXIT $?; else echo PGID MISSING; fi; ls -Ap TMP)` | cleanup `PROVEN` (section 6); `usable_envelope(rc, line, "meta")` true; META `head == expected_head`, `active_state == "active"`, `standard_output == "journal"`, slots `f0`..`f4` `OK` with digests equal to `source_sha256` in order, `f5`..`f7` `UNUSED`; else `FATAL META_PREFLIGHT_MISMATCH` or `FATAL SOURCE_PIN_MISMATCH` |
| 11 | as 9 with `wrapper.pgid.2` and payload `... \| python3 -I -S TMP/receipt_inventory_meta.py TMP receipt_inventory_supervisor.py receipt_inventory_reducer.py receipt_inventory_meta.py` | envelope captured |
| 12 | check for K=2 | `PROVEN`; `usable_envelope(..., "meta")` true; `f0`..`f2` digests equal `script_sha256`; same head and state rules |
| 13 | as 9 with `wrapper.pgid.3` and payload `journalctl -u UNIT --until=@START_S -n 1 -o json --no-pager -q \| head -c 2097153 \| python3 -I -S TMP/receipt_inventory_reducer.py 0 START_US` | envelope captured |
| 14 | check for K=3 | `PROVEN` required; floor classified per section 7, non-fatal |
| 15 | as 9 with `wrapper.pgid.4` and payload `journalctl -u UNIT --since=@START_S --until=@END_S -n 200 -o json --no-pager -q \| head -c 2097153 \| python3 -I -S TMP/receipt_inventory_reducer.py START_US END_US` | envelope captured |
| 16 | check for K=4 | `PROVEN` required; observation classified per section 7, non-fatal |
| 17 | `C(ls -Ap TMP; for n in 1 2 3 4; do f=TMP/wrapper.pgid.$n; if [ -s $f ]; then g=$(cat $f); echo PGID $n $g; pgrep -g $g; echo PGREP_EXIT $n $?; else echo PGID $n MISSING; fi; done)` | every listed name in the permitted set, with no trailing `/` (a directory named like a permitted file is rejected); for every attempted K: `PGID K g` with `g` equal to the captured envelope's `pgid`, no pid line, `PGREP_EXIT K 1`; for every unattempted K: `MISSING`; verdict `ALL_PROVEN` or `RETAINED` |
| 18 | `C(rm -rf -- TMP && if [ ! -e TMP ]; then echo REMOVED; fi)` | rc 0 and exactly `REMOVED`; rendered only after `ALL_PROVEN` |
| R | op 17's string | same evaluation over `attempts.jsonl`; `ALL_PROVEN` licenses one op 18; otherwise `RETAINED` |

Permitted entry set: `receipt_inventory_supervisor.py`, `receipt_inventory_reducer.py`, `receipt_inventory_meta.py`, `wrapper.pgid.1`..`4`, `wrapper.pgid.1.tmp`..`4.tmp`.

## 6. Cleanup proof for every attempted observation

For attempt K, using the envelope file of the observation op and the check file of the following op, both with their `opNN.rc`:

1. Observation `opNN.rc` present with `killed` null and `rc` an integer; the out file is exactly one LF-terminated line within the cap; the line parses through the loaded envelope module's duplicate-rejecting parser to exactly the supervisor's 17 `STATUS_KEYS`; `cleanup_proof is True`; `pgid` is an int greater than 1.
2. Check `opNN.rc` present with `rc` 0; `PGID g` with `g == pgid`; no pid line; `PGREP_EXIT 1`.
3. Both hold: `PROVEN`. A leader already gone is consistent with `PROVEN`.
4. Otherwise `UNPROVEN`, including: `PGID MISSING`; `PGREP_EXIT 0`; any `killed` value; rc 9, 11, 124, 137 or 255 on the observation; missing, oversize or unparseable envelope; a check whose `PGID` differs from the envelope's `pgid`. `UNPROVEN` is fatal; `next` becomes `R`.

Supervisor exit rows (`EXIT_CODES`, `scripts/receipt_inventory_supervisor.py:39-50`): 0 `OK`, 3 `OUTPUT_OVERFLOW`, 4 `COMMAND_FAILED`, 5 `ORPHANS_SWEPT`, 6 `INNER_TIMEOUT`, 7 `OUTER_TIMEOUT`, 8 `INTERRUPTED`, 10 `SUPERVISOR_ERROR` may still be `PROVEN` under rules 1 and 2 while the observation is unavailable; 9 `CLEANUP_FAILED`, 11 report incomplete, 124 and 137 (an outer timeout, not expected in production since none is applied) and 255 (ssh failure) are always `UNPROVEN` because no validated report exists. The driver never signals a remote process; a static test asserts the only `kill` call targets its own local `Popen` child.

## 7. Observation classification

Every observation result is one of three classes, computed by the loaded `usable_envelope` plus fixed rules, never by ad hoc parsing:

- `UNAVAILABLE`: `usable_envelope` false for any reason, including singletons `OVERFLOW`, `RECORD_CAP`, `INTERNAL_ERROR`, `USAGE`, or a non-`OK` supervisor status. Counts from an unusable envelope are never reported.
- `ZERO`: usable and every `events[*]` is 0. Fixed sentence: "no allowlisted event observed in the window at an unknown effective log level".
- `POSITIVE`: usable and at least one `events[*]` greater than 0; the 13 counts, the 11 key presence counts, `unknown_events`, `unknown_keys` and `observed_span` are reported.
- Cap flag: if `saturated` is true, the class carries `CAPPED` and the fixed sentence "the query was capped at 200 entries; every count is a lower bound and the window is not fully observed". `POSITIVE` with `CAPPED` never becomes a completeness claim; `ZERO` with `CAPPED` is reported as `UNAVAILABLE` because 200 records without any allowlisted event does not bound the remaining entries.
- Floor (observation 3): `POSITIVE_FLOOR` only when usable and `observed_span.first` and `last` are both strings parsing strictly before `start_us`; otherwise `FLOOR_UNPROVEN`. Fixed sentence for the positive case: "one retained journal record before the window exists"; no sentence about continuity or coverage exists in the module.
- D1 to D4: the findings renderer writes `UNKNOWN` unconditionally.

## 8. Recovery, once

`R` is offered when `next` is `R`: after any `UNPROVEN`, after any observation or check whose `opNN.rc` is missing or carries a `killed` value, or after a control op with rc 255 later than op 4. It uses op 17's string and evaluation over `attempts.jsonl`. `ALL_PROVEN` licenses one op 18 (the 20th connection at most); if no observation was attempted, permitted-content verification alone licenses op 18. Anything else is `RETAINED`: the findings carry the exact `tmp` and every attempted pgid for operator disposition, no process is signalled, nothing is retried, and `next` becomes `END`.

## 9. Sequence module contract

`scripts/receipt_inventory_sequence.py`:

- Imports exactly `datetime`, `hashlib`, `importlib.util`, `json`, `os`, `re`, `subprocess`, `sys`, `time`. It loads `tests/test_receipt_inventory_envelope.py` by path with the same `load()` pattern that module uses, and takes `usable_envelope`, `_parse` and `STATUS_KEYS` from it; that module's own imports are stdlib and the three scripts.
- Subcommands: `init --rundir D --host --user --repo --unit --expected-head --window-start --window-end --merged-sha --source-pins FILE --scripts-dir DIR`; `run N` (N in 1..18, `R`); `show N` (prints the argv that `run N` would use, for the record); `findings` (renders `tasks/findings_receipt_inventory_<date>.md` content to stdout from `state.json` only).
- The only process it ever starts is the one `Popen` in `run`, with `stdin=DEVNULL`, file handles for stdout and stderr, no shell, and a bounded `wait(timeout=5)` after a kill; `tests/test_round8_subprocess_timeouts.py` sees a bound on every site. No `killpg`, no signal to any pid other than `child.pid`.
- Every state write is temp-file plus `os.replace`; every append to `attempts.jsonl` is followed by `flush` and `os.fsync`.
- Exports written into findings are only the sanitized values named in section 5 and the classes in section 7; raw `opNN.*` files stay in `RUNDIR`.

## 10. Synthetic end-to-end exercise

`tests/test_receipt_inventory_sequence.py`, Linux-only class for execution, cross-platform classes for the pure parts.

Execution substitute: the test replaces `build_argv` so `ssh` ops become `["bash", "-c", REMOTE]` and `scp` ops become `["cp", LOCAL, TMP/<name>]`, with `PATH` prefixed by a stub `bin/` containing `hostname` (prints the manifest host), `systemctl` (prints the four properties from a per-scenario file), and `journalctl` (emits fixture records honouring `-n`, `--since=@`, `--until=@`, `--disk-usage`); real `id`, `uname`, `mktemp`, `stat`, `sha256sum`, `pgrep`, `timeout`, `git`, `python3`. The synthetic `REPO` is a real git repository holding copies of the five real source files committed at a known SHA that the test passes as `expected_head`, with a pins file computed by the same `git show` procedure. Every scenario drives the real CLI (`init`, then `run N` in order) through `subprocess.run(..., timeout=90)`, so the durable state, rc files and replay rules are exercised as shipped, and asserts the exact verdict sequence plus which operations were spawned.

Scenarios, one method each:

1. Happy path: 18 operations, `ALL_PROVEN`, `REMOVED`, `tmp` absent, `findings` renders with three identities, `ZERO` or `POSITIVE` as the fixture dictates, D1 to D4 `UNKNOWN`.
2. Dirty checkout: untracked file in `REPO`; op 2 `FATAL DIRTY_CHECKOUT`; ops 4 and later never spawned; no `mktemp` occurred.
3. Hash mismatch: the copy for op 6 is altered by one byte; op 8 `FATAL HASH_MISMATCH`; no observation spawned; op 18 licensed by permitted-content verification; `REMOVED`.
4. Deterministic publication failure: before op 9 the test creates a **directory** named `TMP/wrapper.pgid.1`, so the supervisor's `os.replace` of its `.tmp` file fails regardless of uid (`error=publish`, exit 10, `cleanup_proof` true); op 10 sees `PGID MISSING` and returns `UNPROVEN`; `R` lists `wrapper.pgid.1/` with a trailing slash and returns `RETAINED`; the test removes its own directory afterwards.
5. Disconnect: the test deletes `op13.out` after `run 13` returns but before `run 14`; `run 14` is refused because the observation's out digest no longer matches state; `R` returns `RETAINED` although `pgrep` reads empty.
6. Oversize: the `journalctl` stub for op 15 is replaced by one whose output makes the supervisor line exceed nothing (the supervisor caps itself), so oversize is exercised at the driver level instead: a stub `bash` argv that writes 131,073 bytes of junk before the real command; the driver kills with `OVERSIZE_OUT`, records it in `op15.rc`, and `run 16` yields `UNPROVEN`; the test verifies the group it created is empty with `assert_empty` from `tests/test_receipt_inventory_timeout.py` and cleans up under a deadline.
7. Wall clock, loss while the child is alive: the observation 15 fixture is a silent producer that sleeps 30 s and the test lowers the local wall clock to 3 s through a manifest override permitted only under a test flag; the driver kills the local `bash` (`WALLCLOCK`), `run 16` is refused and `next` is `R`; the orphaned supervisor completes its own inner timeout and cleanup; the test waits up to 20 s, then `R` finds the group empty but no validated envelope and returns `RETAINED`; the test cleans its own `TMP`.
8. Outer KILL of the supervisor while the wrapper is alive: the test SIGKILLs the supervisor process it can identify by its own `TMP` path in `/proc/*/cmdline`; the leader survives; `R` shows `PGREP_EXIT 0` and returns `RETAINED`; the test performs bounded cleanup of the group it owns with `killpg` and reaps, asserting the driver never signalled it.
9. Stalled supervisor: `build_argv` substitutes `tests/receipt_supervisor_faults.py --fault stall_wait` for the supervisor path in op 9 only, explicitly labelled as a non-production string; wall clock kills locally; `R` returns `RETAINED`; test cleanup as in 8.
10. Non-fatal observation: `journalctl` stub exits 1 for op 15; supervisor `COMMAND_FAILED`, `cleanup_proof` true; op 16 `PROVEN` and class `UNAVAILABLE`; `ALL_PROVEN`; `REMOVED`.
11. Floor semantics: a pre-window record (`POSITIVE_FLOOR`); a record exactly at `start_s` returned by the inclusive `--until` (`out_of_window`, `FLOOR_UNPROVEN`); a malformed pre-window record (`FLOOR_UNPROVEN`).
12. Classification: fixtures producing `POSITIVE`, `ZERO`, `POSITIVE CAPPED` (200 records) and `ZERO CAPPED` reported as `UNAVAILABLE`.

Cross-platform tests on the module alone: grammar and injection cases of section 4; every rendered string has no single quote and equals its template; `start_us`/`end_us` derivation; every rc row of section 6 through the evaluator with synthetic `opNN.*` files, including missing `opNN.rc`, `killed` values, 9, 11, 124, 137, 255; manifest change after `init` is `MANIFEST_CHANGED`; `run` out of order, twice, or after `END` is `REPLAY_REJECTED`; the 21st connection is refused; a second attempt of one observation is refused; `state.json` survives a simulated crash between temp write and replace; the permitted-name check rejects a trailing slash; `TMP` pattern gate.

## 11. Stops and residuals

Fixed fatal reasons: `MANIFEST_CHANGED`, `REPLAY_REJECTED`, `PARAM_GRAMMAR`, `RC_MISSING`, `HOST_IDENTITY`, `SHELL_NOT_BASH`, `HEAD_MISMATCH`, `DIRTY_CHECKOUT`, `UNIT_STATE`, `NO_PGREP`, `TOOLCHAIN`, `TMP_PATTERN`, `SCP_FAILED`, `HASH_MISMATCH`, `REMOTE_OVERSIZE`, `OVERSIZE_OUT`, `OVERSIZE_ERR`, `WALLCLOCK`, `META_PREFLIGHT_MISMATCH`, `SOURCE_PIN_MISMATCH`, `UNPROVEN`, `RETAINED`. Non-fatal: `FLOOR_UNPROVEN`, `UNAVAILABLE` on observation 4. Residual stated rather than widened: if the target's login shell is not bash or `pgrep` is absent, the run stops before any write and the findings name the missing primitive; this design substitutes nothing.

## 12. Files, gates and rollback

Files after design approval: `scripts/receipt_inventory_sequence.py`, `tests/test_receipt_inventory_sequence.py`, later `tasks/findings_receipt_inventory_<date>.md`. Nothing else changes; no workflow edit, the new test runs in the existing full `test` job. Gates: two independent design reviews on this revision; PR592 merged on exact-head green CI; build with TDD; two PR reviews and Linux CI green including the synthetic exercise; then one production sequence; then the findings PR with two reviews and a clearance file. Rollback of the code is a revert; rollback on the host is op 18 or operator disposition of one retained directory whose exact path and pgids are in the findings.
