**New primitives introduced:** two proposed stdlib-only, spawn-nothing scripts, `scripts/receipt_inventory_reducer.py` and `scripts/receipt_inventory_meta.py`; three proposed test modules, `tests/test_receipt_inventory_reducer.py`, `tests/test_receipt_inventory_meta.py` and `tests/test_receipt_inventory_envelope.py` (the last is Linux-only and spawns the existing supervisor with synthetic input); one pure test helper `usable_envelope()` that encodes the consumer rule. No collector, transport, host command, production path, dependency, CI job, secret, config, schema or writer. Implementation is not authorized by this plan.

# Plan: receipt inventory reducer and META, local and synthetic only

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Journald JSON reduction with allowlists | Public hub fetched by the coordinator on 2026-09-16: catalog stuck at Loading, no verified matching skill: https://hermes-agent.nousresearch.com/docs/skills | No skill verified; not an exhaustive absence claim |
| Host identity metadata validation | Same fetch, same result | Same |
| Ecosystem | https://github.com/0xNyk/awesome-hermes-agent fetched 2026-09-16: general orchestration libraries; no inspected item is a Gecko receipt reducer | Not a replacement |
| Process ownership under a deadline | Existing `scripts/receipt_inventory_supervisor.py`, merged in PR590 with Linux cleanup tests green at f5bc54ba | Reuse unchanged; nothing in this plan touches it |
| Journald envelope parsing | In-repo `_structlog_obj` in `scripts/backfill_ledger_eviction_markers.py:50-84` handles the `MESSAGE` envelope and the list-of-bytes form, but imports `scout.db` and does not enforce caps, duplicate-key rejection or redaction | Reference only; the reducer must run under `python3 -I -S` with stdlib imports only |
| Cross-platform contract tests | `tests/test_receipt_supervisor_contracts.py` load pattern and static AST discriminators | Reuse the pattern |

**Verdict:** custom residual is a pure allowlisted reducer and a pure metadata validator on Python stdlib, verified against the existing supervisor. No Hermes dependency is introduced, and no ecosystem-wide absence is claimed.

## Why this slice, and what it is not

Master f5bc54ba contains PR590: the supervisor, fault runner, 29-case Linux harness and cross-platform contracts, all green in Linux CI. That closed the cleanup primitive only. The 2026-09-14 collection design (`design_suppression_receipt_inventory_2026_09_14.md`) remains closed as written: its process model rested on the disproved claim that the bare `timeout` wrapper cleans up the group, and its header says not to execute production collection with it. The prior read-only assessment (receipt-handoff-20260916T1537Z) found that the reducer and META scripts still do not exist, so their adversarial-test gate is untouched. The last two runs deferred this in a circle. This run owns it.

**Scope in one sentence:** implement the reducer and META locally, prove them against synthetic adversarial inputs on Windows, and prove the envelope round trip under the merged supervisor on Linux CI, without any production command.

**Explicit non-authorization.** Passing every test in this plan does not authorize collection. Collection stays behind a separately reviewed integration design and a separately approved identity preflight. This plan does not amend the 2026-09-14 production authorization, does not license the pgid-file host write the supervisor requires, and does not choose a transport for the script source. Those items are listed under Integration prerequisites so the future design cannot miss them, and nowhere else.

**Historical status kept honest.** The original plan's fifteen-second budget and "timeout kills the entire group" sentence are retained only as history. The original plan's prohibition on host writes still stands and now conflicts with the supervisor's mandatory `--pgid-file` write. That conflict is not resolved here.

## Drift check at f5bc54ba (read-only, this worktree)

