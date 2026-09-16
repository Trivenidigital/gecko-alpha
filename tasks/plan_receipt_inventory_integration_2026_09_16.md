**New primitives introduced:** NONE. This plan reuses the merged supervisor (`scripts/receipt_inventory_supervisor.py`), the reducer and META from PR592 (`scripts/receipt_inventory_reducer.py`, `scripts/receipt_inventory_meta.py`), and the `usable_envelope` oracle in `tests/test_receipt_inventory_envelope.py`. It proposes no collector, API, script, supervisor change, workflow change, dependency, schema, secret or writer. Contingent on PR592 merging on exact-head green Linux CI; nothing here runs before that. Revision 2 folds both plan reviews of f5a356a7; folds are marked **F1** to **F10**.

# Plan: production read-only receipt inventory integration

## Hermes-first analysis

| Domain | Checked (coordinator, 2026-09-16 19:30 UTC) | Limited verdict |
|---|---|---|
| Journal reduction, host metadata validation | https://hermes-agent.nousresearch.com/docs/skills : catalog still Loading; no verified matching skill | No skill verified; not an exhaustive absence claim |
| Ecosystem | https://github.com/0xNyk/awesome-hermes-agent : general orchestration tools; no verified Gecko receipt-inventory replacement | Not a replacement |
| Process ownership, reduction, validation, usability rule | In-repo primitives above, merged or pending in PR592 | Reuse unchanged |

Verdict: the residual is integration prose plus one findings file. No custom code.

## Scope in one sentence

One bounded SSH session that stages three already-reviewed scripts into one private temp directory, independently verifies all three by hash before anything executes, runs four supervised read-only commands, verifies every result locally with `usable_envelope`, records sanitized findings under `tasks/`, and removes the temp directory under a stated cleanup policy.

## Not in scope, stated once

No D5 or D6 analysis, no ranking, no collection retry, no window widening, no second collection session, no DB, config, env, settings or repo write, no raw journal or MESSAGE text leaving the host, no secret reads, no deployment, no supervisor or script edit. One recovery-only reconnection is permitted under section "Recovery connection" and nothing else. If any other item becomes necessary, this plan is amended and re-reviewed.

## Session protocol (F3)

The design must make one session realizable, not assumed:

- One SSH connection, one remote non-interactive shell, commands delivered as a fixed numbered sequence on the shell's stdin. Because stdin carries both commands and staged script bytes, each staged file is delivered as a length-prefixed block read by `head -c N` into the target file, followed by a host `sha256sum` of that file. No heredoc delimiter is relied on for framing; the byte count is the frame.
- Every step ends with exactly one control acknowledgement line of a fixed form, `ACK <step> <exit>`, so the step's exit code is transported explicitly. A missing, malformed or duplicated acknowledgement is a protocol stop (fatal, section "Stops").
- Byte limits: each supervisor envelope at most 131,073 bytes; each control line at most 4,096 bytes; each staged file under 32 KiB; total staged bytes under 40 KB. Time limits: each supervised step at most T0 plus 15.5 seconds plus interpreter start; each control step at most 5 seconds; whole session at most 5 minutes wall clock, after which only cleanup runs.
- The design must synthesize an end-to-end transport test on Linux CI that drives the exact stdin stream through a stand-in shell (local `bash` reading the same stream, or a loopback sshd where available), staging real files, checking hashes and parsing acknowledgements. A `shlex` parse of pipelines is not sufficient and is not the test.
- No implementation of the stream exists yet; this plan authorizes designing it, not writing it.

## Host assumptions (enumerated, checked read-only before any write)

Linux; GNU coreutils `timeout`, `head`, `cat`, `sha256sum`, `stat`, `mktemp`; `bash`, `journalctl`, `systemctl`, `git`; `python3` at least 3.10 with `-I -S` usable; `prctl` subreaper permitted for the invoking user; journald `-o json` places the structlog JSON in `MESSAGE` as a string; the host's default temp root exists and is a directory. Nothing is passed as `-c` source, so no per-argument limit is relied on.

## Read-only preflight, before any write (F4, F10)

Only sanitized, bounded values leave the host in this phase. Each control below names its exact export; everything else is discarded on the host.

