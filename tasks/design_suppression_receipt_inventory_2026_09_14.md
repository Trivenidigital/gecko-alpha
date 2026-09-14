# DESIGN (replacement): Bounded suppression receipt inventory

**New primitives introduced:** NONE. Nothing was written or run.

## Hermes-first analysis
| Domain | Hermes skill found? | Decision |
|---|---|---|
| Receipt/log inventory | Hub fetched 2026-09-14, catalog stayed loading: https://hermes-agent.nousresearch.com/docs/skills | Reuse standard read-only tools; no dependency |
| Runtime evidence | Existing Gecko contract/source | Reuse |

awesome-hermes-agent checked https://github.com/0xNyk/awesome-hermes-agent on 2026-09-14; general orchestration listings do not supply Gecko receipts. No exhaustive absence claim.

## Process model
The design never starts a child in its own session and never spawns from Python. Both host commands run inside one outer bound:

`timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c '<pipeline>' 2>/dev/null`

- **Process group:** GNU `timeout` (without `--foreground`) makes itself the process-group leader and signals the whole group when time runs out. Job control is off (`+m`, non-interactive), so every pipeline stage stays in that group. No stage calls `setsid` or `nohup`, or runs in the background.
- **Timeout:** TERM at 10 seconds, then KILL 2 seconds later. This bounds the whole pipeline.
- **Stderr:** every stage has stderr sent to `/dev/null`.
- **Exit codes:** the caller records the pipeline exit code. Exit 124 or 137 means `OUTER_TIMEOUT`, and any partial stdout is discarded.

## Journal pipeline
`/usr/bin/journalctl -u gecko-pipeline.service --since "2026-09-14 20:45:00 UTC" --until "2026-09-14 21:45:00 UTC" -n 200 -o json --no-pager -q | head -c 2097153 | python3 -I -S -c "$REDUCER"`

- **Reducer input:** the reducer is passed as the `-c` argument. Its stdin is data only, and it spawns nothing.
- **Reading:** it reads `sys.stdin.buffer` until EOF, at most 2,097,153 bytes (2 MiB plus one sentinel byte).
- **Status order:**
  1. More than 2,097,152 bytes gives `OVERFLOW`. Nothing is parsed and no counts are emitted.
  2. More than 200 newline-delimited records gives `RECORD_CAP`. Nothing is parsed.
  3. Exactly 200 records gives `saturated=true`.
- **Pipeline exit:** under pipefail, a SIGPIPE from `head` makes the pipeline exit non-zero. The result counts as usable only when the reducer status is `OK` and the pipeline exits 0. Anything else is incomplete.
- **Parsing each record:**
  - Parse the outer JSON with an `object_pairs_hook` that rejects duplicate keys. It must be a dict.
  - `__REALTIME_TIMESTAMP` must be 1-19 decimal digits.
  - Window: `start <= ts < end`, where start = 20:45:00Z and end = 21:45:00Z in epoch microseconds.
  - `MESSAGE` must be a `str`. Parse it with the same duplicate-rejecting hook; it must be a dict with `event` as a `str`.
  - Failures go into fixed counters: `malformed`, `duplicate_key`, `ts_invalid`, `out_of_window`, `message_nonstr`, `message_nonjson`, `event_invalid`.
- **Allowlists (unchanged):**
  - The 13 candidate event names: `ledger_emission_recorded`, `ledger_record_failed`, `ledger_record_skipped_db_closed`, `ledger_record_rollback_failed`, `trade_decision_event_emitted`, `trade_decision_event_emit_failed`, `trade_decision_event_skipped_db_closed`, `trade_decision_event_rollback_failed`, `ledger_label_pass`, `ledger_price_lookup_failed`, `ledger_coverage_check_failed`, `trade_decision_events_pruned`, `volume_history_cg_pruned`.
  - Keys, presence only: `ledger_id`, `event_id`, `kind`, `surface`, `token_id`, `signal_type`, `decision`, `reason`, `site`, `timestamp`, `level`.
  - Unknown events and keys are counted and never printed. No values are ever printed.
- **Output:** one JSON line with a fixed schema: `status`, `bytes`, `records`, the counters above, `saturated`, `observed_span` {first, last in-window ISO UTC}, and `events`.
  - `observed_span` is only the span of observed entries. It is never reported as coverage of the hour.
- **Errors:** `except Exception` prints only `{"status":"INTERNAL_ERROR"}`. Signals simply end the process, and the outer timeout owns cleanup.

## Metadata command
The same outer wrapper runs `{ git -C /root/gecko-alpha rev-parse HEAD; systemctl show gecko-pipeline.service -p ActiveState -p StandardOutput; } | head -c 4097 | python3 -I -S -c "$META"`.

The META script:
- validates HEAD as 40 lowercase hex characters;
- checks `ActiveState` and `StandardOutput` against fixed value sets;
- computes sha256 in-process with `hashlib` for the four source files, reading at most 16 MiB each;
- emits validated JSON, or a fixed status code with no raw text.

## Validation
**Local pure-reducer tests (Windows):** feed byte strings to the reducer and cover:
- canary values in allowlisted keys, unknown events and unknown keys (none printed);
- duplicate keys in the outer record and in `MESSAGE`;
- a non-string or non-JSON `MESSAGE`, and a missing or invalid `event`;
- timestamps at start (in), start-1µs (out), end-1µs (in), end (out), plus missing, non-digit and 20-digit values;
- a partial last line, CRLF line endings, and empty input;
- exactly 2,097,152 bytes (parsed) versus 2,097,153 (`OVERFLOW`, no counts);
- 200 records (`saturated`) versus 201 (`RECORD_CAP`, no counts);
- a forced exception (only `INTERNAL_ERROR` printed);
- a fixed maximum output size.

The same inputs go to META for its validation paths.

**Linux cleanup test (separate, only on a non-production Linux test machine if one is available):** run the exact wrapper with stub producers that:
- sleep silently;
- ignore SIGTERM;
- start a descendant holding the pipe;
- flood stderr.

The test asserts exit 124 or 137 within about 12 seconds and that `pgrep -g <pgid>` finds no survivors. If no such machine is available, the findings say cleanup is unvalidated and never claim it.

## Blocker path
If Linux cleanup can't be validated this run, the correct output is a findings-only report with no execution. It states the exact blocker: "Linux process-group timeout cleanup not validated on a test host." It does not widen the runtime read budget, pick a different mechanism, or claim that all engineering is blocked. Other docs and source work continue.

## Interpretation (unchanged)
- `ledger_emission_recorded` and `trade_decision_event_emitted` are receipt copies written after commit by the same writers. They are not independent attempts and not cross-store keys.
- Returns on disabled flags leave no trace.
- Zero observations do not mean a receipt is unavailable, filtered, disabled or unretained.
- D1-D4 stay `UNKNOWN`. No `ACCEPTED`, no `UNVERIFIABLE_HISTORICAL`.

**Rollback:** revert the docs.
