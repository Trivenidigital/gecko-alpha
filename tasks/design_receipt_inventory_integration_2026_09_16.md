**New primitives introduced:** two disposable artifacts, both plan-scoped and not yet built: `scripts/receipt_inventory_sequence.py`, a spawn-nothing renderer and gate evaluator for the fixed 18-operation table (it prints the exact local command for one operation and judges one captured output file; it never opens a connection or runs a process), and `tests/test_receipt_inventory_sequence.py`, a Linux-only synthetic end-to-end exercise that executes the identical remote strings through a substitute executor. Plus one findings document after the run. No collector, supervisor change, API, persistent transport coordinator, dependency, schema, secret, config or repository writer. Reuses `scripts/receipt_inventory_supervisor.py`, PR592's `scripts/receipt_inventory_reducer.py` and `scripts/receipt_inventory_meta.py`, and `usable_envelope` from `tests/test_receipt_inventory_envelope.py`. DESIGN ONLY: no build until two design reviews; no execution until PR592 is merged on exact-head green CI and the synthetic exercise is green.

# Design: production read-only receipt inventory integration

## Hermes-first analysis

Carried from the approved plan (`plan_receipt_inventory_integration_2026_09_16.md`, coordinator checks 2026-09-16 19:30 UTC, approved at 035cd914). No fresh check was made for this design and none is claimed.

| Domain | Checked (plan evidence, 2026-09-16) | Limited verdict |
|---|---|---|
| Journal reduction, host metadata validation | https://hermes-agent.nousresearch.com/docs/skills : catalog still Loading; no verified matching skill | No skill verified; not an exhaustive absence claim |
| Ecosystem | https://github.com/0xNyk/awesome-hermes-agent : general orchestration tools; no verified Gecko receipt-inventory replacement | Not a replacement |
| Remote execution and staging | Stock OpenSSH `ssh`/`scp`, host coreutils, `pgrep` | Reuse; no custom protocol |
| Process ownership, reduction, validation, usability | In-repo primitives above | Reuse unchanged |

## 1. Decision: gated checklist, not a driver

The plan left one choice: a driver that runs the sequence, or a checklist. This design chooses a **gated checklist**. The coordinator runs each operation by hand on Windows using the repository's two-step SSH pattern (redirect stdout to a file, then read the file; `design_eligible_backlog_sweep_2026_05_27.md:88-93`, `validation_pr158_held_position_refresh_rate_gap.md:43`). The sequence module does only two things: `render N` prints the exact command to type, and `evaluate N` reads that operation's captured file under a byte cap and prints a verdict. It spawns nothing on Windows, so there is no stdout-capture workaround to get wrong, and the same module's `remote_string(N)` is what the synthetic test executes on Linux. This keeps the exact strings in one place without a production process runner.

Reason against a driver: a Windows subprocess runner would have to reimplement the two-step discipline and would be a second transport mechanism to review. Reason against a plain markdown checklist: the synthetic exercise could not execute "the exact strings" without duplicating them by hand.

## 2. Parameters, freshly verified, never baked in

`render` and `evaluate` take every host value on the command line; the module contains no host, path, unit, revision or window literal. Candidate values recorded here for the coordinator to supply are **candidates**, each confirmed by a preflight export before any later operation uses it:

| Parameter | Candidate (source) | Verified by |
|---|---|---|
| `HOST` | the production VPS hostname as recorded in `tasks/lessons.md:202-219` | op 1 export must equal it |
| `USER` | `root` (09-14 design) | op 1 `id -un` |
| `REPO` | `/root/gecko-alpha` (`design_suppression_receipt_inventory_2026_09_14.md:50`) | op 2 succeeds only if it is a git checkout |
| `UNIT` | `gecko-pipeline.service` (same) | op 2 `ActiveState` present |
| `EXPECTED_HEAD` | `77751890c9f1f51ed348c365d4e7a5985ea2827d` (`current_closeout_queue_2026_09_14.md:15`) | op 2 first line; mismatch is fatal, never accepted |
| `WINDOW_START`, `WINDOW_END` | `2026-09-14T20:45:00Z`, `2026-09-14T21:45:00Z` (09-14 design) | `START_S`, `END_S`, `START_US`, `END_US` derived in-module from the ISO strings with `datetime`; a unit test pins the derivation against `datetime(2026, 9, 14, 20, 45, tzinfo=timezone.utc)` |
| expected script hashes | sha256 of the three scripts at the merged PR592 master SHA, computed locally at run start | op 8 |
| `TMP` | unknown until op 4 | must fullmatch `^/tmp/receipt_inventory\.[A-Za-z0-9]{10}$` before any later string is rendered |

