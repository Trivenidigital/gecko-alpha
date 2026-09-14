# DESIGN: Bounded suppression receipt inventory reducer

**New primitives introduced:** NONE. This design only describes a throwaway read-only reducer and the docs that use it. Nothing was written or run.

## Scope
This follows the committed plan at `8836b14a`, which both reviewers approved. It allows exactly one metadata command and one journal command. It never reads the DB, env or settings, config files, or directory listings, and it never exports raw logs.

- **Metadata command** (15-second outer timeout, one line per fact):
  - `git -C /root/gecko-alpha rev-parse HEAD`;
  - `sha256sum` of `scout/main.py`, `scout/outcome_ledger.py`, `scout/trading/signals.py` and `scout/trading/decision_events.py`;
  - `systemctl show gecko-pipeline.service -p ActiveState -p StandardOutput`.
- **Stop rule:** if a hash doesn't match `70462c16`, the journal result is recorded but not interpreted against source.

## Journal reducer (Python stdlib only)
- **Outer run:** `timeout -k 2 15 python3 -I -S -` with the script sent on stdin. It uses no site packages and no repo imports.
- **Fixed child argv** (a list, never a shell string): `/usr/bin/journalctl -u gecko-pipeline.service --since "2026-09-14 20:45:00 UTC" --until "2026-09-14 21:45:00 UTC" -n 200 -o json --no-pager -q`.
- **Child environment:** only `LC_ALL=C` and `SYSTEMD_PAGER=`.
- **Popen settings:** `stdin=DEVNULL`, `stdout=PIPE`, `stderr=DEVNULL`, `start_new_session=True`, `close_fds=True`.
- **Reading:**
  - Set the stdout fd to non-blocking and register it with `selectors.DefaultSelector`.
  - Use a monotonic 10-second deadline and read with `os.read(fd, 65536)`.
  - Keep a running byte total. Going past 2 MiB (2,097,152 bytes) gives status `OVERFLOW`.
  - Split complete lines on `\n` as they arrive. After 200 records, stop with status `RECORD_CAP`. Normally `-n 200` prevents that.
- **Kill and reap:** on overflow, timeout, record cap or a read error:
  - `os.killpg(pgid, SIGKILL)`, with `ProcessLookupError` ignored;
  - `proc.wait(timeout=2)`, then close the fd and selector.
  - A clean EOF also ends in `wait(timeout=2)`. If that wait fails, the fallback is killpg plus wait, with status `REAP_TIMEOUT`.
- **Window rule:** membership uses journald `__REALTIME_TIMESTAMP`, a decimal string of epoch microseconds.
  - A record counts only if `start <= ts < end`, with start = 20:45:00Z and end = 21:45:00Z.
  - `--until` is only a coarse prefilter. The strict end is enforced in the reducer, and out-of-window records are only counted.
  - `-n 200` keeps the most recent entries. If the cap is reached (`saturated=true`), the result covers only the tail of the interval, not the full hour. The output reports first and last in-window timestamps so the covered span is explicit.
- **Parsing each line:**
  1. `json.loads` the outer record. It must be a dict, or the line counts as malformed.
  2. `MESSAGE` must be a `str`. A byte array for non-UTF-8 content or a missing field counts as `message_nonstr`.
  3. `json.loads(MESSAGE)` must give a dict with `event` as a `str`. Otherwise the line counts as `message_nonjson` or `event_invalid`.
  4. If the event is in the allowlist, increment its count and count only the allowlisted field keys present. Other keys increment one integer per event, `unknown_key_count`.
  5. Any other event name increments `unknown_event_count` and is never stored or printed.
  6. A trailing partial line at EOF counts as malformed.
- **Output:** exactly one JSON line with fixed keys:
  - `status`, `child_exit`;
  - the counters `bytes`, `lines`, `records`, `in_window`, `out_of_window`, `malformed`, `message_nonstr`, `message_nonjson`, `event_invalid`, `unknown_event_count`;
  - `saturated`, and first and last in-window timestamps as ISO UTC formatted from the integer microseconds;
  - `events`, which contains every allowlisted name, including zeros, each with `count`, `keys{allowlisted key: count}` and `unknown_key_count`.
  - No value from any record is ever printed, including allowlisted keys like `token_id`.
- **Fixed status codes:** `OK`, `TIMEOUT`, `OVERFLOW`, `RECORD_CAP`, `SPAWN_ERROR`, `CHILD_NONZERO`, `READ_ERROR`, `REAP_TIMEOUT`, `INTERNAL_ERROR`. A top-level `except BaseException` prints only `{"status":"INTERNAL_ERROR"}`, with no exception text or traceback. The outer `timeout` exit code (124 or 137) is recorded by the caller as `OUTER_TIMEOUT`.
- **No retries.** Any status other than `OK` makes the result incomplete, and all dimensions stay `UNKNOWN`.

