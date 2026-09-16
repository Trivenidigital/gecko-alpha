**New primitives introduced:** two stdlib-only offline scripts, three test modules and a pure usable_envelope helper, as approved in PR591. Design approved atfa032eea; implementation reviewed at08063e64. No production collection authorization.

# Design: receipt reducer and META — review draft, revision 2

## Hermes-first analysis

Evidence below is carried from the approved plan (`plan_receipt_reducer_meta_2026_09_16.md`, checks performed by the coordinator on 2026-09-16 and approved at ddb185b6). This revision performed no fresh web or hub check and claims none.

| Domain | Hermes skill found? (plan evidence, 2026-09-16) | Decision |
|---|---|---|
| Journald JSON reduction with allowlists | Public hub catalog stuck at Loading; no verified matching skill | No skill verified; not an exhaustive absence claim |
| Host identity metadata validation | Same fetch, same result | Same |
| Ecosystem | awesome-hermes-agent listing fetched 2026-09-16: general orchestration; no inspected item is a Gecko receipt reducer | Not a replacement |
| Process ownership under a deadline | In-repo `scripts/receipt_inventory_supervisor.py` (PR590, Linux cleanup tests green) | Reuse unchanged |
| Journald envelope parsing | In-repo `_structlog_obj` in `scripts/backfill_ledger_eviction_markers.py:50-84`; imports `scout.db`, no caps, no duplicate-key rejection | Reference only |
| Cross-platform contract tests | `tests/test_receipt_supervisor_contracts.py` load pattern and AST discriminators | Reuse the pattern |

**Verdict:** the residual is a pure allowlisted reducer and a pure metadata validator on Python stdlib, verified against the existing supervisor. No dependency, no ecosystem-wide absence claim.

## Status and scope

This is a bounded design draft for two independent design reviews. It is not approval and does not authorize collection, transport, the pgid-file host write, or an identity preflight. Revision 2 folds both REQUEST CHANGES reviews of 32842337; the folds are marked inline as **Fold 1** to **Fold 7**.

**Drift found while reading source.** The event `ledger_record_failed` is also emitted at `scout/main.py:1534` and `scout/trading/signals.py:88`, beyond the two engine sites the plan noted. The allowlist matches on event name only, so nothing changes, but the review file should record all four.

**Bounded schema amendment (Fold 7; requires explicit acceptance by both design reviewers before build).** The approved plan requires the 11 presence-only keys to be counted and requires unknown events and unknown keys to be counted, never printed, but the carried-over `OK` schema (`status`, `bytes`, `records`, the counters, `saturated`, `observed_span`, `events`) has no field that can carry those counts. This design adds exactly three fixed keys to the `OK` schema and nothing else:

| Added key | Type | Reconciles |
|---|---|---|
| `keys` | object with exactly the 11 presence-key names, int counts | plan: presence-only key counting |
| `unknown_events` | int | plan: unknown events counted, never printed |
| `unknown_keys` | int | plan: unknown keys counted, never printed |

No other schema change is proposed. If either reviewer rejects the amendment, the counts have no home and the plan must be amended instead; the build does not start until both reviewers have recorded acceptance.

**Decisions settled** (plan section "Remaining decisions"):

| # | Choice | Reason |
|---|---|---|
| 1 Exit code | Exit 0 whenever a line was written completely. Exit 1 only when the write itself fails. | Keeps `OVERFLOW`, `RECORD_CAP`, `USAGE`, `INTERNAL_ERROR` visible inside an `OK` envelope. The consumer rule still fails closed because it requires inner status `OK`. |
| 2 Cap and pipefail | Reducer reads at most cap+1 bytes and stops. It never drains stdin. `head -c 2097153` stays upstream. | Draining is unbounded work under a deadline. With `head` present the reducer always reaches EOF, so it never causes SIGPIPE itself. Envelope tests pin both outcomes: exactly cap+1 bytes gives supervisor `OK` with inner `OVERFLOW`; cap plus 1 MiB gives `COMMAND_FAILED` with null output. Both unusable. |
| 3 Partial tail, CR | A non-empty unterminated tail is a record, is classified `malformed` before the parser runs, and the parser is never called on it (Fold 3). Any CR byte anywhere in a terminated record makes it `malformed`. No new key. | An unterminated tail is truncation evidence, not a record to interpret; parsing it could only widen behaviour. journald emits LF only. Python's json silently accepts a trailing CR, so an explicit byte check is required and mutation-testable. |
| 4 Window form | Two positional argv integers in epoch microseconds, each `[0-9]{1,19}`, `start < end`, `end <= 253402300799999999`. | The bound is the last microsecond of year 9999, so every in-window timestamp is representable in the span. A test pins the constant against `datetime.max`. |
| 5 META files | Per-slot fixed status in eight fixed slots `f0`..`f7`, filled contiguously from `f0`. Whole run `OK` only if every used slot is `OK` and all metadata checks pass, else `FAILED` with the same full schema. | Consumer needs one bit. Operator gets per-file diagnosis without paths or raw text. |
| 6 MESSAGE list form | Kept as approved: any non-string `MESSAGE` is `message_nonstr` and never decoded. | Decoding widens what can be printed by mistake. |
| 7 Framework | `unittest.TestCase` modules collected by pytest. | Matches the sibling contract module. Runs on Windows without the venv. |
| 8 Envelope test job | Full `test` job only. No workflow edit. | Plan scope. |