| Control | Export | Bound |
|---|---|---|
| P1 host identity | `hostname` compared locally to the intended name; export is `MATCH` or `MISMATCH` | fixed token |
| P2 current checkout | `git rev-parse HEAD` as 40-hex; porcelain cleanliness as a line count only, never filenames | 40 hex, one integer |
| P3 current process | `ExecMainPID`, `ExecMainStartTimestamp`, `ActiveState`, `StandardOutput` from `systemctl show` | integers, one timestamp, two set members |
| P4 checkout history | reflog entries newer than P3's start: count, each 40-hex SHA and timestamp; never subjects or messages | integer plus (hex, timestamp) pairs, at most 10 |
| P5 toolchain | versions of `python3`, `timeout`, `bash` matched against fixed patterns; presence of the other tools | fixed tokens |
| P6 retention metadata | `journalctl --disk-usage` figure; no journal record of any kind is read before staging | one size figure |
| P7 temp root | existence, directory type and mode of the default temp root via `stat`; no `mktemp` in this phase | one mode |

The sanitized retention probe (command 3) runs only after staging because the reducer must be present to sanitize it; a raw pre-window record is never exported.

## Licensed write phase, in order (F1, F2)

The old no-host-write rule in `plan_suppression_receipt_inventory_2026_09_14.md:21` stands except for exactly this licence, which the design states verbatim:

1. `mktemp -d` under the default temp root, mode 0700, owned by the invoking user. Mode and ownership are verified here, immediately after creation and before any staging, by `stat`; a mismatch is fatal and the directory is removed.
2. Stage the three scripts by length-prefixed blocks. After all three are staged and **before any of them executes**, the host reports `sha256sum` of all three files. All three are verified locally against the values recorded from the merged PR592 master SHA. Only after all three match does any execution begin. No script is trusted on the strength of another script's report: the supervisor is not run to check META, and META's self-report of the scripts (command 2) is a second, later confirmation, never the first.
3. Contents permitted in the directory for the whole session: the three staged scripts; up to four final pgid files `wrapper.pgid.1` to `.4`; up to four publish remnants `wrapper.pgid.N.tmp` that the supervisor's temp-then-rename publish can leave behind on a failed rename. Nothing else, ever. Nothing is written anywhere else on the host: not the repository, `/etc`, unit files, env files, caches or the journal.
4. Cleanup policy: list the directory; the entry set must be a subset of the permitted names above, and the count is the only export; any other entry is fatal and the directory is retained. For every published group (each `wrapper.pgid.N` that exists), verify emptiness under the recovery rules below before removal. Remove the exact `mktemp` path only, then verify it is absent. Removal is recorded as `REMOVED`, `RETAINED` with a fixed reason, or `UNPROVEN`.

Reversible temp staging under standing permissions needs no operator approval. It does need this plan's two reviews and the design's two reviews; that is not an approval bypass.

## The four supervised commands

Every command runs as `python3 -I -S TMP/receipt_inventory_supervisor.py --pgid-file TMP/wrapper.pgid.N -- timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c PIPELINE`. Each has its own pgid file. Stdout is one captured line; stderr is discarded on the host.

1. **META, repository root.** `{ git -C REPO rev-parse HEAD; systemctl show UNIT -p ActiveState -p StandardOutput; } | head -c 4097 | python3 -I -S TMP/receipt_inventory_meta.py REPO scout/main.py scout/outcome_ledger.py scout/trading/decision_events.py scout/trading/signals.py scout/trading/engine.py`. Five slots pin the current checkout's event-site sources, including the four extra `ledger_record_failed` sites.
2. **META, temp root.** Same metadata pipeline, `python3 -I -S TMP/receipt_inventory_meta.py TMP receipt_inventory_supervisor.py receipt_inventory_reducer.py receipt_inventory_meta.py`. Three slots confirm the staged scripts a second time. Two META runs are required because META accepts one root; scripts are never copied under the repository.
3. **Retention floor probe (F7).** `journalctl -u UNIT --until "WINDOW_START" -n 1 -o json --no-pager -q | head -c 2097153 | python3 -I -S TMP/receipt_inventory_reducer.py 0 START_US`. `--until` is inclusive, so an entry at exactly WINDOW_START may be returned and will be `out_of_window` under the reducer's half-open window. A **positive floor** requires all of: a usable envelope; `observed_span.first` and `observed_span.last` both non-null; and both strictly before START_US when parsed. `records == 1` with `out_of_window == 0` is not sufficient, because a malformed record satisfies it. A positive floor proves exactly one retained pre-window record and nothing about continuity across the window; the findings never say the retained span covers the window.
4. **Main reducer.** `journalctl -u UNIT --since "WINDOW_START" --until "WINDOW_END" -n 200 -o json --no-pager -q | head -c 2097153 | python3 -I -S TMP/receipt_inventory_reducer.py START_US END_US`. Run once, whatever command 3 showed.

