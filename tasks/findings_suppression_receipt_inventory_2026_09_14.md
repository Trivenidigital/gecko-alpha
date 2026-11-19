# Findings: suppression receipt inventory, source-only (2026-09-14)

**New primitives introduced:** NONE. Docs only.

**Status:** this run finished a source-only inventory of receipt candidates, plus the approved plan (`8836b14a`) and design (`d8625be4`). It did **not** run a runtime inventory, and it did **not** implement or test the reducer. No production journal read, metadata audit, hash check, config read, retention read, DB query or file listing took place. **Rollback:** revert the docs.

## Runtime identity (dated, limited)
The only runtime evidence is the 2026-09-14T21:37:54Z preflight: production HEAD `77751890c9f1f51ed348c365d4e7a5985ea2827d`, with `gecko-pipeline` and `gecko-dashboard` active. This run read no source hashes, `StandardOutput` setting, flags, log level or retention values. The source references below come from master `70462c16`. They are candidates only and can't prove how production actually behaves.

## Source receipt candidates (at `70462c16`)
- **`ledger_emission_recorded`** (`scout/outcome_ledger.py:590-596`): a debug event carrying `ledger_id`, `kind`, `token_id` and `surface`. The writer emits it after commit, so it is a copy of the receipt, not an independent attempt record.
- **Ledger failure events:**
  - `ledger_record_failed`, inner shape (`outcome_ledger.py:598-605`) and outer `site="dispatcher_suppressed"` shape (`scout/trading/signals.py:86-93`);
  - `ledger_record_skipped_db_closed` (`outcome_ledger.py:481`);
  - `ledger_record_rollback_failed` (`outcome_ledger.py:610`).
- **Silent drops:** the `LEDGER_SAMPLE_SUPPRESSED` return (`signals.py:66-67`) and the `LEDGER_ENABLED` return (`outcome_ledger.py:476-477`) log nothing, so those return sites emit no attempt record; independent upstream evidence was not inventoried.
- **Decision events:**
  - `trade_decision_event_emitted` (`scout/trading/decision_events.py:67-74`) carries only its own `event_id`, again as a copy written after commit.
  - The failure and skip events are at `decision_events.py:35-41`, `77-84` and `88-95`.
  - `_emit_dispatch_decision` passes no ledger ID (`signals.py:103-132`). No dedicated cross-store key appears in these inspected helpers.
- **Logging config:** structlog uses `JSONRenderer`, `BoundLogger` and `PrintLoggerFactory`, with no level filter in the source (`scout/main.py:2327-2336`). Whether debug events reach journald in production is unverified.
- **Label aggregates:** `ledger_label_pass` (`outcome_ledger.py:730`, `831`) and the lookup and coverage failure events (`195`, `305`) don't record which price observation was selected.
- **Retention in the source:**
  - `trade_decision_events` is pruned with a 45-day default (`scout/db.py:12749-12764`, `scout/config.py:882`).
  - `volume_history_cg` is pruned with a 10-day default (`db.py:12766-12782`, `config.py:909`).
  - The hourly prune calls log `*_pruned` events (`main.py:1864-1890`).
  - `price_cache` is upserted on `coin_id` (`db.py:12435-12439`).
  - The effective values in production were not read.

## Contract verdicts
| Dimension | Verdict | Reason |
|---|---|---|
| D1 attempts | `UNKNOWN` | Inspected success logs copy receipts, warnings describe failures and flag-return sites are silent; no independent denominator established |
| D2 identity | `UNKNOWN` | Each store logs its own ID; no shared key in inspected helpers/logs |
| D3 emission lineage | `UNKNOWN` | No observation ID/source time in the inspected candidate logs |
| D4 horizon lineage | `UNKNOWN` | Label events are aggregates; overwrite and prune behavior not verified in production |
| D5, D6 | Untouched | Out of scope |

Finding no observations would not mean receipts are unavailable. No `ACCEPTED` and no `UNVERIFIABLE_HISTORICAL`.

## Design review folds (captured)
- **Session escape:** a child in a separate session could escape the outer timeout. The fix is a single process group: `timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c` with no `setsid`, no background jobs, and all stderr to `/dev/null`.
- **Orphan risk:** catching `BaseException` in a spawning reducer could orphan the child. The fix: the reducer spawns nothing and catches only `Exception`, printing a fixed `INTERNAL_ERROR`. The outer timeout owns cleanup.
- **Parser hardening:**
  - input is bounded by `head -c 2097153`, and more than 2 MiB gives `OVERFLOW` with no parsing;
  - more than 200 records gives `RECORD_CAP`; exactly 200 gives `saturated`;
  - duplicate JSON keys are rejected in both the outer record and `MESSAGE`;
  - the window is `start <= ts < end` on `__REALTIME_TIMESTAMP`;
  - the output reports the observed span, not coverage;
  - the allowlists are fixed;
  - the metadata command emits only validated JSON.

## Verification blocker
Linux process-group cleanup could not be validated on this workstation:
- `wsl --list --quiet` listed only `docker-desktop`, and `wsl --exec python3` failed with "no such file".
- The first `docker image ls` failed because the `dockerDesktopLinuxEngine` pipe was missing. After Docker Desktop was started hidden, a second `docker image ls` got no answer for more than 115 seconds.
- Only that read-only probe (PID 20412), which this session started, was terminated.
- Docker daemon health was not diagnosed, and no image was pulled.

Following the design's blocker path, the production journal/metadata inventory did not run; only the earlier identity/service preflight ran. The inventory read budget was not widened.

## Next action
1. Get a healthy local or non-production Linux runner.
2. Build the reducer and metadata scripts and pass the pure tests on Windows.
3. Run the process-group timeout test on that runner.
4. Only then run the approved bounded production reads.

Read-only engineering needs no operator authorization. Separate gates for authentication, paid calls and trading are unchanged.

**User action, only if Docker startup stays unhealthy:** restore Docker Desktop, or provide a working Linux runner such as a WSL distro with `python3`.

This blocker applies only to runtime validation for this inventory. It doesn't block other engineering work, and this report makes no claim about backlog state.

**Boundary:** no code, schema, writer, retention, activation, UI, ranking or deployment change.