Window bounds are passed to `journalctl` as `@EPOCHSECONDS`, which avoids timezone parsing and shell spaces.

## 3. Local run directory, budgets and byte caps

All local artifacts live outside the repository in `%LOCALAPPDATA%\receipt_inventory\<run-id>\` (`RUNDIR`): `state.json`, `attempts.jsonl`, and per operation `opNN.out` and `opNN.err`. `evaluate` opens `opNN.out` and reads at most cap plus one byte; cap plus one is `OVERSIZE`, fatal, and nothing is parsed. `opNN.err` is never parsed; only its byte count is recorded. Caps: control operations 4,096 bytes; `scp` operations 4,096 bytes; observation operations 131,073 bytes.

Time budgets. Each observation is bounded on the host by `timeout -k 5 45` around the supervisor: the supervisor's own report deadline is T0 plus 15.5 s plus interpreter startup; 45 s is a last resort. If it fires, the supervisor receives TERM, which its handler records as `INTERRUPTED` and follows with its normal cleanup and report phases; KILL after a further 5 s means no report, and no report means no cleanup proof. Control operations are bounded by `timeout -k 2 10` on the host. Every `ssh`/`scp` uses `-o BatchMode=yes -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=3`, so a dead connection surfaces within about 15 s; no Windows-side process killer is needed. Finite connections: 18 sequence operations plus at most 1 recovery connection; `render` refuses a 20th.

## 4. Durable attempt log

Before printing the command for an observation (ops 9, 11, 13, 15), `render` appends `{"op": N, "obs": K, "pgid_file": "TMP/wrapper.pgid.K", "attempted_at": <UTC ISO>}` to `attempts.jsonl` and fsyncs it. `render` refuses to render an observation whose attempt record already exists: there are no retries, and an ambiguous transport outcome cannot be re-run. Cleanup and recovery quantify over `attempts.jsonl`, never over the files that happen to exist on the host.

## 5. The 18 operations: exact remote strings and gates

Every local command is `ssh <options> USER@HOST '<remote>' > RUNDIR\opNN.out 2> RUNDIR\opNN.err` or the `scp` form. Remote strings contain no single quote (a unit test asserts this for every rendered string), no `$`-expansion except where shown, and no secret. Placeholders are substituted by `render`.

| Op | Remote string (after substitution) | Gate evaluated from `opNN.out` |
|---|---|---|
| 1 | `hostname; id -un; uname -s` | exactly three lines equal to `HOST`, `USER`, `Linux`; export is `MATCH`/`MISMATCH` |
| 2 | `cd REPO && git rev-parse HEAD && git status --porcelain \| wc -l && systemctl show UNIT -p ActiveState -p StandardOutput -p ExecMainPID -p ExecMainStartTimestamp && git reflog show --date=iso --format=%H%x20%gd -n 10` | line 1 equals `EXPECTED_HEAD`; line 2 is `0` (nonzero is fatal); `ActiveState=active`; `StandardOutput=journal`; `ExecMainPID` integer; reflog lines are `40-hex HEAD@{timestamp}` only, and the evaluator exports the count of entries newer than `ExecMainStartTimestamp` plus their (hash, timestamp) pairs, at most ten |
| 3 | `python3 --version; timeout --version \| head -n 1; bash --version \| head -n 1; for t in head cat sha256sum stat mktemp journalctl systemctl git pgrep ls rm; do if command -v $t >/dev/null; then echo TOOL $t ok; else echo TOOL $t missing; fi; done; journalctl --disk-usage; stat -c %a:%U:%F /tmp` | `Python 3.(1[0-9]\|[2-9][0-9])`, `GNU coreutils` in the timeout line, every `TOOL` line `ok`, one disk-usage line exported as-is (one figure), `/tmp` mode line `1777:root:directory` or `755:root:directory` |
| 4 | `d=$(mktemp -d /tmp/receipt_inventory.XXXXXXXXXX) && stat -c %n:%a:%U:%F $d` | one line `TMP:700:USER:directory` with `TMP` fullmatching the pattern in section 2; `TMP` is stored in `state.json` and is the only path ever substituted |
| 5, 6, 7 | `scp <options> <local script> USER@HOST:TMP/receipt_inventory_supervisor.py` (then reducer, then META) | `opNN.err` byte count recorded; the transfer is not trusted until op 8 |
| 8 | `cd TMP && sha256sum receipt_inventory_supervisor.py receipt_inventory_reducer.py receipt_inventory_meta.py && ls -A \| wc -l` | three lines whose digests equal the locally computed expected hashes, in order; entry count `3`. **No staged file executes before this gate passes.** |
| 9 | `timeout -k 5 45 python3 -I -S TMP/receipt_inventory_supervisor.py --pgid-file TMP/wrapper.pgid.1 -- timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c "{ git -C REPO rev-parse HEAD; systemctl show UNIT -p ActiveState -p StandardOutput; } \| head -c 4097 \| python3 -I -S TMP/receipt_inventory_meta.py REPO scout/main.py scout/outcome_ledger.py scout/trading/decision_events.py scout/trading/signals.py scout/trading/engine.py"` | envelope captured (exactly one LF-terminated line under the cap) or recorded missing |
| 10 | cleanup check for K=1 (string below) | `PROVEN` and `usable_envelope(rc, line, "meta")` true, else fatal |
| 11 | as op 9 with `--pgid-file TMP/wrapper.pgid.2` and pipeline `... \| python3 -I -S TMP/receipt_inventory_meta.py TMP receipt_inventory_supervisor.py receipt_inventory_reducer.py receipt_inventory_meta.py` | envelope captured |
| 12 | cleanup check K=2 | `PROVEN` and `usable_envelope(..., "meta")` true with three `OK` slots whose digests equal op 8's, else fatal |
| 13 | as op 9 with `wrapper.pgid.3` and pipeline `journalctl -u UNIT --until=@START_S -n 1 -o json --no-pager -q \| head -c 2097153 \| python3 -I -S TMP/receipt_inventory_reducer.py 0 START_US` | envelope captured |
| 14 | cleanup check K=3 | `PROVEN` required (fatal otherwise); floor result recorded per section 8, non-fatal either way |
| 15 | as op 9 with `wrapper.pgid.4` and pipeline `journalctl -u UNIT --since=@START_S --until=@END_S -n 200 -o json --no-pager -q \| head -c 2097153 \| python3 -I -S TMP/receipt_inventory_reducer.py START_US END_US` | envelope captured |
| 16 | cleanup check K=4 | `PROVEN` required; observation result recorded per section 8, non-fatal either way |
| 17 | `ls -A TMP; echo COUNT=$(ls -A TMP \| wc -l); for n in 1 2 3 4; do f=TMP/wrapper.pgid.$n; if [ -s $f ]; then g=$(cat $f); echo PGID $n $g; pgrep -g $g; echo PGREP_EXIT $n $?; else echo PGID $n MISSING; fi; done` | every listed name is in the permitted set; for every attempted K: `PGID K` integer greater than 1 equal to the captured envelope's `pgid`, no pid line, `PGREP_EXIT K 1`; for every unattempted K: `MISSING` |
| 18 | `rm -rf -- TMP && if [ ! -e TMP ]; then echo REMOVED; fi` | exactly `REMOVED`; rendered only when op 17 (or recovery) verdict is `ALL_PROVEN` |

Cleanup check string for K (ops 10, 12, 14, 16): `f=TMP/wrapper.pgid.K; if [ -s $f ]; then g=$(cat $f); echo PGID $g; pgrep -g $g; echo PGREP_EXIT $?; else echo PGID MISSING; fi; ls -A TMP \| wc -l`.

The permitted entry set is exactly: the three script names, `wrapper.pgid.1` to `.4`, `wrapper.pgid.1.tmp` to `.4.tmp`. Any other name at op 17 is fatal and the directory is retained.

## 6. Cleanup proof per attempted observation

For attempt K the evaluator combines `op(2K+7).out` (the envelope) with `op(2K+8).out` (the check):

1. Envelope present: exactly one LF-terminated line, parses with a duplicate-rejecting hook to exactly the supervisor's 17 `STATUS_KEYS`, `cleanup_proof is True`, `pgid` an int greater than 1.
2. Check present: `PGID g` with `g == envelope.pgid`, no pid line, `PGREP_EXIT 1`.
3. Both hold: `PROVEN`. A leader already gone is consistent with `PROVEN`.
4. Anything else, including `PGID MISSING` with a proof-bearing envelope, an empty group without an envelope, `PGREP_EXIT 0` (members remain, zombies included), an unparseable or oversize envelope, an `opNN.out` that does not exist, or an `ssh` exit of 255: `UNPROVEN`. `UNPROVEN` is fatal and stops the sequence before the next observation; the directory is retained.

Supervisor exit table (`EXIT_CODES` in `scripts/receipt_inventory_supervisor.py:39-50`) as the ssh exit code of an observation op: 0 `OK`, 3 `OUTPUT_OVERFLOW`, 4 `COMMAND_FAILED`, 5 `ORPHANS_SWEPT`, 6 `INNER_TIMEOUT`, 7 `OUTER_TIMEOUT`, 8 `INTERRUPTED`, 9 `CLEANUP_FAILED`, 10 `SUPERVISOR_ERROR`, 11 report incomplete, 124 or 137 host `timeout` fired, 255 ssh failure. Cleanup proof depends only on rule 1 to 4 above, so 3 to 8 and 10 can still be `PROVEN` while the observation is `OBSERVATION_UNAVAILABLE`; 9, 11, 124, 137 and 255 are always `UNPROVEN` because no validated report exists. Nothing is ever signalled from the Windows side, and no pgid integer is reused for any purpose other than the emptiness read.

## 7. Recovery, once

If any operation after op 4 ends without a readable `opNN.out` or with ssh exit 255, `render` offers exactly one recovery connection, `R`, whose remote string is op 17's. `evaluate R` quantifies over `attempts.jsonl`: for every attempted K it requires the captured envelope proof and the fresh emptiness read, exactly as section 6. Only if every attempted K is `PROVEN` and the listing is within the permitted set may op 18 be rendered; if no observation was attempted, permitted-content verification alone licenses op 18. Otherwise the verdict is `RETAINED` with the exact `TMP` and the pgids for operator disposition, the findings status is fatal, and no process is signalled and nothing is retried. `R` is the 19th connection at most; op 18 after it is the 20th.

## 8. Interpretation contract, encoded in `evaluate`

- D1 to D4 are written as `UNKNOWN` in every findings layout; no verdict can change them.
- Observation 3 positive floor: `usable_envelope(..., "reducer")` true, `observed_span.first` and `last` both strings, both parse to instants strictly before `START_US`. Otherwise "retention floor unproven". A positive floor means one retained pre-window record; the evaluator's fixed sentence never mentions coverage or continuity.
- Observation 4: only `usable_envelope` true with every `events[*]` zero yields the fixed sentence "no allowlisted event observed in the window at an unknown effective log level". Any other outcome is `OBSERVATION_UNAVAILABLE` with the oracle's fixed reason; counts from an unusable envelope are not reported.
- Identities are three separate blocks: current checkout (op 2 line 1, op 9 five digests), current process (op 2 `ExecMainPID`, `ExecMainStartTimestamp`, which may postdate the window), historical window producer (fixed text "window producer identity: not established" unless a later reviewed evidence source ties it to a revision; this design supplies none). The reflog export is recorded as correlation only.

## 9. Sequence module contract (`scripts/receipt_inventory_sequence.py`)

- Imports exactly `datetime`, `hashlib`, `json`, `os`, `re`, `sys`. No `subprocess`, `socket`, `shutil`, `urllib`, `ctypes`. It never spawns. Lints in `tests/test_round8_subprocess_timeouts.py` and `tests/test_datetime_hygiene.py` scan it; it uses `datetime.now(timezone.utc)` only.
- Pure functions: `remote_string(n, params) -> str`; `local_command(n, params, rundir) -> str` (PowerShell form with the two-step redirects); `evaluate_bytes(n, params, out: bytes, err_size: int, state) -> Verdict`. `Verdict` has fixed keys `op`, `status` (`PASS`, `FATAL`, `NONFATAL`, `PROVEN`, `UNPROVEN`, `ALL_PROVEN`, `RETAINED`, `REMOVED`), `reason` (fixed token), `exports` (sanitized values only), `next` (the only operation number `render` will accept next, or `null`).
- CLI: `render N` (checks `state.json` says `N` is next; for observation ops writes the attempt record first; prints the local command), `evaluate N` (reads `opNN.out` under the cap, `opNN.err` size, updates `state.json`, prints the verdict JSON), `findings` (renders the findings markdown from `state.json` with the fixed sentences and D1 to D4 `UNKNOWN`).
- Exports are the only strings that leave `RUNDIR` into the findings; raw `opNN.out` files are never copied into the repository.

## 10. Synthetic end-to-end exercise (`tests/test_receipt_inventory_sequence.py`, Linux only)

A `LocalExecutor` runs `remote_string(n)` under `bash -c` with stdout redirected to `RUNDIR/opNN.out` and stderr to `opNN.err` (the same two-step shape), and implements the `scp` ops as file copies into the synthetic `TMP`. The synthetic target is a temporary tree: a real git repository as `REPO` containing copies of the five real source files; a `bin/` directory placed first on `PATH` holding stub executables `hostname` (prints the test's `HOST`), `id`-free (real `id`), `systemctl` (prints the four fixed properties), and `journalctl` (emits fixture records from a file chosen per scenario, honouring `-n`, `--since=@`, `--until=@` by filtering the fixture's `__REALTIME_TIMESTAMP`); real `mktemp`, `sha256sum`, `stat`, `pgrep`, `timeout`, `bash`, `python3`. The test drives the identical 18 operations through `evaluate_bytes` and asserts every gate. Scenarios, each a separate test method, each asserting the exact verdicts and that no later operation string is executed:

1. Happy path: all `PASS`/`PROVEN`, op 17 `ALL_PROVEN`, op 18 `REMOVED`, `TMP` absent, findings render with D1 to D4 `UNKNOWN`.
2. Failure before publication: `TMP` made read-only after op 8, so the supervisor's `publish` fails (`error=publish`, exit 10); op 10 sees `PGID MISSING` and returns `UNPROVEN`; the sequence stops; recovery `R` returns `RETAINED`; the test restores the mode and asserts the retained directory still holds only permitted names.
3. Disconnect: the executor deletes `op13.out` after running op 13 (ambiguous transport); op 14 is refused; `R` finds `attempts.jsonl` has K=3 without a validated envelope and returns `RETAINED` even though `pgrep` reads empty.
4. Oversize: the executor appends 131,073 bytes to `op15.out`; `evaluate 15` returns `FATAL OVERSIZE`, nothing parsed, cleanup path continues to `R`.
5. Hash mismatch: the executor stages a one-byte-different reducer; op 8 returns `FATAL HASH_MISMATCH`; assert no string for ops 9 to 16 was ever executed; op 18 is licensed by permitted-content verification and removes `TMP`.
6. Dirty checkout: an untracked file in the synthetic `REPO`; op 2 returns `FATAL DIRTY_CHECKOUT`; assert ops 4 and later never ran and no `mktemp` occurred.
7. Non-fatal observation: the `journalctl` stub exits 1 for op 15; supervisor `COMMAND_FAILED` with `cleanup_proof` true; op 16 `PROVEN`, observation `OBSERVATION_UNAVAILABLE`; op 17 `ALL_PROVEN`, op 18 `REMOVED`.
8. Floor semantics: fixtures with a pre-window record, with a record exactly at `START_S` (returned by inclusive `--until`, reported `out_of_window`, floor unproven), and with a malformed pre-window record (floor unproven).

Unit tests on the module alone, cross-platform: no single quote in any rendered string; `START_US`/`END_US` derivation; every exit-code row of section 6 through `evaluate_bytes` with synthetic files, including exit 11 and 255 as `UNPROVEN`; `render` refuses a second attempt and a 20th connection; the permitted-name check; the `TMP` pattern gate.

## 11. Stops and residuals

Fatal stops and non-fatal outcomes are exactly the plan's list, with `OVERSIZE`, `HASH_MISMATCH`, `DIRTY_CHECKOUT`, `TMP_PATTERN`, `UNPROVEN` and `RETAINED` as the evaluator's fixed reasons. If op 3 reports `pgrep` missing, the sequence stops before any write with the residual "no independent group-emptiness primitive on the target"; this design does not substitute a `/proc` scan or any other mechanism. If op 1 reports a different hostname, the run stops and the alias configuration is investigated separately.

## 12. Files, gates and rollback

Files after design approval: `scripts/receipt_inventory_sequence.py`, `tests/test_receipt_inventory_sequence.py`, later `tasks/findings_receipt_inventory_<date>.md`. Nothing else changes. Gates in order: two independent design reviews on this file; PR592 merged on exact-head green CI; build with TDD; two PR reviews and Linux CI green including the synthetic exercise; then one production sequence; then the findings PR with two reviews and a clearance file. Rollback of the code is a revert; rollback on the host is op 18 or operator disposition of one retained directory whose exact path and pgids are in the findings.