`usable_envelope` takes an explicit third argument `schema="reducer"|"meta"` and never infers the schema from content.

## Reducer: `scripts/receipt_inventory_reducer.py`

**Module layout.** Imports are exactly `json`, `re`, `sys` and `from datetime import datetime, timedelta, timezone`. Constants: `BYTE_CAP=2_097_152`, `RECORD_CAP=200`, `LINE_BOUND=4_096`, `MAX_END_US=253_402_300_799_999_999`, `EPOCH`, `EVENTS` (13 names), `KEYS` (11 names), `STRUCTURAL_KEYS = frozenset({"event"})`, `COUNTERS` (7 names), `OK_KEYS` frozenset, `_TS = re.compile(r"[0-9]{1,19}")`. Two private exception classes: `_DuplicateKey(Exception)` (deliberately not a `ValueError`) and `_Rejected(Exception)`. Functions:

- `_pairs(pairs)`: builds the dict, raises `_DuplicateKey` on a repeated key.
- `_reject_constant`, `_parse_int` (more than 19 digits raises `_Rejected`), `_parse_float` (more than 64 chars raises `_Rejected`).
- `_loads(text)`: module-level name wrapping `json.loads` with the four hooks, so tests can replace it or count its calls.
- `_iso(ts_us)`: `EPOCH + timedelta(microseconds=ts_us)` rendered by a manual f-string to exactly 27 chars `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Manual formatting avoids the glibc versus Windows difference in `%Y` padding for small years.
- `reduce(data: bytes, start_us: int, end_us: int) -> dict`: pure, the unit under test.
- `render(result) -> bytes`: `json.dumps(separators=(",",":"), sort_keys=True)` encoded UTF-8 plus one LF. **Fold 4:** `render` raises if `len(line) > LINE_BOUND`, which `main` turns into the singleton `INTERNAL_ERROR`; the constant is therefore load-bearing at runtime, and a test pins `LINE_BOUND == 4096` literally.
- `parse_args(argv)`, `_read(stream, limit)` (bounded loop until limit+1 bytes or EOF), `main(argv, stdin=None, stdout=None) -> int`, guarded by `__name__`.

`main` resolves `stdin` to `sys.stdin.buffer` and `stdout` to `sys.stdout.buffer` only when the arguments are `None`, so in-process tests inject `BytesIO`. Output goes through `stdout.write` plus `flush`, never `print`, so Windows never rewrites LF as CRLF. Order: bad argv gives `USAGE` without reading stdin; read at most cap+1; `reduce`; `render`. Any `Exception` in those steps is replaced by the singleton `INTERNAL_ERROR` line. Nothing broader than `Exception` is caught anywhere. A write or flush failure returns 1; otherwise 0.

**Argv grammar.** `ARGV := START END`, each `[0-9]{1,19}` checked with `re.fullmatch` on the ASCII class, never `str.isdigit`. Accept iff `int(START) < int(END) <= MAX_END_US`. Anything else, including extra args, signs, whitespace or Unicode digits, is `USAGE`.

**Input grammar and status precedence**, on raw bytes before any parsing:

```
INPUT   := RECORD* TAIL?
RECORD  := SEGMENT LF          (an empty SEGMENT is a record)
TAIL    := one or more non-LF bytes   (unterminated; counted as a record; never parsed)
```

1. `len(data) > BYTE_CAP` gives singleton `OVERFLOW`.
2. `records = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)`; `records > 200` gives singleton `RECORD_CAP`. Nothing is parsed in either case.
3. Otherwise `OK`, `saturated = (records == 200)`, `bytes = len(data)`, each record classified.

Empty input is `OK` with zero records. Whitespace-only input is an unterminated tail: one record, `malformed`, parser not called.

**Per-record classification.** Exactly one counter per failing record; the first failing step wins.

| Step | Check | Counter on failure |
|---|---|---|
| 0 | **Fold 3:** record is a terminated `SEGMENT`, not the `TAIL`. The tail is classified here and `_loads` is never invoked on it | `malformed` |
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
| 11 | `event in EVENTS`: increment `events[event]`; increment `keys[k]` for each of the 11 names present; **Fold 1:** increment `unknown_keys` once for the record iff any message key is outside `KEYS ∪ STRUCTURAL_KEYS`, so the structural `event` key never counts as unknown | none |
| 11a | `event not in EVENTS`: increment `unknown_events`; message keys are not inspected | none |

Only the enumerated exception classes are caught per record. Anything else propagates to `main` and becomes `INTERNAL_ERROR`, so an unclassified failure is reported as a reducer failure rather than folded into a count. `observed_span` uses min and max, not encounter order, and is a span, never coverage. The only strings that can reach output are fixed schema names, fixed event and key names, and two ISO timestamps derived from validated digit strings.

**Conservation law** (used by the consumer rule, Fold 2): every record terminates in exactly one of the seven counters, one allowlisted event count, or `unknown_events`. Therefore `records == sum(counters) + sum(events.values()) + unknown_events` always holds for an `OK` result, and every `keys[k]` and `unknown_keys` is at most `sum(events.values())`.

**Output schemas.** Singleton schema for `USAGE`, `OVERFLOW`, `RECORD_CAP`, `INTERNAL_ERROR`: exactly `{"status": name}`. The `OK` schema (including the Fold 7 amendment keys):

| Key | Type | Bound |
|---|---|---|
| `status` | `"OK"` | |
| `bytes` | int | 0 to 2,097,152 |
| `records`, the seven counters, `unknown_events`, `unknown_keys` | int | 0 to 200 |
| `saturated` | bool | equals `records == 200` |
| `observed_span` | `{"first": str or null, "last": str or null}` | both null or both 27-char ISO with `first <= last` |
| `events` | object with exactly the 13 names | int 0 to 200 each |
| `keys` | object with exactly the 11 names | int 0 to 200 each |

`unknown_keys` counts records, not key occurrences, so every integer except `bytes` has at most three digits. Every key is a fixed literal, so rendering a result populated with the maximum of every bound is the exact worst case. Expected size is under 900 bytes against `LINE_BOUND` and the supervisor's 65,536 capture cap.

**Static contracts.** Import allowlist as above. No `os`, `subprocess`, builtin `open`, `print`, `eval`, `exec`, `compile`, `__import__`, `input`, `breakpoint`. No literal `journalctl`, `systemctl`, `gecko-pipeline`, `.service`, `/root/`, `2026-09-14`, `20:45`, `21:45` anywhere including docstrings. `__name__` guard calls `main(sys.argv[1:])`. File bytes contain no CR.

## META: `scripts/receipt_inventory_meta.py`

**Module layout.** Imports are exactly `errno`, `hashlib`, `json`, `os`, `re`, `stat`, `sys`. Both PR reviewers accepted the portable stdlib `errno` amendment on candidate406ed010; it supplies symbolic ELOOP/ENOENT/EISDIR constants without new capabilities. Constants: `INPUT_CAP=4_096`, `FILE_CAP=16 * 1024 * 1024`, `CHUNK=65_536`, `READ_ITERATIONS = 4 * (FILE_CAP // CHUNK) + 2`, `LINE_BOUND=4_096`, `SLOTS` `f0`..`f7`, `PATH_BOUND=256`, `ROOT_BOUND=1_024`, `ACTIVE_STATES` (active, reloading, inactive, failed, activating, deactivating, maintenance), `STANDARD_OUTPUTS` (inherit, null, tty, journal, kmsg, journal+console, kmsg+console, socket), `SLOT_STATUSES` (OK, UNUSED, MISSING, LINK, NOT_REGULAR, TOO_LARGE, CHANGED, UNREADABLE), `FULL_KEYS`, and `OPEN_FLAGS = O_RDONLY | O_BINARY | O_NOFOLLOW | O_NONBLOCK | O_NOCTTY | O_CLOEXEC` using `getattr(os, name, 0)` for flags absent on Windows. Functions: `parse_args`, `resolve_root` (strict realpath, must be a directory), `validate_metadata(data) -> dict`, `hash_slot(root_real, rel) -> dict`, `check(root_real, rels, metadata) -> dict`, `render` (raises above `LINE_BOUND`, as in the reducer), `_read`, `main(argv, stdin=None, stdout=None)`.

`main` order: bad argv gives `USAGE` without reading stdin; bad root gives `ROOT_INVALID`; stdin longer than 4,096 gives `INPUT_CAP` before parsing and before any file is touched; then `check` and `render`. Exception and exit-code policy identical to the reducer.

**Argv grammar.**

```
ARGV := ROOT REL{1,8}
ROOT := absolute per os.path.isabs, 1 to 1,024 chars, no NUL
REL  := SEG ("/" SEG)*        total 1 to 256 chars
SEG  := [A-Za-z0-9._-]{1,255}  and SEG not in {".", ".."}
```

Backslashes, empty segments, leading slash, drive letters, NUL, duplicate RELs, zero or more than eight RELs are all `USAGE`. Path syntax is the caller's contract and fails the whole run. Filesystem findings are per slot. RELs fill slots `f0`..`f{n-1}` in argv order; the remaining slots are `UNUSED`.

**Metadata stdin grammar.** The production shape with no production name:

```
INPUT    := HEADLINE KVLINE KVLINE
HEADLINE := [0-9a-f]{40} LF
KVLINE   := ("ActiveState=" VALUE | "StandardOutput=" VALUE) LF    each key once, either order
VALUE    := one or more bytes other than LF and "="
```

`format_ok` is false when input is not strict ASCII, lacks a final LF, has other than three lines, repeats or misuses a key, or a line has no `=`. Then all value fields are null and all `_ok` flags false. Otherwise `head` is the fullmatched 40-hex or null, `active_state` and `standard_output` are the value if it is a member of the fixed set, else null with the flag false. Output values are set members or null, never raw text.

**Per-slot hashing, containment and identity.** In this order; the first failing step sets the slot status. A slot reports `sha256` only on `OK`; every other status carries `sha256: null` (Fold 6).

1. `full = os.path.join(root_real, *segments)`.
2. `real = os.path.realpath(full, strict=True)`. `FileNotFoundError` gives `MISSING`; other `OSError` gives `UNREADABLE`; `real != full` gives `LINK`. This rejects a symlink, junction or reparse point at any component and therefore any escape from the root before the content is opened.
3. `pre = os.lstat(full)`: `S_ISLNK` gives `LINK`; not `S_ISREG` gives `NOT_REGULAR`; size over cap gives `TOO_LARGE`.
4. `fd = os.open(full, OPEN_FLAGS)`: `ELOOP` gives `LINK`; `ENOENT` gives `MISSING`; `EISDIR` gives `NOT_REGULAR`; other `OSError` gives `UNREADABLE`. Every later path closes `fd` in `finally`.
5. `st = os.fstat(fd)`: not `S_ISREG` gives `NOT_REGULAR`; size over cap gives `TOO_LARGE`; `(st_dev, st_ino)` differing from `pre` gives `CHANGED`.
6. **Fold 6, bounded read.** `total = 0`; loop at most `READ_ITERATIONS` times: `chunk = os.read(fd, min(CHUNK, FILE_CAP + 1 - total))`; stop at EOF; feed the chunk to the running sha256; `total += len(chunk)`; stop when `total == FILE_CAP + 1`. The cumulative read never exceeds `FILE_CAP + 1` bytes. `total > FILE_CAP` gives `TOO_LARGE` with no digest (the extra byte is the sentinel: an observed read total above the cap never yields a digest). Iteration cap reached gives `UNREADABLE`, no digest. `total != st.st_size` gives `CHANGED`, no digest.
7. `post = os.fstat(fd)`: any difference in `(st_dev, st_ino, st_size, st_mtime_ns)` from `st` gives `CHANGED`, no digest.
8. Slot is `OK` with 64-lowercase-hex `sha256` and `size == total`.

**What the slot checks observe (Fold 5, narrowed).** The slot reports `CHANGED` exactly when one of three observations disagrees: the byte count actually read versus the size reported by the post-open `fstat`; the `(st_dev, st_ino)` identity of the pre-open `lstat` versus the post-open `fstat`; the `(st_dev, st_ino, st_size, st_mtime_ns)` tuple of the post-open `fstat` versus the post-read `fstat`. Nothing more is claimed. In particular there is no consistent-snapshot guarantee: a same-size in-place rewrite that also preserves `st_mtime_ns` is not detected, content that changes between the two `fstat` calls without changing size or `st_mtime_ns` is not detected, and an adversary racing the filesystem between the `realpath` check and `os.open` is not defended against on any platform. `LINK` is reported for a link or escape present at the pre-open checks and, on POSIX, for a leaf symlink present at open time via `O_NOFOLLOW`. On Windows `O_NOFOLLOW` does not exist, so leaf-link rejection rests on the pre-open checks only. The Windows run is a discovery check; Linux CI is mandatory for the link and special-file cases.

**Output schemas.** Singleton for `USAGE`, `ROOT_INVALID`, `INPUT_CAP`, `INTERNAL_ERROR`. Full schema for `OK` and `FAILED`: `status`; four booleans `format_ok`, `head_ok`, `active_state_ok`, `standard_output_ok`; `head` (40-hex or null); `active_state` and `standard_output` (set member or null); `files` with exactly `f0`..`f7`, each `{"status": SLOT_STATUS, "sha256": 64-hex or null, "size": int or null}`. `status` is `OK` iff all four booleans are true and every used slot is `OK`. Worst case is eight `OK` slots plus the longest set members, asserted at most `LINE_BOUND`, pinned at 4,096.

**Static contracts.** Import allowlist as above. The only `os` attributes referenced are `open`, `read`, `close`, `fstat`, `lstat`, `path.join`, `path.isabs`, `path.realpath`, `path.isdir` and `O_*` constants. No builtin `open` at all. No `O_WRONLY`, `O_RDWR`, `O_CREAT`, `O_TRUNC`, `O_APPEND` name anywhere. No `print`, no forbidden builtins, no production literals including `/root/gecko-alpha` and `gecko-pipeline.service`, no CR bytes.

## Consumer rule, tests and falsifiers

**`usable_envelope(returncode, line, schema) -> (bool, reason)`** in `tests/test_receipt_inventory_envelope.py`. Returns `(True, "OK")` only when every check passes, else the first failing fixed reason in this order. Fold 2 additions are marked.

| Order | Check | Reason |
|---|---|---|
| 1 | **Fold 2:** `type(returncode) is int` (rejects bool, float, str, None) and `returncode == 0` | `RETURNCODE` |
| 2 | bytes, ends with LF, exactly one LF | `NOT_ONE_LINE` |
| 3 | at most 131,072 bytes | `LINE_TOO_LONG` |
| 4 | duplicate-rejecting parse finds a duplicate | `OUTER_DUPLICATE_KEY` |
| 5 | parse failure or not a dict | `OUTER_JSON` |
| 6 | key set equals the supervisor's 17 `STATUS_KEYS` | `OUTER_KEYS` |
| 7 | types: `status` str; `exit_code`, `pgid`, `inner_exit`, `reaped`, `kills`, `signals_received`, `teardowns`, `dropped` are `type(...) is int`; `inner_elapsed`, `total_elapsed` are `type(...) in (int, float)` and **Fold 2:** `math.isfinite`; `cleanup_proof`, `overflow` are `type(...) is bool`; `signal_phase`, `error`, `survivors` are `None`; `output` str | `OUTER_TYPES` |
| 8 | `status=="OK"`, `exit_code==0`, `inner_exit==0`, `cleanup_proof is True`, `dropped==0`, `overflow is False`, `reaped==0`, `kills==0`, `signals_received==0`, `teardowns==1`, `pgid>1`, `0<=inner_elapsed<=total_elapsed` | `OUTER_INVARIANTS` |
| 9 | `base64.b64decode(output, validate=True)` | `OUTPUT_BASE64` |
| 10 | decoded ends with LF, exactly one LF | `INNER_NOT_ONE_LINE` |
| 11 | decoded at most 4,096 bytes | `INNER_TOO_LONG` |
| 12 | inner duplicate key | `INNER_DUPLICATE_KEY` |
| 13 | inner parse failure or not dict | `INNER_JSON` |
| 14 | key set equals `reducer.OK_KEYS` or `meta.FULL_KEYS` per `schema`; nested `events`, `keys`, `observed_span`, `files` and each slot have exactly their fixed keys; any other `schema` raises `ValueError` as a caller bug | `INNER_KEYS` |
| 15 | inner types per the schema tables with `type(x) is int` for every integer, `type(x) is bool` for every boolean, `str` or `None` where nullable | `INNER_TYPES` |
| 16 | inner `status == "OK"` | `INNER_STATUS` |
| 17 | **Fold 2, reducer domains:** `0 <= bytes <= 2_097_152`; `0 <= records <= 200`; every counter, `unknown_events`, `unknown_keys`, every `events[*]` and `keys[*]` in `0..records` | `INNER_DOMAIN` |
| 18 | **Fold 2, reducer conservation:** `records == sum(counters) + sum(events.values()) + unknown_events`; every `keys[*] <= sum(events.values())`; `unknown_keys <= sum(events.values())` | `INNER_CONSERVATION` |
| 19 | **Fold 2, saturation equivalence:** `saturated == (records == 200)` | `INNER_SATURATED` |
| 20 | **Fold 2, span:** `first` and `last` both `None` or both strings that fullmatch `[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z`, parse with `datetime.strptime` in UTC, and satisfy `first <= last`; if both `None` then `sum(events.values()) + unknown_events + message_nonstr + message_nonjson + event_invalid == 0`; if both strings then `records - malformed - ts_invalid - out_of_window >= 1` | `INNER_SPAN` |
| 21 | **Fold 2, META flags:** `format_ok`, `head_ok`, `active_state_ok`, `standard_output_ok` all `True` | `INNER_META_FLAGS` |
| 22 | **Fold 2, META values:** `head` fullmatches `[0-9a-f]{40}`; `active_state in ACTIVE_STATES`; `standard_output in STANDARD_OUTPUTS` | `INNER_META_VALUES` |
| 23 | **Fold 2, META slots:** there is an `n` in `1..8` such that slots `f0`..`f{n-1}` each have `status == "OK"`, `sha256` fullmatching `[0-9a-f]{64}`, and `type(size) is int` with `0 <= size <= FILE_CAP`; and slots `f{n}`..`f7` each have `status == "UNUSED"`, `sha256 is None`, `size is None` | `INNER_SLOTS` |

Checks 17 to 20 apply to `schema="reducer"`; checks 21 to 23 apply to `schema="meta"`. The `reaped==0`, `kills==0` invariants follow from supervisor `_status` at `scripts/receipt_inventory_supervisor.py:289-296`, where `OK` is unreachable after any sweep. `teardowns==1` follows from the cleanup phase always running once after a spawn. The module loads the scripts with the `load()` pattern from `tests/test_receipt_supervisor_contracts.py:33-38` and additionally pins each schema constant, `LINE_BOUND`, `BYTE_CAP`, `RECORD_CAP` and `FILE_CAP` to literals so script drift fails a test rather than moving the oracle.

**Reducer tests** (`tests/test_receipt_inventory_reducer.py`, cross-platform, in-process via `BytesIO` and via `[sys.executable, "-I", "-S", REDUCER, ...]` with `timeout=60`):

- `test_canaries_never_appear`: canaries in allowlisted keys, unknown keys, unknown events, non-JSON MESSAGE, outer fields, bad timestamps. Kills any value copied to output.
- `test_duplicate_outer_key_counted_and_not_parsed`, `test_duplicate_message_key_counted`: kill hook removal in either layer.
- `test_message_list_number_null_object_are_nonstr`: kills a decoder for the list form.
- `test_message_nonjson_and_nondict`, `test_event_missing_nonstr_unknown`: the latter also asserts an unknown-event record's keys are not counted.
- **Fold 1 discriminators:** `test_event_only_message_has_no_unknown_keys` (message is exactly `{"event": <allowlisted>}`: `unknown_keys == 0`, all `keys[*] == 0`); `test_known_keys_only_has_no_unknown_keys` (event plus a subset of the 11: `unknown_keys == 0`, the subset counted); `test_one_unknown_key_counts_once` (event, the 11, and one foreign key: `unknown_keys == 1`; two foreign keys in one record still `1`). The mutant that treats `event` as unknown fails the first; the mutant that drops the check fails the third.
- `test_window_boundaries`: start in, start-1 out, end-1 in, end out. Kills `<` to `<=`.
- `test_ts_forms`: missing, empty, non-digit, signed, 20 digits, float, fullwidth digit all `ts_invalid`; 19 digits and leading zeros accepted. Kills an `isdigit` substitution.
- `test_cr_is_malformed`: kills removal of the CR check, since json would otherwise parse it.
- **Fold 3:** `test_unterminated_tail_is_malformed_and_parser_not_called`: `_loads` replaced by a counting shim; input of one valid terminated record followed by a valid unterminated record gives `records == 2`, `malformed == 1`, one allowlisted event, and the shim was called exactly twice, once with the terminated outer record text and once with its inner MESSAGE text, never with the tail; tail-only valid JSON gives `malformed == 1` and zero shim calls; `b""` gives zero records; `b"   "` gives one malformed with zero shim calls; `b"\n"` gives one malformed with one shim call. The mutant that parses the tail fails the call-count assertion.
- `test_byte_cap_exact`, `test_record_cap_exact`: exact boundary pairs. The 201st record is garbage that must not appear in any counter, proving the LF count precedes parsing.
- `test_overcap_stdin_subprocess_exits`: 3 MiB on stdin, returncode 0, one `OVERFLOW` line, within timeout.
- `test_forced_exception_is_singleton_internal_error`: `_loads` replaced by a `RuntimeError` raiser gives exactly the singleton line; a `KeyboardInterrupt` raiser propagates. Kills widening to `BaseException`.
- `test_nan_infinity_big_int_long_float_rejected`, `test_deep_nesting_is_malformed`: hooks and `RecursionError` classification.
- `test_ok_schema_exact`, `test_singleton_schemas_exact`, `test_usage_forms` (including proof that stdin is not read on `USAGE` via a `read` that raises).
- `test_conservation_holds_on_mixed_input`: a fixture exercising every counter, several events and unknown events satisfies the conservation law.
- `test_max_end_us_matches_datetime_max`, `test_iso_is_27_chars_and_platform_independent`, `test_observed_span_is_min_max_over_in_window`.
- **Fold 4:** `test_line_bound_constant_and_worst_case`: asserts `module.LINE_BOUND == 4096` literally; a max-value render and a 200-record input spread across all 13 events with all 11 keys are both at most `LINE_BOUND` and at most 4,096 and exactly one LF; `render` of a result whose serialized form exceeds `LINE_BOUND` raises, and through `main` yields the singleton `INTERNAL_ERROR`. Raising or removing the constant fails this test.
- `test_seeded_fuzz`: `random.Random(20260916)`, 300 iterations, per-iteration unique canaries, no exception escapes `main`, one line under bound, conservation law holds on every `OK` result.
- `test_output_uses_lf_only_on_windows`, `test_exit_code_policy`, `test_static_contracts`.

**META tests** (`tests/test_receipt_inventory_meta.py`):

- `test_two_small_files_ok`, `test_head_forms`, `test_state_values` (including a `file:/x` form giving null), `test_format_forms`, `test_input_cap` (4,097 bytes gives the singleton and no `lstat` call occurs, proven by a recording shim), `test_usage_forms`, `test_root_invalid`.
- `test_missing_empty_and_oversized`: empty file hashes fine; 16 MiB + 1 gives `TOO_LARGE` with `sha256 == None`; exactly 16 MiB is `OK`.
- **Fold 6:** `test_read_never_exceeds_cap_plus_one`: an `os.read` shim records every requested length and the cumulative bytes returned; start with a file of `FILE_CAP` bytes, then append `CHUNK` bytes immediately after the real initial post-open `fstat` returns (the test shim returns that original within-cap stat), so the actual read path is reached; every request is at most `min(CHUNK, FILE_CAP + 1 - total)`, the cumulative total is exactly `FILE_CAP + 1`, status is `TOO_LARGE`, `sha256 is None`. The mutant that reads a full `CHUNK` past the cap fails the request-length assertion.
- `test_canary_file_not_read`: a canary in root but not in the list. Its bytes and digest never appear and a shim over `os.open`, `os.lstat`, `os.path.realpath` records no call with its path.
- `test_leaf_symlink_is_link_before_open`, `test_directory_symlink_component_is_link`: pre-open path checks; Linux mandatory; Windows skips when `os.symlink` raises. `test_directory_is_not_regular`: pre-open `lstat`.
- **Fold 4, open-time guards reached under controlled replacement (Linux only).** These tests exist because the pre-open checks would otherwise stop a symlink or FIFO before `os.open`, leaving `O_NOFOLLOW` and `O_NONBLOCK` untested.
  - `test_o_nofollow_rejects_leaf_symlink_when_pre_checks_pass`: on disk, `rel` is a symlink to a regular file inside root. The test shims `os.path.realpath` to return `full` unchanged and shims `os.lstat` to return `os.stat(target)` (following, so `S_ISREG` and identity match the target). `hash_slot` therefore reaches the real `os.open` with the real `OPEN_FLAGS`. Expected `LINK` via `ELOOP`, `sha256 is None`. Under the mutant that drops `O_NOFOLLOW`, open follows the link, `fstat` is regular and matches the shimmed identity, and the slot returns `OK` with the target's digest, so the assertion fails.
  - `test_o_nonblock_returns_on_fifo_when_pre_checks_pass`: on disk, `rel` is a FIFO (`os.mkfifo`) with no writer. Same two shims, with `os.lstat` returning the stat of a sibling regular file so `S_ISREG` passes. Because a missing `O_NONBLOCK` would block `os.open` forever, the call runs in a disposable subprocess: a driver written under `tmp_path` that loads the module, installs the shims, calls `hash_slot`, and prints the slot JSON. The test uses `subprocess.Popen` then `communicate(timeout=5)`; on `TimeoutExpired` it calls `kill()`, then `communicate(timeout=5)` to reap, and fails with a fixed message. Expected within the timeout: `NOT_REGULAR` from the post-open `fstat`, `sha256 is None`. Under the mutant that drops `O_NONBLOCK`, the driver never returns, is killed and reaped, and the test fails. The suite cannot hang either way.
  - `test_post_open_fstat_rejects_nonregular`: same FIFO driver with `O_NONBLOCK` present proves step 5 is what reports `NOT_REGULAR`; the mutant that removes the post-open `S_ISREG` check attempts to read a FIFO with `O_NONBLOCK` and reports `UNREADABLE` or `CHANGED` rather than `NOT_REGULAR`, so the exact status assertion fails.
- **Fold 5, observed-change tests, each naming the single observation it exercises:** `test_byte_count_mismatch_is_changed` (`os.read` shim appends one byte beyond `st_size`: `CHANGED`); `test_identity_mismatch_is_changed` (`os.fstat` shim returns a different `st_ino` at the post-open call: `CHANGED`); `test_size_or_mtime_drift_is_changed` (`os.fstat` shim returns changed `st_size` at the post-read call, then separately changed `st_mtime_ns`: `CHANGED` in both); `test_growth_past_cap_is_too_large` (file of exactly `FILE_CAP` grows by one byte during the read: `TOO_LARGE`, not `OK`); `test_read_iteration_cap` (`os.read` returning one byte per call: `UNREADABLE` after `READ_ITERATIONS`). A same-size, same-`mtime_ns` rewrite is documented as undetected and has no test claiming otherwise.
- `test_full_and_singleton_schemas_exact`, `test_line_bound_constant_and_worst_case` (asserts `module.LINE_BOUND == 4096` and the eight-slot worst case is under it), `test_forced_exception_is_singleton_internal_error`, `test_static_contracts`.

**Envelope tests.** Pure negative oracle, one test per contradiction, each mutating exactly one thing in a known-good synthetic envelope and asserting the exact reason. Fold 2 requires each contradiction to be independently tested; the case list:

- `RETURNCODE`: 11; `True`; `0.0`; `"0"`; `None`.
- `NOT_ONE_LINE`, `LINE_TOO_LONG`, `OUTER_DUPLICATE_KEY`, `OUTER_JSON`, `OUTER_KEYS` (missing, extra).
- `OUTER_TYPES`: `cleanup_proof: 1`; `exit_code: 0.0`; `inner_elapsed: NaN`; `inner_elapsed: Infinity`; `total_elapsed: "1"`; `error: ""`.
- `OUTER_INVARIANTS`: `dropped: 1`; `overflow: true`; `reaped: 1`; `kills: 1`; `teardowns: 0`; `pgid: 1`; `OK` with `exit_code: 3`; `inner_elapsed > total_elapsed`.
- `OUTPUT_BASE64`, `INNER_NOT_ONE_LINE`, `INNER_TOO_LONG`, `INNER_DUPLICATE_KEY`, `INNER_JSON`, `INNER_KEYS` (missing, extra, a nested `events` with a 14th name, a slot with an extra key), `INNER_TYPES` (`saturated: 0`, `records: 1.0`, `bytes: true`, `sha256: 5`), `INNER_STATUS` (`OVERFLOW`, `FAILED`).
- `INNER_DOMAIN`: `bytes: 2097153`; `records: 201`; `malformed: records + 1`; one `events[*]` above `records`; negative counter.
- `INNER_CONSERVATION`: counters summing to one less than `records`; `keys[ledger_id]` above `sum(events)`; `unknown_keys` above `sum(events)`.
- `INNER_SATURATED`: `records: 200` with `saturated: false`; `records: 199` with `saturated: true`.
- `INNER_SPAN`: `first` string with `last` null; malformed ISO; `first > last`; both null with one allowlisted event; both strings with every record `malformed`.
- `INNER_META_FLAGS`: each of the four flags false, one at a time.
- `INNER_META_VALUES`: uppercase `head`; 39-hex; `active_state: "running"`; `standard_output: "file:/x"`.
- `INNER_SLOTS`: zero used slots; `f1` used while `f0` `UNUSED`; a used slot `MISSING`; a used slot with 63-hex; a used slot with `size: -1`; an `UNUSED` slot carrying a digest. A ninth-slot key is a separate `INNER_KEYS` case, rejected at step 14 before slot invariants.
- bad `schema` raises `ValueError`.

Linux-only cases under `skipUnless(sys.platform == "linux")`, with the prerequisite check from `tests/test_receipt_inventory_timeout.py:1259-1268` and reusing that module's `assert_empty` and `guarded_group` as the survivor oracle:

- Round trip `OK`: supervisor with `--pgid-file` in `tmp_path`, then `timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c` around `cat FIXTURE | head -c 2097153 | <sys.executable> -I -S REDUCER START END`, via `subprocess.run(timeout=20)`. Asserts returncode 0, helper true, decoded counts equal the fixture's known counts, group empty.
- Cap+1 fixture: supervisor `OK`, inner `OVERFLOW`, helper false with `INNER_KEYS`.
- Cap + 1 MiB fixture: `COMMAND_FAILED`, null output, helper false, `inner_exit` recorded. This pins the pipefail interaction.
- Forced raise: a fault runner written under `tmp_path` that loads the reducer, replaces `_loads`, calls `main`. Inner `INTERNAL_ERROR`, helper false. Mirrors the precedent of `tests/receipt_supervisor_faults.py` without committing a new file.
- META round trip: a valid three-line fixture piped into META with a `tmp_path` root and two files; helper true with `schema="meta"`, digests match.

**Mutation checklist for the review file.** Hook removed: the duplicate-key tests plus the envelope duplicate cases. Window `<=`: boundaries test. Byte cap off by one: exact test plus cap+1 round trip. Record cap off by one: exact test. Allowlist widened: schema-exact tests with literal name sets. Value leaked on one key: canary and fuzz tests. `BaseException`: forced-exception test. `LINE_BOUND` raised, removed or not enforced in `render`: the line-bound constant test in each module. CR check removed: CR test. Tail parsed: parser-not-called test. `event` counted as unknown key: event-only test. `unknown_keys` check dropped: one-unknown-key test. `isdigit` substitution: ts and usage tests. META read past cap+1: read-never-exceeds test. META sentinel removed: growth test. Post-hash fstat removed: size-or-mtime drift test. Identity compare removed: identity mismatch test. realpath comparison removed: directory-symlink test. `O_NOFOLLOW` dropped: the controlled-replacement symlink test. `O_NONBLOCK` dropped: the disposable-subprocess FIFO test, which kills and reaps the driver and fails. Post-open `S_ISREG` removed: the exact-status FIFO test. Helper accepting a bool, float or string returncode, a non-finite elapsed, an out-of-domain count, a broken conservation sum, a wrong `saturated`, an unpaired or unordered span, a false META flag, an invalid META value, or a non-contiguous or non-`OK` slot prefix: the corresponding oracle case.

**Verification for the build run**, local Windows, in order: `py_compile` on both scripts; `python -m unittest discover -s tests -p "test_receipt_inventory_*.py" -v` with Linux cases skipping; pytest on the three new modules plus the contract module and the six scanning lints named in the plan; `git diff --check`. New files must be created with `Path.write_bytes` to keep LF, per the 2026-05-13 tooling lesson. Then the full Linux `test` job on the PR head with the count baseline, two independent PR reviews, the mutation table completed in the review file, and a new clearance file in the shape of `.reviewers/590.toml` on the final SHA.

**Not in this design.** No production command, transport, host write licence, identity preflight, unit name, host path, window value, schedule, flag, account or vendor. No change to the supervisor, fault runner, harness, contract tests or CI workflow. If the build finds it must touch any of those, that is a plan amendment. Build does not start until both design reviewers return terminal approval, including explicit acceptance of the Fold 7 schema amendment.

## Accepted implementation review amendments

Both PR reviewers approved08063e64. META additionally imports stdlib errno as stated above. Consumer elapsed integers are limited to [-2^53,2^53] to avoid implicit float conversion overflow; finite floats retain normal invariant checks. Four Linux-only mutation proofs run against temporary source copies in the existing META test module, requiring passing controls and expected assertion failures with bounded group cleanup. These amend only portable constants and local synthetic validation; no production or workflow scope expands.