- No `scripts/*reduc*`, `scripts/*meta*` or `scripts/*journal*` exists; the proposed names do not collide.
- `tests/test_receipt_archive.py` and `scout/trading/receipt_archive.py` concern detection receipts, an unrelated concept. The `receipt_inventory_` prefix already used by the supervisor is kept for everything here to avoid conflation.
- All 13 allowlisted event names from the approved design are still present in `scout/`: `outcome_ledger.py` 195, 305, 481, 591, 600, 610, 730, 831; `trading/decision_events.py` 36, 68, 78, 89; `main.py` 1869, 1885. Two further `ledger_record_failed` string sites exist at `scout/trading/engine.py:288` and `:779` that the 09-14 findings did not list. The allowlist is unaffected because it matches on event name and never prints values; the design should note the sites.
- Logging shape is unchanged at `scout/main.py:2327-2336`: structlog with `TimeStamper(fmt="iso")`, `add_log_level`, `JSONRenderer`, `PrintLoggerFactory`. The `timestamp` and `level` keys in the presence allowlist come from those processors.
- Lints that scan `scripts/*.py` and will run against the new files: `tests/test_round8_subprocess_timeouts.py` (no unbounded subprocess; the scripts spawn nothing), `tests/test_datetime_hygiene.py` (no `utcnow`), `tests/test_datetime_predicate_lint.py`, `tests/test_alert_registry_coverage.py`, `tests/test_tg_shadow_pinning.py`, `tests/test_retire_dead_tables_no_dangling_refs.py`. None should match a pure reducer; all must stay green.
- `.gitattributes` pins `*.py text eol=lf`; the Windows author must keep LF on the new scripts.
- CI: the `receipt-inventory-timeout` job runs only `test_receipt_inventory_timeout.py`; the full `test` job runs the suite with a test-count baseline of 1232 in `.github/workflows/test.yml:113-120`. New tests can only raise the count.
- `.reviewers/590.toml` belongs to the merged PR; this work needs its own PR and its own clearance file.

## Supervisor contract the scripts must fit (from source, not from prose)

Read from `scripts/receipt_inventory_supervisor.py` at f5bc54ba:

| Fact | Value | Line |
|---|---|---|
| Retained inner stdout cap | 65,536 bytes; excess counts in `dropped` | 32, 100-123 |
| Status line cap | 131,072 bytes; beyond it `output` is dropped and OK becomes `OUTPUT_OVERFLOW` | 34, 321-329 |
| `output` present | only when status is `OK` and `dropped` is 0, base64 of the captured bytes | 319-320 |
| Inner exit that yields `INNER_TIMEOUT` | 124, -9, 137 | 31 |
| Nonzero inner exit not in that set | `COMMAND_FAILED`, `output` null | 291-293 |
| Fixed envelope keys | 17 keys in `STATUS_KEYS` | 52-56 |
| Consumer rule | a line lacking a terminating newline or failing fixed-key JSON parsing is a failure | design section 4 |

Consequences the design must honor:

- The reducer's single output line must be provably small. The plan sets a hard test bound of 4,096 bytes for the reducer line and 4,096 bytes for the META line, both far under the 65,536 capture cap, so `dropped` stays 0 and the base64 envelope stays under 131,072.
- Under `pipefail`, any nonzero inner exit hides the reducer's own status line. The reducer's exit-code policy is therefore a real decision, listed below; whichever choice the design makes, the consumer rule must fail closed.
- The reducer never writes stderr under `-I -S`; the wrapper discards stderr anyway.

## Obligations for the design

The design must choose and prove these; the plan only fixes what the approved 2026-09-14 design already fixed (d8625be4) and what the supervisor's source now imposes.

**Reducer, carried over from d8625be4.**
- Reads `sys.stdin.buffer` only, at most 2,097,153 bytes. More than 2,097,152 bytes gives `OVERFLOW` with no counts. More than 200 newline-delimited records gives `RECORD_CAP` with no counts. Exactly 200 gives `saturated=true`.
- Each record: outer JSON parsed with a duplicate-key-rejecting `object_pairs_hook`; must be a dict; `__REALTIME_TIMESTAMP` must be 1 to 19 decimal digits; window is `start <= ts < end` in epoch microseconds; `MESSAGE` must be `str`, parsed with the same hook, must be a dict with `event` as `str`.
- Fixed counters: `malformed`, `duplicate_key`, `ts_invalid`, `out_of_window`, `message_nonstr`, `message_nonjson`, `event_invalid`.
- Allowlists fixed: the 13 event names and the 11 presence-only keys (`ledger_id`, `event_id`, `kind`, `surface`, `token_id`, `signal_type`, `decision`, `reason`, `site`, `timestamp`, `level`). Unknown events and keys are counted, never printed. No value is ever printed.
- Output: one newline-terminated JSON line with fixed keys `status`, `bytes`, `records`, the counters, `saturated`, `observed_span` (first and last in-window ISO UTC, span only, never coverage) and `events`.
- `except Exception` prints only `{"status":"INTERNAL_ERROR"}`. The reducer spawns nothing and catches nothing broader.

