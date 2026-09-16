**New primitives introduced:** no new collector, supervisor, API, transport coordinator, dependency, schema, secret or persistent writer. Two **candidate disposable artifacts** are named provisionally and are not authorized by this plan: a fixed-sequence driver `scripts/receipt_inventory_sequence.py` that runs the standard `ssh`/`scp` operation table below with per-operation limits, and its synthetic exercise `tests/test_receipt_inventory_sequence.py`. Whether they are built, or the sequence is executed operation by operation from a checklist, is a design decision requiring two design reviews before any build. This plan reuses the merged supervisor (`scripts/receipt_inventory_supervisor.py`), the reducer and META from PR592 (`scripts/receipt_inventory_reducer.py`, `scripts/receipt_inventory_meta.py`), and the `usable_envelope` oracle in `tests/test_receipt_inventory_envelope.py`. Contingent on PR592 merging on exact-head green Linux CI. Revision 3 folds both plan reviews of 446d91af.

# Plan: production read-only receipt inventory integration

## Hermes-first analysis

| Domain | Checked (coordinator, 2026-09-16 19:30 UTC) | Limited verdict |
|---|---|---|
| Journal reduction, host metadata validation | https://hermes-agent.nousresearch.com/docs/skills : catalog still Loading; no verified matching skill | No skill verified; not an exhaustive absence claim |
| Ecosystem | https://github.com/0xNyk/awesome-hermes-agent : general orchestration tools; no verified Gecko receipt-inventory replacement | Not a replacement |
| Remote execution and file staging | Stock OpenSSH `ssh` and `scp` with local `timeout` and byte caps | Reuse standard tools; no custom protocol |
| Process ownership, reduction, validation, usability rule | In-repo primitives above, merged or pending in PR592 | Reuse unchanged |

Verdict: the residual is a fixed sequence of standard operations plus one findings file, and possibly one disposable driver with its synthetic test, scoped by the design.

## Scope in one sentence

A fixed, finite sequence of standard `ssh` and `scp` operations that stages three already-reviewed scripts into one private temp directory, verifies all three by hash before anything executes, runs four supervised read-only observations with an independent cleanup check gating each next one, verifies every envelope locally with `usable_envelope`, records sanitized findings under `tasks/`, and removes the temp directory only when every group's cleanup is proven.

## Not in scope, stated once

No D5 or D6 analysis, no ranking, no collection retry, no window widening, no DB, config, env, settings or repo write, no raw journal or MESSAGE text leaving the host, no secret reads, no deployment, no supervisor or script edit, no custom stdin framing or acknowledgement protocol, no persistent transport coordinator. One recovery-only connection is permitted under "Recovery" and nothing else.

## Operation model

- Every operation is one fresh `ssh` or `scp` invocation to the explicit hostname (never an alias), with multiplexing disabled (`ControlMaster=no`, `ControlPath=none`), `BatchMode=yes`, a connect timeout, and a local `timeout` around the whole invocation. No remote shell session persists between operations.
- The existing SSH output discipline holds: every operation's stdout is captured to a bounded local artifact first, then parsed and gated in a separate local step. Nothing is interpreted from a live stream.
- Per-operation limits: control operations 10 s and 4,096 output bytes; `scp` operations 30 s and one file under 32 KiB; supervised operations 40 s (T0 plus 15.5 s plus interpreter and connection) and 131,073 output bytes, the supervisor line cap plus one sentinel byte. Any operation exceeding a limit is a fatal stop.
- Finite connections: at most 18 sequence operations plus 1 recovery connection; hard cap 20. Nothing is repeated.

## Exact sequence (18 operations)

