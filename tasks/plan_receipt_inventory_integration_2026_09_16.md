**New primitives introduced:** NONE. This plan reuses the merged supervisor (`scripts/receipt_inventory_supervisor.py`), the reducer and META from PR592 (`scripts/receipt_inventory_reducer.py`, `scripts/receipt_inventory_meta.py`), and the `usable_envelope` oracle in `tests/test_receipt_inventory_envelope.py`. It proposes no collector, API, script, supervisor change, workflow change, dependency, schema, secret or writer. Contingent on PR592 merging on exact-head green Linux CI; nothing here runs before that.

# Plan: production read-only receipt inventory integration

## Hermes-first analysis

| Domain | Checked (coordinator, 2026-09-16) | Limited verdict |
|---|---|---|
| Journal reduction, host metadata validation | https://hermes-agent.nousresearch.com/docs/skills : catalog still Loading; no verified matching skill | No skill verified; not an exhaustive absence claim |
| Ecosystem | https://github.com/0xNyk/awesome-hermes-agent : general orchestration tools; no verified Gecko receipt-inventory replacement | Not a replacement |
| Process ownership, reduction, validation, usability rule | In-repo primitives above, merged or pending in PR592 | Reuse unchanged |

Verdict: the residual is integration prose plus one findings file. No custom code.

## Scope in one sentence

One bounded SSH session that stages three already-reviewed scripts into one private temp directory, runs four supervised read-only commands, verifies every result locally with `usable_envelope`, records sanitized findings under `tasks/`, and removes the temp directory.

## Not in scope, stated once

No D5 or D6 analysis, no ranking, no retries, no window widening, no second session to "get a better result", no DB, config, env, settings or repo write, no raw journal or MESSAGE text leaving the host, no secret reads, no deployment, no supervisor or script edit. If any of these becomes necessary, this plan is amended and re-reviewed.

## The four commands, each under the supervisor

Every command runs as `python3 -I -S TMP/receipt_inventory_supervisor.py --pgid-file TMP/wrapper.pgid.N -- timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c PIPELINE`, with the supervisor's report deadline at T0 plus 15.5 seconds plus interpreter start. Each command has its own pgid file. Stdout is captured locally as one line; stderr is discarded on the host.

1. **META, repository root.** `{ git -C REPO rev-parse HEAD; systemctl show UNIT -p ActiveState -p StandardOutput; } | head -c 4097 | python3 -I -S TMP/receipt_inventory_meta.py REPO scout/main.py scout/outcome_ledger.py scout/trading/decision_events.py scout/trading/signals.py scout/trading/engine.py`. Five slots pin the event-site sources, including the four extra `ledger_record_failed` sites.
2. **META, temp root.** Same metadata pipeline, `python3 -I -S TMP/receipt_inventory_meta.py TMP receipt_inventory_supervisor.py receipt_inventory_reducer.py receipt_inventory_meta.py`. Three slots pin the staged scripts. Two META runs are required because META accepts one root; the scripts are never copied under the repository.
3. **Retention floor probe.** `journalctl -u UNIT --until "WINDOW_START" -n 1 -o json --no-pager -q | head -c 2097153 | python3 -I -S TMP/receipt_inventory_reducer.py 0 START_US`. Expected `OK` with `records` 1 and `out_of_window` 0. This proves exactly one retained record before the window exists. It does not prove continuous retention across the window, and the findings must never say the retained span covers the window.
4. **Main reducer.** `journalctl -u UNIT --since "WINDOW_START" --until "WINDOW_END" -n 200 -o json --no-pager -q | head -c 2097153 | python3 -I -S TMP/receipt_inventory_reducer.py START_US END_US`.

Concrete values for REPO, UNIT, WINDOW_START and WINDOW_END come from the design, not from this plan; the scripts themselves contain none of them (enforced by their static tests).

## Interpretation contract, fixed now

- D1 to D4 stay `UNKNOWN` whatever the counts show, per the approved interpretation in `design_suppression_receipt_inventory_2026_09_14.md`. Counts are positive presence evidence only. A null main result plus a passing floor probe reads as "one pre-window record retained; no allowlisted event observed in the window at an unknown effective log level" and nothing stronger.
- A usable envelope requires supervisor return code 0, the fixed 17-key line, `dropped` 0, decoded inner `OK`, and the schema-specific invariants already coded in `usable_envelope`. Anything else is `UNKNOWN` with the fixed reason recorded. The caller never inspects `output` of a non-`OK` supervisor status.
- Supervisor exits 0 to 11 replace the old `OUTER_TIMEOUT` vocabulary: 6 is `INNER_TIMEOUT` (wrapper exit 124, -9 or 137), 7 is `OUTER_TIMEOUT` (leader alive at T0 plus 13), 11 is an incomplete report write. The design writes the full table against `EXIT_CODES` in the supervisor source.