Concrete REPO, UNIT, WINDOW_START and WINDOW_END come from the design; the scripts contain none of them (enforced by their static tests).

## Interpretation contract (F8, F9)

- D1 to D4 stay `UNKNOWN` whatever the counts show, per `design_suppression_receipt_inventory_2026_09_14.md`. Counts are positive presence evidence only.
- Only a **usable** command 4 envelope with zero allowlisted event counts supports the sentence "no allowlisted event observed in the window at an unknown effective log level". An unusable, truncated, saturated-and-capped, `OVERFLOW`, `RECORD_CAP`, `COMMAND_FAILED` or missing result is recorded as `OBSERVATION_UNAVAILABLE` with the fixed reason, and supports no sentence about the window.
- Three identities are reported separately and never merged: **current checkout** (P2 plus command 1 hashes); **current process** (P3, which may postdate the window entirely); **historical window producer** (the process that wrote the window's journal entries, whose identity this plan does not establish). Reflog and process start time are correlation, not proof, of what any process loaded; the findings do not prescribe an inferred loaded identity. If no evidence ties the window producer to a revision, the findings state "window producer identity: not established".

## Recovery connection (F5)

If the session is lost after the write phase began, exactly one reconnection is permitted, for cleanup and evidence retrieval only. It runs no collection command and no retry. Signalling is permitted only when ownership of a group is verified independently: the published integer in `wrapper.pgid.N` alone, or a `.tmp` remnant alone, licenses nothing. Verification requires, for the candidate pgid, that `/proc/<pgid>/stat` shows session id and process group id equal to the pgid, that its start time falls inside this session's window, and that its command line matches the supervisor or wrapper shape. If any check fails or the process is gone, nothing is signalled, the directory is retained, and cleanup is recorded `UNPROVEN`. The recovery connection exports only the same sanitized controls as cleanup.

## Stops (F6)

**Fatal** stops end collection; cleanup is still attempted, and the findings status is the stop reason: P1 mismatch; P2 revision unexpected; P3 unit not active or output not journal; P5 assumption failing; temp directory mode or ownership mismatch; any staged hash mismatch; protocol acknowledgement failure; command 1 or 2 envelope unusable; an unexpected entry in the temp directory; cleanup `UNPROVEN` or `RETAINED`. Unresolved cleanup is always fatal for the findings status, even when every envelope was usable.

**Non-fatal** evidence outcomes are recorded and the session continues in order: floor probe unusable, empty or invalid (recorded "retention floor unproven"); command 4 unusable (recorded `OBSERVATION_UNAVAILABLE`). Neither triggers a retry.

## Obligations for the design

- Realizable session protocol per section "Session protocol", with the end-to-end transport test.
- Bounded outer SSH loss: what the caller knows at each step, how the temp directory and any live group are found on the recovery connection, and the hard cap of one recovery connection.
- Pgid file identity per the supervisor's lemmas; four distinct names so no run reads another's.
- Evidence preservation: never delete the directory while a published group may be live; removal only after emptiness is verified or explicitly `UNPROVEN` with retention.
- Independent bootstrap hash of all three scripts before any execution.
- No secrets, no raw journal text, no reflog subjects, no porcelain filenames; the only host exports are the P1 to P7 controls, the four envelopes, the staged hashes and the cleanup controls.
- Exact host values, the supervisor exit table against `EXIT_CODES`, the findings file layout, and the fixed uncertainty statements above.

## Tests, smokes and rollback

Local, before the session: PR592 modules green on the merged SHA; the transport test from the design green on Linux CI; `usable_envelope` run against synthetic envelopes for each planned schema. On host, before writes: P1 to P7. After staging: three bootstrap hashes. After each command: local `usable_envelope`. Rollback is the cleanup policy; nothing else changes on the host. The findings PR is docs-only and reverts cleanly.

## Gates

Two independent parallel plan reviews on this file; separate design with two reviews resolving every obligation above; PR592 merged on exact-head green CI; then one session; then a findings PR with two reviews and a clearance file. Passing this plan's reviews authorizes design work only.