| Op | Tool | Purpose | Local gate before the next op |
|---|---|---|---|
| 1 | ssh | P1 `hostname` | equals intended host |
| 2 | ssh | P2 `git rev-parse HEAD`, porcelain line count; P3 `systemctl show` fields; P4 reflog SHAs and timestamps only | revision expected; **porcelain count is 0** (nonzero is fatal); unit active, output journal |
| 3 | ssh | P5 tool versions and presence; P6 `journalctl --disk-usage`; P7 temp-root `stat` | assumptions hold |
| 4 | ssh | `mktemp -d` under the default temp root, then `stat` of it | path exported once; mode 0700 and owner uid match |
| 5 to 7 | scp | stage supervisor, reducer, META, one file each | transfer exit 0 |
| 8 | ssh | `sha256sum` of all three staged files | all three equal the values recorded from merged PR592 master; **no staged file executes before this gate** |
| 9 | ssh | observation 1: META, repository root (five source slots) | envelope captured |
| 10 | ssh | cleanup check for observation 1 | cleanup PROVEN (see below) and `usable_envelope` true |
| 11 | ssh | observation 2: META, temp root (three script slots) | envelope captured |
| 12 | ssh | cleanup check for observation 2 | cleanup PROVEN and `usable_envelope` true |
| 13 | ssh | observation 3: retention floor probe | envelope captured |
| 14 | ssh | cleanup check for observation 3 | cleanup PROVEN; floor result recorded, non-fatal either way |
| 15 | ssh | observation 4: main reducer | envelope captured |
| 16 | ssh | cleanup check for observation 4 | cleanup PROVEN; envelope result recorded, non-fatal either way |
| 17 | ssh | directory listing: names must be a subset of the permitted set, count exported; re-check every published group empty | all groups PROVEN |
| 18 | ssh | remove the exact `mktemp` path, verify absent | `REMOVED` |

Observation commands are unchanged from revision 2: each is `python3 -I -S TMP/receipt_inventory_supervisor.py --pgid-file TMP/wrapper.pgid.N -- timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c PIPELINE`, with the four pipelines (META repo root with `scout/main.py`, `scout/outcome_ledger.py`, `scout/trading/decision_events.py`, `scout/trading/signals.py`, `scout/trading/engine.py`; META temp root with the three scripts; `journalctl -u UNIT --until WINDOW_START -n 1 -o json --no-pager -q | head -c 2097153 | reducer 0 START_US`; `journalctl -u UNIT --since WINDOW_START --until WINDOW_END -n 200 -o json --no-pager -q | head -c 2097153 | reducer START_US END_US`). Concrete REPO, UNIT and window values come from the design; the scripts contain none of them.

## Cleanup proof per observation

A cleanup check (ops 10, 12, 14, 16) reads `wrapper.pgid.N`, requires an integer greater than 1, and runs an independent emptiness check on that group (`pgrep -g G` returning no members; zombies count as members). Cleanup is **PROVEN** only when both hold: the captured envelope parses with the fixed 17 keys and `cleanup_proof` is true with the same `pgid`, and the independent check shows the group empty. A leader that is already gone is consistent with PROVEN under these two conditions. Any other combination, including a missing or unparseable envelope with an empty group, is **UNPROVEN**. UNPROVEN is fatal and stops the sequence immediately, before the next observation; in particular no cleanup uncertainty from observations 1 to 3 may be carried into observation 4. Reuse of the integer G by an unrelated process can only make a check read non-empty, which fails safe toward UNPROVEN; nothing is ever signalled on the strength of that integer.

## Read-only preflight exports (before any write)

Only these sanitized values leave the host: P1 match token; P2 40-hex HEAD and one porcelain line count, never filenames; P3 `ExecMainPID`, `ExecMainStartTimestamp`, `ActiveState`, `StandardOutput`; P4 count and at most ten (40-hex, timestamp) reflog pairs newer than the process start, never subjects; P5 fixed tokens; P6 one disk-usage figure; P7 one mode. No journal record of any kind is read before staging; the sanitized floor probe runs only after the reducer is staged and verified.

## Licensed write phase

The old no-host-write rule in `plan_suppression_receipt_inventory_2026_09_14.md:21` stands except for exactly this licence, which the design states verbatim: one `mktemp -d` directory under the default temp root, mode 0700, owned by the invoking user, verified by `stat` immediately after creation; permitted contents for the whole session are the three staged scripts, up to four final pgid files `wrapper.pgid.1` to `.4`, and up to four publish remnants `wrapper.pgid.N.tmp`; total staged bytes under 40 KB; nothing written anywhere else on the host. Removal happens at op 18 only after op 17 proves every published group empty, and is recorded `REMOVED`, `RETAINED` with a fixed reason, or `UNPROVEN`. Reversible temp staging under standing permissions needs no operator approval; it needs this plan's two reviews and the design's two reviews.