## Allowlists (exact)
- **Events (13):**
  - `ledger_emission_recorded`, `ledger_record_failed`, `ledger_record_skipped_db_closed`, `ledger_record_rollback_failed`;
  - `trade_decision_event_emitted`, `trade_decision_event_emit_failed`, `trade_decision_event_skipped_db_closed`, `trade_decision_event_rollback_failed`;
  - `ledger_label_pass`, `ledger_price_lookup_failed`, `ledger_coverage_check_failed`;
  - `trade_decision_events_pruned`, `volume_history_cg_pruned`.
- **Field keys (11, presence counts only):** `ledger_id`, `event_id`, `kind`, `surface`, `token_id`, `signal_type`, `decision`, `reason`, `site`, `timestamp`, `level`.
- **Excluded:** `error` and all other keys are counted in `unknown_key_count` and never named. The per-cycle summary is outside this fixed allowlist; source scout/trading/signals.py:201-205 does emit trade_volume_spikes_filtered with skipped_suppressed, but it is an aggregate, not a per-event attempt receipt.

## Synthetic adversarial checks (before any host run)
The checks run locally against a test copy of the reducer. The only difference is the child path constant, which points at stub executables. The test asserts that a diff of the two copies shows only that constant. Every case asserts:
- the output is one line under a fixed size bound;
- canary strings are absent;
- the status and counters are as expected;
- no zombie or orphan process remains (`os.waitpid` or `ps` on the stub's process group).

1. **Value leakage:** canary values in allowlisted keys (`token_id` set to an address-like canary, a `reason` containing a URL, an `error` with a traceback). None appear in the output.
2. **Unknown names:** an unknown event name and an unknown key, each carrying a canary. Only the counts change.
3. **Malformed messages:** `MESSAGE` that is non-JSON, a byte array, a nested JSON string, a list, or has a non-string or missing `event`, plus duplicate JSON keys, CRLF line endings, a partial last line, and an empty stream.
4. **Timestamp boundaries:**
   - exactly at start: in;
   - start minus 1µs: out;
   - end minus 1µs: in;
   - exactly at end: out;
   - missing, non-digit, negative, or larger than 20 digits: malformed.
5. **Record cap:** a stub printing 201 valid records gives `RECORD_CAP`, and the child is killed and reaped. Exactly 200 gives `saturated=true`.
6. **Overflow:** a single line with no newline, 3 MiB long, gives `OVERFLOW` with the byte count capped near the limit.
7. **Timeout:** a stub that sleeps silently, and a stub that trickles one byte per second, both give `TIMEOUT` at about 10 seconds. A stub that ignores SIGTERM is still killed by SIGKILL.
8. **Grandchild:** a stub that forks a grandchild holding the pipe open is ended by the process-group kill, and EOF follows.
9. **Stderr flood:** 10 MiB written to stderr does not block, because stderr goes to DEVNULL.
10. **Child failures:** non-zero child exit gives `CHILD_NONZERO`, and a missing binary gives `SPAWN_ERROR`.
11. **Outer timeout:** a reducer forced to hang (test-only) is ended by the outer 15-second `timeout -k 2`.
12. **Internal error:** a forced internal exception prints only `INTERNAL_ERROR`.

## Interpretation rules for findings
- **Receipt copies are not independent.** `ledger_emission_recorded` and `trade_decision_event_emitted` are written by the same writers after commit. They are not attempt records and can't be the D1 denominator. Neither carries the other store's ID, so they don't establish D2 identity.
- **Silent drops stay silent.** Returns on disabled flags (`signals.py:66-67`, `outcome_ledger.py:476-477`) leave no journal trace. D1 drop classes stay `UNKNOWN`.
- **Zero observations prove nothing about why.** A zero count doesn't show that an event is unavailable, filtered by level, disabled, idle or unretained. It only means nothing was observed in this capped tail slice.
- **Label and prune aggregates don't give lineage.** They don't establish D3 or D4 observation lineage.
- **Verdicts:** D1-D4 stay `UNKNOWN`. Candidate availability is not `ACCEPTED`. No `UNVERIFIABLE_HISTORICAL` verdict and no claim of full-hour or historical coverage.

## Files
`tasks/design_suppression_receipt_inventory_2026_09_14.md` (this design). The plan, findings, review doc, the `tasks/todo.md` checklist and the PR-owned reviewer metadata stay as committed in the plan.

**Rollback:** revert the docs. Nothing is deployed.
## Hermes-first analysis
| Domain | Hermes skill found? | Decision |
|---|---|---|
| Receipt/log inventory | Hub fetched 2026-09-14, catalog stayed loading: https://hermes-agent.nousresearch.com/docs/skills | Reuse standard read-only tools; no dependency |
| Runtime evidence | Existing Gecko contract/source | Reuse |

awesome-hermes-agent checked https://github.com/0xNyk/awesome-hermes-agent on 2026-09-14; general orchestration listings do not supply Gecko receipts. No exhaustive absence claim.