**Reducer, new obligations.**
- No baked-in production values: the window bounds are explicit inputs; the record cap and byte cap are module constants; no `journalctl`, unit name, host path or the 2026-09-14 window appears in the script. A static test enforces this.
- Import allowlist enforced by a static test: `sys`, `json`, and at most `re` and `datetime`. No `os` beyond what a test proves necessary, no `subprocess`, `socket`, `urllib`, `http`, `sqlite3`, `ctypes`, `signal`, `shutil`, `asyncio`, `aiohttp`, no `open()` in a write mode, no `eval`, `exec` or `__import__`.
- Output key set equals the fixed set exactly; no key is derived from input. Serialization is `json.dumps(..., separators=(",", ":"), sort_keys=True)` so the size bound is checkable.
- A pure function `reduce(data: bytes, start_us: int, end_us: int) -> dict` is separated from `main()` so tests call it in-process and through a `python -I -S` subprocess.

**META, carried over from d8625be4 and adapted for local testing.**
- Reads stdin only; validates a 40-lowercase-hex HEAD; checks `ActiveState` and `StandardOutput` against fixed value sets; computes sha256 in-process with `hashlib` for an explicit bounded list of files, reading at most 16 MiB each; emits fixed-key JSON, or a fixed status with no raw text.
- New: file paths are explicit inputs relative to an explicit root so tests use `tmp_path`; no `/root/gecko-alpha` or `gecko-pipeline.service` literal in the script. The future preflight, not this script, decides which files to hash; the plan only requires the list to be able to include the supervisor, reducer and META themselves, which the handoff asked for.
- Unknown or malformed values are reported as fixed status codes, never echoed.

**Consumer rule as code.** `usable_envelope(line: bytes) -> tuple[bool, str]` in the Linux round-trip test module: newline-terminated, fixed 17 keys, `status == "OK"`, `dropped == 0`, `output` decodes, decoded line is one newline-terminated JSON line with the reducer's fixed keys and `status == "OK"`. Every other case returns false with a fixed reason. This exists to make the handoff's usability rule executable, not to authorize its use.

## Files

| File | Purpose | Status |
|---|---|---|
| `tasks/plan_receipt_reducer_meta_2026_09_16.md` | this plan | this run |
| `tasks/todo.md` | owned checklist prepended, existing bytes preserved | this run |
| `tasks/design_receipt_reducer_meta_2026_09_16.md` | design after two plan reviews | later, separate |
| `scripts/receipt_inventory_reducer.py` | pure reducer | after design approval |
| `scripts/receipt_inventory_meta.py` | pure metadata validator | after design approval |
| `tests/test_receipt_inventory_reducer.py` | adversarial reducer tests, cross-platform | with the scripts |
| `tests/test_receipt_inventory_meta.py` | META validation tests, cross-platform | with the scripts |
| `tests/test_receipt_inventory_envelope.py` | Linux-only round trip under the merged supervisor | with the scripts |
| `tasks/review_receipt_reducer_meta_2026_09_16.md` | review ledger and mutation record | with the PR |
| `.reviewers/<PR>.toml` | four-vector clearances on the final SHA | after real reviews |

The existing supervisor, fault runner, harness, contract tests and CI workflow are not modified. If the design finds it must touch any of them, that is a scope change requiring a plan amendment.

## Tests

