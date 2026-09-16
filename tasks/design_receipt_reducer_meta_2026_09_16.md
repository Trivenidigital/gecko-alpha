**New primitives introduced:** two stdlib-only offline scripts, three test modules and a pure usable_envelope helper, as approved in PR591. DESIGN DRAFT ONLY; no implementation or collection authorization.

# Design: receipt reducer and META — review draft

## Hermes-first analysis

Retain the checked domains, in-tree drift and ecosystem verdict in [the approved plan](plan_receipt_reducer_meta_2026_09_16.md#hermes-first-analysis). No additional capability or dependency is proposed by this design.

The Write tool is disabled in this session, so the design is returned here in full rather than to a file. Nothing was edited or run.

**Status.** This is a bounded design draft for two independent design reviews. It is not approval and does not authorize collection, transport, the pgid-file host write, or an identity preflight. Intended destination once reviewed: `tasks/design_receipt_reducer_meta_2026_09_16.md`, headed by the required new-primitives line naming the two scripts, three test modules and the `usable_envelope` helper.

**Drift found while reading source.** The event `ledger_record_failed` is also emitted at `scout/main.py:1534` and `scout/trading/signals.py:88`, beyond the two engine sites the plan noted. The allowlist matches on event name only, so nothing changes, but the review file should record all four.

**Decisions settled** (plan section "Remaining decisions"):

| # | Choice | Reason |
|---|---|---|
| 1 Exit code | Exit 0 whenever a line was written completely. Exit 1 only when the write itself fails. | Keeps `OVERFLOW`, `RECORD_CAP`, `USAGE`, `INTERNAL_ERROR` visible inside an `OK` envelope. The consumer rule still fails closed because it requires inner status `OK`. |
| 2 Cap and pipefail | Reducer reads at most cap+1 bytes and stops. It never drains stdin. `head -c 2097153` stays upstream. | Draining is unbounded work under a deadline. With `head` present the reducer always reaches EOF, so it never causes SIGPIPE itself. Envelope tests pin both outcomes: exactly cap+1 bytes gives supervisor `OK` with inner `OVERFLOW`; cap plus 1 MiB gives `COMMAND_FAILED` with null output. Both unusable. |
| 3 Partial tail, CR | A non-empty unterminated tail is a record and is `malformed`. Any CR byte anywhere in a record makes it `malformed`. No new key. | journald emits LF only. Python's json silently accepts a trailing CR, so an explicit byte check is required and mutation-testable. |
| 4 Window form | Two positional argv integers in epoch microseconds, each `[0-9]{1,19}`, `start < end`, `end <= 253402300799999999`. | The bound is the last microsecond of year 9999, so every in-window timestamp is representable in the span. A test pins the constant against `datetime.max`. |
| 5 META files | Per-slot fixed status in eight fixed slots `f0`..`f7`. Whole run `OK` only if every used slot is `OK` and all metadata checks pass, else `FAILED` with the same full schema. | Consumer needs one bit. Operator gets per-file diagnosis without paths or raw text. |
| 6 MESSAGE list form | Kept as approved: any non-string `MESSAGE` is `message_nonstr` and never decoded. | Decoding widens what can be printed by mistake. |
| 7 Framework | `unittest.TestCase` modules collected by pytest. | Matches the sibling contract module. Runs on Windows without the venv. |
| 8 Envelope test job | Full `test` job only. No workflow edit. | Plan scope. |

Two under-specified items are also settled and flagged for reviewers. The plan requires the 11 presence keys to be counted but the carried-over schema gives them no home, so the `OK` schema gains a fixed `keys` object plus two top-level integers `unknown_events` and `unknown_keys`. And `usable_envelope` takes an explicit third argument `schema="reducer"|"meta"` and never infers the schema from content.

## Reducer: `scripts/receipt_inventory_reducer.py`

**Module layout.** Imports are exactly `json`, `re`, `sys` and `from datetime import datetime, timedelta, timezone`. Constants: `BYTE_CAP=2_097_152`, `RECORD_CAP=200`, `LINE_BOUND=4_096`, `MAX_END_US=253_402_300_799_999_999`, `EPOCH`, `EVENTS` (13 names), `KEYS` (11 names), `COUNTERS` (7 names), `OK_KEYS` frozenset, `_TS = re.compile(r"[0-9]{1,19}")`. Two private exception classes: `_DuplicateKey(Exception)` (deliberately not a `ValueError`) and `_Rejected(Exception)`. Functions:

- `_pairs(pairs)`: builds the dict, raises `_DuplicateKey` on a repeated key.
- `_reject_constant`, `_parse_int` (more than 19 digits raises `_Rejected`), `_parse_float` (more than 64 chars raises `_Rejected`).
- `_loads(text)`: module-level name wrapping `json.loads` with the four hooks, so tests can replace it.
- `_iso(ts_us)`: `EPOCH + timedelta(microseconds=ts_us)` rendered by a manual f-string to exactly 27 chars `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Manual formatting avoids the glibc versus Windows difference in `%Y` padding for small years.
- `reduce(data: bytes, start_us: int, end_us: int) -> dict`: pure, the unit under test.
- `render(result) -> bytes`: `json.dumps(separators=(",",":"), sort_keys=True)` encoded UTF-8 plus one LF.
- `parse_args(argv)`, `_read(stream, limit)` (bounded loop until limit+1 bytes or EOF), `main(argv, stdin=None, stdout=None) -> int`, guarded by `__name__`.

`main` resolves `stdin` to `sys.stdin.buffer` and `stdout` to `sys.stdout.buffer` only when the arguments are `None`, so in-process tests inject `BytesIO`. Output goes through `stdout.write` plus `flush`, never `print`, so Windows never rewrites LF as CRLF. Order: bad argv gives `USAGE` without reading stdin; read at most cap+1; `reduce`; `render`. Any `Exception` in those steps is replaced by the singleton `INTERNAL_ERROR` line. Nothing broader than `Exception` is caught anywhere. A write or flush failure returns 1; otherwise 0.

**Argv grammar.** `ARGV := START END`, each `[0-9]{1,19}` checked with `re.fullmatch` on the ASCII class, never `str.isdigit`. Accept iff `int(START) < int(END) <= MAX_END_US`. Anything else, including extra args, signs, whitespace or Unicode digits, is `USAGE`.

**Input grammar and status precedence**, on raw bytes before any parsing:

```
INPUT   := RECORD* TAIL?
RECORD  := SEGMENT LF          (an empty SEGMENT is a record)
TAIL    := one or more non-LF bytes   (unterminated; counted as a record)
```

1. `len(data) > BYTE_CAP` gives singleton `OVERFLOW`.
2. `records = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)`; `records > 200` gives singleton `RECORD_CAP`. Nothing is parsed in either case.
3. Otherwise `OK`, `saturated = (records == 200)`, `bytes = len(data)`, each record classified.

Empty input is `OK` with zero records. Whitespace-only input is one malformed record.

**Per-record classification.** Exactly one counter per failing record; the first failing step wins.

| Step | Check | Counter on failure |
|---|---|---|
| 1 | no CR byte in segment | `malformed` |
| 2 | strict UTF-8 decode | `malformed` |
| 3 | `_loads` succeeds; `JSONDecodeError`, `RecursionError`, `_Rejected`, other `ValueError` | `malformed` |
| 3a | `_loads` raises `_DuplicateKey` | `duplicate_key` |
| 4 | top value is a dict | `malformed` |
| 5 | `__REALTIME_TIMESTAMP` present, is str, fullmatches `_TS` | `ts_invalid` |
| 6 | `start_us <= ts < end_us` | `out_of_window` |
| 7 | record is in-window: update `observed_span` min and max now | none |
| 8 | `MESSAGE` present and is str | `message_nonstr` |
| 9 | inner `_loads` raises `_DuplicateKey` | `duplicate_key` |
| 9a | inner parse fails for any other enumerated reason, or top value not dict | `message_nonjson` |
| 10 | `event` present and is str | `event_invalid` |
| 11 | `event in EVENTS`: increment `events[event]`, increment `keys[k]` for each of the 11 names present, increment `unknown_keys` once if any other key is present | none |
| 11a | `event not in EVENTS`: increment `unknown_events`; message keys are not inspected | none |

Only the enumerated exception classes are caught per record. Anything else propagates to `main` and becomes `INTERNAL_ERROR`, so an unclassified failure is reported as a reducer failure rather than folded into a count. `observed_span` uses min and max, not encounter order, and is a span, never coverage. The only strings that can reach output are fixed schema names, fixed event and key names, and two ISO timestamps derived from validated digit strings.

**Output schemas.** Singleton schema for `USAGE`, `OVERFLOW`, `RECORD_CAP`, `INTERNAL_ERROR`: exactly `{"status": name}`. The `OK` schema:

| Key | Type | Bound |
|---|---|---|
| `status` | `"OK"` | |
| `bytes` | int | 0 to 2,097,152 |
| `records`, the seven counters, `unknown_events`, `unknown_keys` | int | 0 to 200 |
| `saturated` | bool | |
| `observed_span` | `{"first": str or null, "last": str or null}` | 27 chars each |
| `events` | object with exactly the 13 names | int 0 to 200 each |
| `keys` | object with exactly the 11 names | int 0 to 200 each |

`unknown_keys` counts records, not key occurrences, so every integer except `bytes` has at most three digits. Every key is a fixed literal, so rendering a result populated with the maximum of every bound is the exact worst case. Expected size is under 900 bytes against the 4,096 bound and the supervisor's 65,536 capture cap.

**Static contracts.** Import allowlist as above. No `os`, `subprocess`, builtin `open`, `print`, `eval`, `exec`, `compile`, `__import__`, `input`, `breakpoint`. No literal `journalctl`, `systemctl`, `gecko-pipeline`, `.service`, `/root/`, `2026-09-14`, `20:45`, `21:45` anywhere including docstrings. `__name__` guard calls `main(sys.argv[1:])`. File bytes contain no CR.

## META: `scripts/receipt_inventory_meta.py`

**Module layout.** Imports are exactly `hashlib`, `json`, `os`, `re`, `stat`, `sys`. Constants: `INPUT_CAP=4_096`, `FILE_CAP=16 MiB`, `CHUNK=65_536`, `READ_ITERATIONS = 4 * (FILE_CAP // CHUNK) + 2`, `SLOTS` `f0`..`f7`, `PATH_BOUND=256`, `ROOT_BOUND=1_024`, `ACTIVE_STATES` (active, reloading, inactive, failed, activating, deactivating, maintenance), `STANDARD_OUTPUTS` (inherit, null, tty, journal, kmsg, journal+console, kmsg+console, socket), `SLOT_STATUSES` (OK, UNUSED, MISSING, LINK, NOT_REGULAR, TOO_LARGE, CHANGED, UNREADABLE), `FULL_KEYS`, and `OPEN_FLAGS = O_RDONLY | O_BINARY | O_NOFOLLOW | O_NONBLOCK | O_NOCTTY | O_CLOEXEC` using `getattr(os, name, 0)` for flags absent on Windows. Functions: `parse_args`, `resolve_root` (strict realpath, must be a directory), `validate_metadata(data) -> dict`, `hash_slot(root_real, rel) -> dict`, `check(root_real, rels, metadata) -> dict`, `render`, `_read`, `main(argv, stdin=None, stdout=None)`.

`main` order: bad argv gives `USAGE` without reading stdin; bad root gives `ROOT_INVALID`; stdin longer than 4,096 gives `INPUT_CAP` before parsing and before any file is touched; then `check` and `render`. Exception and exit-code policy identical to the reducer.

**Argv grammar.**

```
ARGV := ROOT REL{1,8}
ROOT := absolute per os.path.isabs, 1 to 1,024 chars, no NUL
REL  := SEG ("/" SEG)*        total 1 to 256 chars
SEG  := [A-Za-z0-9._-]{1,255}  and SEG not in {".", ".."}
```

Backslashes, empty segments, leading slash, drive letters, NUL, duplicate RELs, zero or more than eight RELs are all `USAGE`. Path syntax is the caller's contract and fails the whole run. Filesystem findings are per slot.

**Metadata stdin grammar.** The production shape with no production name:

```
INPUT    := HEADLINE KVLINE KVLINE
HEADLINE := [0-9a-f]{40} LF
KVLINE   := ("ActiveState=" VALUE | "StandardOutput=" VALUE) LF    each key once, either order
VALUE    := one or more bytes other than LF and "="
```

`format_ok` is false when input is not strict ASCII, lacks a final LF, has other than three lines, repeats or misuses a key, or a line has no `=`. Then all value fields are null and all `_ok` flags false. Otherwise `head` is the fullmatched 40-hex or null, `active_state` and `standard_output` are the value if it is a member of the fixed set, else null with the flag false. Output values are set members or null, never raw text.

**Per-slot hashing, containment and identity.** In this order; the first failing step sets the slot status.

1. `full = os.path.join(root_real, *segments)`.
2. `real = os.path.realpath(full, strict=True)`. `FileNotFoundError` gives `MISSING`; other `OSError` gives `UNREADABLE`; `real != full` gives `LINK`. This rejects a symlink, junction or reparse point at any component and therefore any escape from the root before the content is opened.
3. `pre = os.lstat(full)`: `S_ISLNK` gives `LINK`; not `S_ISREG` gives `NOT_REGULAR`; size over cap gives `TOO_LARGE`.
4. `fd = os.open(full, OPEN_FLAGS)`: `ELOOP` gives `LINK`; `ENOENT` gives `MISSING`; `EISDIR` gives `NOT_REGULAR`; other `OSError` gives `UNREADABLE`. Every later path closes `fd` in `finally`.
5. `st = os.fstat(fd)`: not `S_ISREG` gives `NOT_REGULAR`; size over cap gives `TOO_LARGE`; `(st_dev, st_ino)` differing from `pre` gives `CHANGED`.
6. Read loop of at most `READ_ITERATIONS` calls to `os.read(fd, CHUNK)`, stopping at EOF or once the running total exceeds the cap. Total over cap gives `TOO_LARGE`. The loop reads past the cap rather than stopping at it, so the extra byte is the sentinel: a file that grew past the cap is never reported as hashed. Iteration cap reached gives `UNREADABLE`. Total not equal to `st.st_size` gives `CHANGED`, covering growth, truncation and pseudo-files that report size 0 but produce data.
7. `post = os.fstat(fd)`: any difference in `(st_dev, st_ino, st_size, st_mtime_ns)` gives `CHANGED`.
8. Slot is `OK` with 64-hex `sha256` and `size`.

Claimed: a link or escape present at the pre-open check is rejected; a leaf symlink present at open time is rejected on POSIX by `O_NOFOLLOW`; replacement between `lstat` and `open`, and any size change during hashing, is reported as `CHANGED`. Not claimed: protection against an adversary racing the filesystem between realpath and open on any platform. On Windows `O_NOFOLLOW` does not exist, so leaf-link rejection rests on the pre-open checks only. The Windows run is a discovery check; Linux CI is mandatory for the link and special-file cases.

**Output schemas.** Singleton for `USAGE`, `ROOT_INVALID`, `INPUT_CAP`, `INTERNAL_ERROR`. Full schema for `OK` and `FAILED`: `status`; four booleans `format_ok`, `head_ok`, `active_state_ok`, `standard_output_ok`; `head` (40-hex or null); `active_state` and `standard_output` (set member or null); `files` with exactly `f0`..`f7`, each `{"status": SLOT_STATUS, "sha256": 64-hex or null, "size": int or null}`. `status` is `OK` iff all four booleans are true and every used slot is `OK`. Worst case is eight `OK` slots plus the longest set members, asserted under 4,096 bytes.

**Static contracts.** Import allowlist as above. The only `os` attributes referenced are `open`, `read`, `close`, `fstat`, `lstat`, `path.join`, `path.isabs`, `path.realpath`, `path.isdir` and `O_*` constants. No builtin `open` at all. No `O_WRONLY`, `O_RDWR`, `O_CREAT`, `O_TRUNC`, `O_APPEND` name anywhere. No `print`, no forbidden builtins, no production literals including `/root/gecko-alpha` and `gecko-pipeline.service`, no CR bytes.

## Consumer rule, tests and falsifiers

**`usable_envelope(returncode, line, schema) -> (bool, reason)`** in `tests/test_receipt_inventory_envelope.py`. Returns `(True, "OK")` only when every check passes, else the first failing fixed reason in this order:

| Order | Check | Reason |
|---|---|---|
| 1 | `returncode == 0`, and not a bool | `RETURNCODE` |
| 2 | bytes, ends with LF, exactly one LF | `NOT_ONE_LINE` |
| 3 | at most 131,072 bytes | `LINE_TOO_LONG` |
| 4 | duplicate-rejecting parse finds a duplicate | `OUTER_DUPLICATE_KEY` |
| 5 | parse failure or not a dict | `OUTER_JSON` |
| 6 | key set equals the supervisor's 17 `STATUS_KEYS` | `OUTER_KEYS` |
| 7 | types: `status` str; `exit_code`, `pgid`, `inner_exit`, `reaped`, `kills`, `signals_received`, `teardowns`, `dropped` int and not bool; `inner_elapsed`, `total_elapsed` number and not bool; `cleanup_proof`, `overflow` bool; `signal_phase`, `error`, `survivors` null; `output` str | `OUTER_TYPES` |
| 8 | `status=="OK"`, `exit_code==0`, `inner_exit==0`, `cleanup_proof is True`, `dropped==0`, `overflow is False`, `reaped==0`, `kills==0`, `signals_received==0`, `teardowns==1`, `pgid>1`, `0<=inner_elapsed<=total_elapsed` | `OUTER_INVARIANTS` |
| 9 | `base64.b64decode(output, validate=True)` | `OUTPUT_BASE64` |
| 10 | decoded ends with LF, exactly one LF | `INNER_NOT_ONE_LINE` |
| 11 | decoded at most 4,096 bytes | `INNER_TOO_LONG` |
| 12 | inner duplicate key | `INNER_DUPLICATE_KEY` |
| 13 | inner parse failure or not dict | `INNER_JSON` |
| 14 | key set equals `reducer.OK_KEYS` or `meta.FULL_KEYS` per `schema`; any other `schema` raises `ValueError` as a caller bug | `INNER_KEYS` |
| 15 | inner types per the schema tables, nested shapes included, bool never accepted as int | `INNER_TYPES` |
| 16 | inner `status == "OK"` | `INNER_STATUS` |

The `reaped==0`, `kills==0` invariants follow from supervisor `_status` at `scripts/receipt_inventory_supervisor.py:289-296`, where `OK` is unreachable after any sweep. `teardowns==1` follows from the cleanup phase always running once after a spawn. The module loads the scripts with the `load()` pattern from `tests/test_receipt_supervisor_contracts.py:33-38` and additionally pins each schema constant to a literal frozenset so script drift fails a test rather than moving the oracle.

**Reducer tests** (`tests/test_receipt_inventory_reducer.py`, cross-platform, in-process via `BytesIO` and via `[sys.executable, "-I", "-S", REDUCER, ...]` with `timeout=60`):

- `test_canaries_never_appear`: canaries in allowlisted keys, unknown keys, unknown events, non-JSON MESSAGE, outer fields, bad timestamps. Kills any value copied to output.
- `test_duplicate_outer_key_counted_and_not_parsed`, `test_duplicate_message_key_counted`: kill hook removal in either layer.
- `test_message_list_number_null_object_are_nonstr`: kills a decoder for the list form.
- `test_message_nonjson_and_nondict`, `test_event_missing_nonstr_unknown`: the latter also asserts an unknown-event record's keys are not counted.
- `test_window_boundaries`: start in, start-1 out, end-1 in, end out. Kills `<` to `<=`.
- `test_ts_forms`: missing, empty, non-digit, signed, 20 digits, float, fullwidth digit all `ts_invalid`; 19 digits and leading zeros accepted. Kills an `isdigit` substitution.
- `test_cr_is_malformed`: kills removal of the CR check, since json would otherwise parse it.
- `test_partial_tail_is_a_malformed_record`: unterminated valid JSON gives one record, one malformed; `b""` zero records; `b"   "` and `b"\n"` one malformed each.
- `test_byte_cap_exact`, `test_record_cap_exact`: exact boundary pairs. The 201st record is garbage that must not appear in any counter, proving the LF count precedes parsing.
- `test_overcap_stdin_subprocess_exits`: 3 MiB on stdin, returncode 0, one `OVERFLOW` line, within timeout.
- `test_forced_exception_is_singleton_internal_error`: `_loads` replaced by a `RuntimeError` raiser gives exactly the singleton line; a `KeyboardInterrupt` raiser propagates. Kills widening to `BaseException`.
- `test_nan_infinity_big_int_long_float_rejected`, `test_deep_nesting_is_malformed`: hooks and `RecursionError` classification.
- `test_ok_schema_exact`, `test_singleton_schemas_exact`, `test_usage_forms` (including proof that stdin is not read on `USAGE` via a `read` that raises).
- `test_max_end_us_matches_datetime_max`, `test_iso_is_27_chars_and_platform_independent`, `test_observed_span_is_min_max_over_in_window`.
- `test_worst_case_line_bound`: a max-value render and a 200-record input spread across all 13 events with all 11 keys, both at most 4,096 bytes with the literal pinned in the test.
- `test_seeded_fuzz`: `random.Random(20260916)`, 300 iterations, per-iteration unique canaries, no exception escapes `main`, one line under bound.
- `test_output_uses_lf_only_on_windows`, `test_exit_code_policy`, `test_static_contracts`.

**META tests** (`tests/test_receipt_inventory_meta.py`):

- `test_two_small_files_ok`, `test_head_forms`, `test_state_values` (including a `file:/x` form giving null), `test_format_forms`, `test_input_cap` (4,097 bytes gives the singleton and no `lstat` call occurs, proven by a recording shim), `test_usage_forms`, `test_root_invalid`.
- `test_missing_empty_and_oversized`: empty file hashes fine; 16 MiB + 1 gives `TOO_LARGE` with no digest; exactly 16 MiB is `OK`.
- `test_canary_file_not_read`: a canary in root but not in the list. Its bytes and digest never appear and a shim over `os.open`, `os.lstat`, `os.path.realpath` records no call with its path.
- `test_leaf_symlink_is_link`, `test_directory_symlink_component_is_link`: Linux mandatory; Windows skips when `os.symlink` raises. `test_directory_and_fifo_are_not_regular`: FIFO case Linux only, proves `O_NONBLOCK` by returning at all.
- `test_changed_during_hash`, `test_growth_past_cap_is_too_large`, `test_read_iteration_cap`: shims over `os.read` and `os.fstat`. These kill removal of the sentinel read and the post-hash fstat.
- `test_full_and_singleton_schemas_exact`, `test_worst_case_line_bound`, `test_forced_exception_is_singleton_internal_error`, `test_static_contracts`.

**Envelope tests.** Pure negative oracle, one case per reason above, each mutating exactly one thing in a known-good synthetic envelope: returncode 11, returncode `True`, two lines, missing LF, 131,073 bytes, duplicate outer key, missing and extra key, `cleanup_proof: 1`, `dropped: 1`, `overflow: true`, `reaped: 1`, `OK` with `exit_code: 3`, bad base64, base64 of two lines, inner duplicate, inner extra key, inner `saturated: 0`, inner `OVERFLOW`, bad `schema` raises. Linux-only cases under `skipUnless(sys.platform == "linux")`, with the prerequisite check from `tests/test_receipt_inventory_timeout.py:1259-1268` and reusing that module's `assert_empty` and `guarded_group` as the survivor oracle:

- Round trip `OK`: supervisor with `--pgid-file` in `tmp_path`, then `timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c` around `cat FIXTURE | head -c 2097153 | <sys.executable> -I -S REDUCER START END`, via `subprocess.run(timeout=20)`. Asserts returncode 0, helper true, decoded counts equal the fixture's known counts, group empty.
- Cap+1 fixture: supervisor `OK`, inner `OVERFLOW`, helper false with `INNER_KEYS`.
- Cap + 1 MiB fixture: `COMMAND_FAILED`, null output, helper false, `inner_exit` recorded. This pins the pipefail interaction.
- Forced raise: a fault runner written under `tmp_path` that loads the reducer, replaces `_loads`, calls `main`. Inner `INTERNAL_ERROR`, helper false. Mirrors the precedent of `tests/receipt_supervisor_faults.py` without committing a new file.
- META round trip: a valid three-line fixture piped into META with a `tmp_path` root and two files; helper true with `schema="meta"`, digests match.

**Mutation checklist for the review file.** Hook removed: the duplicate-key tests plus the envelope duplicate cases. Window `<=`: boundaries test. Byte cap off by one: exact test plus cap+1 round trip. Record cap off by one: exact test. Allowlist widened: schema-exact tests with literal name sets. Value leaked on one key: canary and fuzz tests. `BaseException`: forced-exception test. Bound raised in script: worst-case test pins 4,096. CR check removed: CR test. `isdigit` substitution: ts and usage tests. META sentinel removed: growth test. Post-hash fstat removed: changed test. realpath comparison removed: directory-symlink test. `O_NOFOLLOW` dropped: leaf-symlink test with the lstat step stubbed in a variant. `O_NONBLOCK` dropped: FIFO test hangs into the pytest timeout. Helper accepting returncode 11 or bool-as-int: the corresponding oracle cases.

**Verification for the build run**, local Windows, in order: `py_compile` on both scripts; `python -m unittest discover -s tests -p "test_receipt_inventory_*.py" -v` with Linux cases skipping; pytest on the three new modules plus the contract module and the six scanning lints named in the plan; `git diff --check`. New files must be created with `Path.write_bytes` to keep LF, per the 2026-05-13 tooling lesson. Then the full Linux `test` job on the PR head with the count baseline, two independent PR reviews, the mutation table completed in the review file, and a new clearance file in the shape of `.reviewers/590.toml` on the final SHA.

**Not in this design.** No production command, transport, host write licence, identity preflight, unit name, host path, window value, schedule, flag, account or vendor. No change to the supervisor, fault runner, harness, contract tests or CI workflow. If the build finds it must touch any of those, that is a plan amendment.

Next step is two independent design reviews on this draft before any file is created.