## Recovery

If a connection is lost after op 4, exactly one recovery connection is permitted. It is read-only: it lists the directory, reads the pgid files, and runs the emptiness check on each published group. It never signals any process, because no pinned start-tick association exists for these groups and a start-time or command-shape heuristic is not an identity. If every published group is empty and every corresponding envelope was captured with `cleanup_proof` true, the directory may be removed and cleanup recorded `REMOVED`; otherwise the directory is retained, cleanup is `UNPROVEN`, the findings status is fatal, and the exact path and pgids are reported for operator disposition. Live group members found this way are evidence and are left running. No collection command runs on the recovery connection.

## Interpretation contract

- D1 to D4 stay `UNKNOWN` whatever the counts show, per `design_suppression_receipt_inventory_2026_09_14.md`. Counts are positive presence evidence only.
- Positive retention floor requires a usable observation 3 envelope with `observed_span.first` and `last` both non-null and both strictly before START_US when parsed; `records` 1 with `out_of_window` 0 is insufficient because a malformed record satisfies it; `--until` is inclusive. A positive floor proves one retained pre-window record and nothing about continuity across the window.
- Only a usable observation 4 envelope with zero allowlisted event counts supports "no allowlisted event observed in the window at an unknown effective log level". Anything unusable, truncated, capped or missing is `OBSERVATION_UNAVAILABLE` and supports no sentence about the window.
- Current checkout (P2 plus observation 1 hashes), current process (P3, which may postdate the window), and historical window producer are reported separately. Reflog and start time are correlation, not proof, of loaded code; when no evidence ties the window producer to a revision the findings say "window producer identity: not established".

## Stops

Fatal, ending collection and proceeding only to cleanup: any per-operation limit exceeded; P1 mismatch; P2 revision unexpected or porcelain count nonzero; P3 unit not active or output not journal; P5 assumption failing; temp directory mode or owner mismatch; any staged hash mismatch; observation 1 or 2 envelope unusable; any cleanup UNPROVEN; an unexpected entry in the directory; final cleanup `RETAINED` or `UNPROVEN`. Non-fatal, recorded and continued: floor probe unusable, empty or invalid; observation 4 unusable. Nothing triggers a retry.

## Synthetic exercise before production

The exact operation table, with the exact remote command strings and the same local gates, must be exercised end to end on Linux CI against a synthetic target before any production run: real staging of the three real scripts into a temp directory, real supervised execution of the four pipeline shapes against fixture data, real cleanup checks and real removal, with `ssh`/`scp` replaced by a local executor that runs the identical remote strings under `bash` on the same machine. This is where the design must be honest: executing 18 gated operations reproducibly requires either the disposable driver named in the header or a manually followed checklist, and the synthetic exercise requires a test module. If the design chooses the driver, it scopes the two filenames, their import and spawn contracts, and their tests, and receives two design reviews before any build. If the design finds that standard tools cannot satisfy the per-observation cleanup gate on the target (for example no `pgrep`), it stops and states that exact residual rather than adding mechanism.

## Obligations for the design

Exact remote command strings for all 18 operations; the executor abstraction for the synthetic exercise; the supervisor exit table against `EXIT_CODES`; the permitted-entry set and cleanup states; the findings file layout with the fixed uncertainty statements; the choice between driver and checklist with its artifact names; concrete REPO, UNIT and window values.

## Tests, smokes and rollback

Local before the session: PR592 modules green on the merged SHA; the synthetic exercise green on Linux CI; `usable_envelope` against synthetic envelopes for both schemas. On host: ops 1 to 3 before any write; op 8 before any execution; cleanup checks between observations. Rollback is op 18 or, on failure, operator disposition of one retained temp directory; nothing else changes on the host. The findings PR is docs-only and reverts cleanly.

## Gates

Two independent parallel plan reviews on this revision; separate design with two reviews resolving every obligation and scoping any disposable artifact; PR592 merged on exact-head green CI; synthetic exercise green; then one sequence run; then a findings PR with two reviews and a clearance file. Passing this plan's reviews authorizes design work only.