**Reducer adversarial set (Windows-runnable, in-process and via subprocess).**
1. Canary values (`CANARY_9f3a`, a URL, an exception string, a token-like id) placed in allowlisted keys, unknown keys, unknown event names, non-JSON `MESSAGE`, outer fields and invalid timestamps: no canary byte sequence appears in the output.
2. Duplicate keys in the outer record and inside `MESSAGE`: counted under `duplicate_key`, nothing parsed from that record.
3. `MESSAGE` as a list of integers (journald's non-UTF-8 form), a number, null, and a non-dict JSON: `message_nonstr` or `message_nonjson` as the design assigns; nothing printed.
4. Missing, non-string and unknown `event`: `event_invalid` or unknown-event count as the design assigns.
5. Timestamps at `start` (in), `start - 1` (out), `end - 1` (in), `end` (out); missing, empty, non-digit, signed, 20-digit and float forms (`ts_invalid`).
6. A partial last line without newline; CRLF endings; empty input; whitespace-only input.
7. Exactly 2,097,152 bytes parsed versus 2,097,153 bytes `OVERFLOW` with no counts; a subprocess run with more than the cap on stdin still exits within the test timeout.
8. 200 records `saturated` versus 201 `RECORD_CAP` with no counts.
9. Forced exception through a monkeypatched parser: only `{"status":"INTERNAL_ERROR"}` printed, exactly one line.
10. Output bound: with 200 records that each hit every allowlisted event and key, the serialized line is at most 4,096 bytes and is exactly one newline-terminated line.
11. Fixed keys: the output key set equals the constant, for every status.
12. Seeded stdlib `random` fuzz, bounded iterations: random nesting, random key collisions, random bytes; no exception escapes `main`, output is one line under the bound, and no input substring of eight or more bytes appears in the output unless it is an allowlisted name.
13. Static contracts: import allowlist, no write-mode `open`, no forbidden calls, no production literals, `main` guarded by `__name__`.

**META set (Windows-runnable).**
1. Valid HEAD, valid states, two small files: fixed JSON with expected sha256 values.
2. Uppercase, short, long and non-hex HEAD: fixed status, HEAD not echoed.
3. Unknown `ActiveState` and `StandardOutput` values, missing lines, extra lines, duplicate keys: fixed status, values not echoed.
4. Missing file, empty file, file above 16 MiB (sparse or generated): the design's fixed per-file or whole-run status; no path outside the explicit list is read (tested with a canary file present in the root but not in the list).
5. Output bound 4,096 bytes; fixed keys; static contracts as for the reducer.

**Linux envelope round trip (skipped on Windows, not cleanup proof).**
1. Spawn the merged supervisor with `--pgid-file` in `tmp_path` and the exact wrapper shape `timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c` around `cat FIXTURE | head -c 2097153 | python3 -I -S scripts/receipt_inventory_reducer.py ARGS`, where FIXTURE is a synthetic journal file written by the test. Assert supervisor status `OK`, `dropped` 0, `cleanup_proof` true, `pgrep -g G` returns 1 afterwards, and `usable_envelope` returns true with the expected counts.
2. The same with a fixture above the byte cap: assert the envelope is not usable and record which status the supervisor reports; this pins the `pipefail` interaction named under Remaining decisions rather than leaving it to prose.
3. The same with a reducer forced to raise: `usable_envelope` false, reason fixed.
4. META under the supervisor with a fixture stdin: status `OK`, decodable, expected hashes.

**Mutation checklist, recorded in the review file.** For each guard, delete or flip it and name the test that fails: duplicate-key hook removed; window `<` changed to `<=`; byte cap off by one; record cap off by one; allowlist widened by one name; canary redaction bypassed on one key; `except Exception` widened to `BaseException`; output bound raised. A guard whose removal keeps the suite green is a comment, per the 2026-08-03 lesson, and must gain a discriminating test before review.

## Validation gates, in order

1. **Plan gate.** Two independent parallel plan reviews on this file. No design until both are terminal and folds are applied.
2. **Design gate.** Separate design file; two independent parallel design reviews; the design settles every item under Remaining decisions with a stated reason.
3. **Local gate (Windows, this workstation).** `python -m py_compile` on both scripts; `python -m unittest discover -s tests -p "test_receipt_inventory_*.py" -v` with the envelope module skipping; `git diff --check` clean; LF endings verified on the new `.py` files; the six scanning lints listed under Drift check run and pass.
4. **Linux gate (GitHub CI, exact head).** Full `test` job green including the envelope round trip; test-count baseline satisfied; `receipt-inventory-timeout` job unchanged and green; no workflow edit.
5. **Review barrier.** Two independent PR reviews on the final candidate; mutation checklist complete; four-vector clearances recorded in a new `.reviewers/<PR>.toml` on the exact final SHA; any code change after a review creates a new candidate and re-runs the affected reviewers.
6. **Merge.** Per-PR recorded operator approval. Merging changes nothing on any host.
7. **Not a gate here.** Collection. Green on every gate above leaves collection blocked behind the integration design and identity preflight below.

## Integration prerequisites for a future, separately reviewed design

Recorded so the next design cannot omit them. None is resolved by this plan, and none amends the 2026-09-14 authorization.

- Exit vocabulary: the old design's `OUTER_TIMEOUT` on wrapper exit 124 or 137 is now the supervisor's `INNER_TIMEOUT` (exit 6); `OUTER_TIMEOUT` (exit 7) means the leader was alive at `T0+13`. The caller's rule must be rewritten against supervisor exits 0 to 11.
- Usability rule: supervisor exit 0, newline-terminated fixed-key line, `dropped` 0, base64 decode, then reducer status `OK`. The `usable_envelope` helper in this plan is the local executable form of that rule.
- Host write: the supervisor requires `--pgid-file` and performs a temp-file write plus rename. The original plan forbids host writes. A future design must license exactly that write or collection cannot produce output.
- Caps: reducer and META lines bounded at 4,096 bytes here against the 65,536 capture cap and 131,072 line cap.
- Transport: how multi-kilobyte script source reaches the host over SSH without an oversized argv or an unlicensed file write is unspecified and belongs to the integration design.
- Identity: the supervisor must be present on the host and its hash, plus the reducer and META hashes, should be pinned by the identity preflight. META in this plan accepts an explicit file list so that pin is possible.
- Window source identity: expected source hashes must be pinned to the revision running during the collection window, not the HEAD at collection time. The pinned 2026-09-14 window predates the supervisor deploy, and journal retention is unknown.
- Budget text: the supervisor's report deadline is `T0+15.5` plus interpreter startup, not fifteen seconds.

## Remaining decisions for the design and reviewers

1. **Reducer exit code.** Exit 0 for every self-reported status so `OVERFLOW`, `RECORD_CAP` and `INTERNAL_ERROR` travel inside an `OK` envelope, versus nonzero on non-OK so the supervisor reports `COMMAND_FAILED` with null output. Both fail closed under the consumer rule; the first keeps the diagnostic, the second is simpler. Plan default: exit 0 on any written line, nonzero only when the line could not be written.
2. **Byte cap and `pipefail`.** With `head -c` upstream, an over-cap journal makes `journalctl` take SIGPIPE and the pipeline exit nonzero, masking `OVERFLOW` as `COMMAND_FAILED`. Whether the reducer drains stdin past its cap, and whether `head` stays in the pipeline, is a transport decision; the envelope test 2 pins observed behavior either way.
3. **Partial tail and CRLF.** Whether an unterminated last line is a record counted as `malformed`, or a separate fixed counter; whether a trailing `\r` is stripped or makes the record `malformed`. Plan default: count the partial tail as a record and as `malformed`; treat `\r` as malformed, since journald `-o json` emits LF.
4. **Window input form.** Two epoch-microsecond integers on argv versus ISO strings parsed in-script. Plan default: integers, validated as 1 to 19 digits, `start < end` required.
5. **META file list and missing files.** Per-file status inside a fixed map versus whole-run failure. Plan default: per-file fixed status, bounded to eight paths, whole run `OK` only if every file hashed.
6. **`MESSAGE` list-of-bytes form.** Keep the approved rule that a non-string `MESSAGE` is `message_nonstr` and not decoded, even though the in-repo backfill script decodes it. Plan default: keep the approved rule; decoding widens what can be printed by mistake.
7. **Test framework.** `unittest` for consistency with the sibling contract module and so the Windows run works without the project venv, versus pytest style. Plan default: `unittest`, which pytest collects under `testpaths`.
8. **Envelope test placement.** In the full-suite job only, versus also in the dedicated timeout job. Plan default: full suite only; the dedicated job's shape stays unchanged per the approved supervisor design.

## Rollback

Revert the PR: two scripts, three test modules, the review file and the clearance file. Nothing is deployed, nothing runs on any host, and no schedule, flag, config, account, vendor or policy state changes at any point in this plan.