## Provenance uncertainty, stated honestly

Checkout HEAD, `ExecMainStartTimestamp`, `ExecMainPID` and the host reflog together bound which revision was checked out when the pipeline process started. They do not prove which bytes the running interpreter loaded: the process could have been started from a different working tree, a bytecode cache, or an in-place edit later reverted. Source hashes from META run 1 pin the checkout now, not the process. The findings must therefore carry a fixed statement: "running-code identity inferred from checkout history and process start time; not verified from the process". No further inference is licensed.

## Host assumptions to enumerate in the design

Linux with GNU coreutils `timeout`, `bash`, `head`, `cat`, `journalctl`, `systemctl`, `git`, `mktemp`, `sha256sum`; `python3` at least 3.10 with `-I -S` usable; `prctl` subreaper permitted for the invoking user; journald `-o json` places the structlog JSON in `MESSAGE` as a string; a per-argument limit above 128 KiB is not relied on because nothing is passed as `-c` source. Each assumption is checked read-only in the preflight before any write.

## The one private temp directory licence

The old no-host-write rule in `plan_suppression_receipt_inventory_2026_09_14.md:21` stands except for exactly this reviewed licence, which the design must state verbatim: one directory from `mktemp -d` under the host's default temp root, mode 0700, owned by the invoking user, holding only the three staged scripts and up to four pgid files; total staged bytes under 40 KB; nothing written anywhere else on the host, including the repository, `/etc`, unit files, env files, caches or the journal; the directory removed by the session's final step, and its removal recorded. Reversible temp staging under standing permissions needs no operator approval, but it does need this plan's two reviews and the design's two reviews. Nothing in this licence extends to any other write.

## Transport and bootstrap identity

Scripts are sent over SSH stdin into the temp directory, one file per transfer. Before transport the local sha256 of each file at the merged PR592 master SHA is recorded. After transport and before any META result is trusted, the host reports `sha256sum` of the staged META file, verified locally against the recorded value. Only then is META run 2's self-report of the three scripts accepted, and run 1's report of the five sources. A mismatch anywhere is a stop.

## Stop conditions, in order

Host identity mismatch; checkout not at the expected revision or porcelain not clean; unit not active or `StandardOutput` not `journal`; toolchain assumption failing; staged META hash mismatch; any envelope not usable in commands 1 or 2; floor probe unusable or `records` not 1 (the main reducer still runs, the finding is marked "retention floor unproven"); any supervisor status other than `OK` on command 4. A stop records the fixed reason and proceeds only to cleanup.

## Obligations for the design

- Bounded outer SSH loss: if the SSH session drops, what the caller knows, how the temp directory and any live group are found and recovered on the next connection, and a hard cap on recovery attempts.
- Pgid file identity: the consumer verifies the published group against the supervisor's lemma rather than trusting the file, and the four files are named so no run can read another's.
- Evidence preservation: never delete a temp directory while a published group may still be live; cleanup waits for the supervisor's cleanup proof or the recovery step.
- No secrets, no raw journal text: the only bytes leaving the host are the four supervisor lines, whose inner payloads are fixed-schema by construction.
- Independent bootstrap hash of META before any self-report is trusted (above).
- Exact host values, exit table, findings file layout and the fixed uncertainty statements.

## Read-only preflight questions, answered before any write

1. Does `hostname` on a fresh non-multiplexed connection match the intended target?
2. Is `git rev-parse HEAD` the expected revision, and is porcelain status empty?
3. What are `ExecMainPID` and `ExecMainStartTimestamp`, and does `git reflog --date=iso` show any checkout change after that start?
4. Are `ActiveState` `active` and `StandardOutput` `journal`?
5. Does one unit entry exist before WINDOW_START, and what does `journalctl --disk-usage` report?
6. Are `python3 -I -S` version, `timeout --version` (GNU), and the other tools present?
7. Is the temp root writable by the invoking user with a 0700 `mktemp -d`?

## Tests, smokes and rollback

Local, before the session: the PR592 modules green on the merged SHA; a dry parse of the four planned pipelines through `shlex` to catch quoting errors; `usable_envelope` run against synthetic envelopes for each planned schema. On host, before writes: the seven preflight questions. After staging: the META bootstrap hash. After each command: local `usable_envelope`. Rollback is removal of the temp directory, which the session performs anyway; nothing else changed on the host. The findings PR is docs-only and reverts cleanly.

## Gates

Two independent parallel plan reviews on this file; separate design with two reviews resolving every obligation above; PR592 merged on exact-head green CI; then one session; then a findings PR with two reviews and a clearance file. Passing this plan's reviews authorizes design work only.
