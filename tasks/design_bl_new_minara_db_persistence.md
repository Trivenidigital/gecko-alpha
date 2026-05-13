**New primitives introduced:** New SQLite table `minara_alert_emissions`; new `Database.record_minara_alert_emission(...)` helper; optional bounded persistence hook in `maybe_minara_command(...)`; mandatory journalctl backfill script `scripts/backfill_minara_alert_emissions.py`; parity watchdog `scripts/check_minara_emission_persistence.py` with ops wrapper `scripts/minara-emission-persistence-watchdog.sh`.

## Drift Check

| Check | Evidence | Verdict |
|---|---|---|
| Existing event | `scout/trading/minara_alert.py` emits `minara_alert_command_emitted` only to structlog | Extend this event with DB persistence |
| Existing DB schema | No `minara_alert_emissions` table or Minara-specific write helper | New table needed |
| Existing `tg_alert_log` | Tracks alert/cooldown outcomes, not Minara command emissions or paste acknowledgement | Do not reuse outcome enum |
| Existing D+14 criterion | `backlog.md` query expects `minara_alert_emissions` | This PR supplies the missing substrate |

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Minara command construction | Existing gecko-alpha code already constructs the operator command | Reuse existing code |
| SQLite telemetry persistence | None found in installed VPS Hermes skills/plugins or public Hermes hub | Build inline |
| Journalctl historical backfill | None found in installed VPS Hermes skills/plugins or public Hermes hub | Build project script |
| Persistence parity watchdog | None found in installed VPS Hermes skills/plugins or public Hermes hub | Build small local watchdog |
| Operator paste acknowledgement UI | None in scope | Defer column only |

Awesome-Hermes ecosystem check: checked the public Hermes skills hub, `0xNyk/awesome-hermes-agent`, and `NousResearch/hermes-agent-self-evolution`; no reusable capability covers this project-specific telemetry persistence. Installed VPS check covered the Hermes skill/plugin inventory available to `gecko-agent`; notification and webhook-adjacent skills do not provide gecko-alpha SQLite persistence or journalctl-to-DB parity checks.

Verdict: custom code is justified because this is local gecko-alpha audit persistence for an existing log event, not a reusable Hermes skill capability.

# BL-NEW-MINARA-DB-PERSISTENCE Design

## Objective

Persist Minara command-generation events to SQLite so the 2026-05-25 D+14 evaluation does not depend on journalctl retention.

## Semantics

`minara_alert_emissions` counts "Minara command delivered to the operator in a Telegram paper-trade alert." The `minara_alert_command_emitted` log is now emitted after Telegram delivery succeeds, so prepared-but-undelivered commands do not enter the durable denominator.

The D+14 query can additionally join `tg_alert_log` by logical `tg_alert_log_id` to split delivered vs dispatch-failed rows. That split is diagnostic, not the primary denominator.

## Schema

```sql
CREATE TABLE IF NOT EXISTS minara_alert_emissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_trade_id INTEGER REFERENCES paper_trades(id) ON DELETE RESTRICT,
    tg_alert_log_id INTEGER,
    signal_type TEXT NOT NULL,
    coin_id TEXT NOT NULL,
    chain TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    command_text TEXT,
    command_hash TEXT,
    command_text_observed INTEGER NOT NULL DEFAULT 0 CHECK (command_text_observed IN (0,1)),
    source TEXT NOT NULL CHECK (source IN ('live','journalctl_backfill')),
    source_event_id TEXT NOT NULL UNIQUE,
    emitted_at TEXT NOT NULL,
    operator_paste_acknowledged_at TEXT
);
```

Indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_minara_alert_emissions_emitted_at
    ON minara_alert_emissions(emitted_at);
CREATE INDEX IF NOT EXISTS idx_minara_alert_emissions_coin_id
    ON minara_alert_emissions(coin_id, emitted_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_minara_alert_emissions_tg_alert_log_id
    ON minara_alert_emissions(tg_alert_log_id)
    WHERE tg_alert_log_id IS NOT NULL;
```

`tg_alert_log_id` is a logical reference, not an FK, because `tg_alert_log` has existing table-rebuild migrations. A physical child FK would make future enum/check migrations dangerous.

`command_text` and `command_hash` are nullable because historical journalctl logs did not include the command or SPL address. Live rows set `command_text_observed=1`; historical rows set it to `0`.

## Migration

Add `_migrate_minara_alert_emissions_v1()` after `_migrate_narrative_scanner_v1()` in `Database.initialize()`.

Migration properties:
- `BEGIN EXCLUSIVE`
- create `paper_migrations` if missing
- idempotency gate: `paper_migrations.name = 'bl_minara_alert_emissions_v1'`
- create table + indexes
- post-assert required columns, the unique `source_event_id` index, and partial unique `tg_alert_log_id` index before stamping
- insert `paper_migrations` row
- insert `schema_version` row `(20260519, ..., 'bl_minara_alert_emissions_v1')`
- commit, rollback on exception

## Runtime Write

`Database.record_minara_alert_emission(...)`:
- requires initialized DB
- acquires `_txn_lock`
- hashes command text when present
- requires explicit `source_event_id` when `tg_alert_log_id` is absent
- uses plain `INSERT`
- commits under lock
- returns `True` when a row was inserted and `False` when a duplicate was ignored
- catches only known duplicate unique-key violations as idempotent duplicates; CHECK/NOT NULL/FK violations raise after rollback
- rolls back before releasing `_txn_lock` on any insert/commit exception or cancellation

Runtime split:
- `maybe_minara_command(...)` only prepares the copy-paste command and performs CoinGecko/Solana eligibility checks.
- `notify_paper_trade_opened(...)` sends the Telegram message first.
- Only after Telegram send succeeds, `notify_paper_trade_opened(...)` emits `minara_alert_command_emitted` and calls `persist_minara_alert_emission(...)`.

After successful Telegram delivery:
- missing DB/context -> `minara_alert_emission_persist_skipped`
- successful insert -> `minara_alert_emission_persisted`
- duplicate ignored -> `minara_alert_emission_persist_duplicate_ignored`
- lock acquisition timeout -> `minara_alert_emission_persist_timeout`
- exception -> `minara_alert_emission_persist_failed`

All persistence paths run after send and cannot delay or suppress Telegram delivery. If Telegram send is cancelled or fails, the pre-claimed `tg_alert_log` row is demoted and no live Minara emission row is written.

The timeout bounds only `_txn_lock` acquisition. Once the lock is acquired, the DB insert/commit is not cancelled with `asyncio.wait_for`, because cancelling an in-flight `aiosqlite` operation can leave shared connection state ambiguous.

`notify_paper_trade_opened(...)` passes `db`, `paper_trade_id`, `signal_type`, and pre-claimed `sent_row_id` as `tg_alert_log_id`.

## Backfill

`scripts/backfill_minara_alert_emissions.py` reads journalctl JSON lines and inserts historical rows:
- `source='journalctl_backfill'`
- `signal_type='unknown_historical_backfill'`
- `paper_trade_id=NULL`
- `tg_alert_log_id=NULL`
- `command_text=NULL`
- `command_hash=NULL`
- `command_text_observed=0`
- `source_event_id` from the log when present; otherwise fallback `journalctl:<timestamp>:<coin_id>:<chain>:<amount_usd>` for pre-PR historical rows

Backfill is mandatory before closing the backlog item. If journalctl rows are unavailable, the backlog closeout must say the denominator is partial and include the first DB row timestamp.

## Freshness SLO And Watchdog

This table is sparse by design; "no rows in 24h" is not itself a failure if no Minara command was emitted. The SLO is parity with the existing source of truth:

> For a checked journalctl window, every observed `minara_alert_command_emitted.source_event_id` must be present in `minara_alert_emissions`, with tolerance 0 by default.

Artifacts:
- `scripts/check_minara_emission_persistence.py` compares a pre-filtered journal file against the DB by `source_event_id` and exits nonzero on deficit.
- `scripts/minara-emission-persistence-watchdog.sh` collects recent journalctl events, fails closed if journalctl cannot be read, and sends a Telegram alert via the existing `.env` credentials when parity fails.
- `systemd/minara-emission-persistence-watchdog.service` and `.timer` run the watchdog hourly with `WorkingDirectory=/root/gecko-alpha`.

Recurring install after deploy:

```bash
cd /root/gecko-alpha
install -m 0644 systemd/minara-emission-persistence-watchdog.service /etc/systemd/system/
install -m 0644 systemd/minara-emission-persistence-watchdog.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now minara-emission-persistence-watchdog.timer
systemctl start minara-emission-persistence-watchdog.service
journalctl -u minara-emission-persistence-watchdog.service -n 30 --no-pager
```

## Failure Modes

| Failure | Behavior |
|---|---|
| DB missing/uninitialized | skip persistence, log `minara_alert_emission_persist_skipped`, return command |
| missing `signal_type` | skip persistence, log reason, return command |
| DB lock contention | timeout before acquiring `_txn_lock`, log timeout, return command |
| DB write exception after lock | rollback, log failure, return command |
| duplicate live event | `source_event_id`/`tg_alert_log_id` unique index ignores duplicate |
| duplicate backfill run | deterministic journalctl `source_event_id` ignores duplicate |
| historical command text unavailable | store NULL command fields and `command_text_observed=0` |
| persistence writer disconnected | watchdog exits nonzero and emits Telegram alert when journal event IDs are missing from DB |

## Test Plan

1. Migration creates table, indexes, schema_version, and paper_migrations row.
2. Migration is idempotent on second initialization.
3. Migration refuses to stamp success if an incompatible partial table already exists.
4. Helper inserts live row with valid parent `paper_trades` and logical `tg_alert_log_id`.
5. Helper returns `False` for duplicate `source_event_id`/`tg_alert_log_id`.
6. Helper requires explicit `source_event_id` when `tg_alert_log_id` is absent.
7. `maybe_minara_command` prepares the command without DB persistence.
8. `notify_paper_trade_opened` persists only after Telegram delivery succeeds.
9. Persistence exception returns command and logs failure.
10. Persistence lock timeout returns command and logs timeout without cancelling an in-flight DB statement.
11. `notify_paper_trade_opened` passes contextual IDs into Minara persistence.
12. Backfill parser extracts journalctl JSON event rows.
13. Backfill apply is idempotent.
14. Watchdog passes when DB rows cover journal emissions.
15. Watchdog fails when journal event IDs are missing from persisted DB rows.

## Deployment Verification

Deployment must stop the pipeline before broad journalctl backfill, so no post-migration live row can overlap with the historical backfill window.

1. Stop `gecko-pipeline`, then capture the stopped timestamp.
2. Backup DB.
3. Deploy code.
4. Run an offline one-shot migration command while the service remains stopped; do not do a normal pipeline startup before broad backfill.
5. Export journalctl events only through the captured stop timestamp.
6. Run mandatory backfill from that bounded journal file.
7. Run the parity watchdog against the same bounded file.
8. Install and enable the hourly systemd watchdog timer.
9. Start `gecko-pipeline`.

Verification:

```sql
SELECT name FROM sqlite_master WHERE type='table' AND name='minara_alert_emissions';

SELECT source, command_text_observed, COUNT(*)
FROM minara_alert_emissions
WHERE emitted_at >= '2026-05-11T01:54:00Z'
GROUP BY source, command_text_observed;
```

Backfill command:

```bash
sudo systemctl stop gecko-pipeline
STOP_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cd /root/gecko-alpha
.venv/bin/python - <<'PY'
import asyncio
from scout.db import Database

async def main():
    db = Database("scout.db")
    await db.initialize()
    await db.close()

asyncio.run(main())
PY
journalctl -u gecko-pipeline --since '2026-05-11 01:54:00' --until "$STOP_TS" --no-pager -o cat \
  | grep minara_alert_command_emitted > /tmp/minara_emissions.jsonl
.venv/bin/python scripts/backfill_minara_alert_emissions.py --db scout.db --journal /tmp/minara_emissions.jsonl --apply
.venv/bin/python scripts/check_minara_emission_persistence.py --db scout.db --journal /tmp/minara_emissions.jsonl
install -m 0644 systemd/minara-emission-persistence-watchdog.service /etc/systemd/system/
install -m 0644 systemd/minara-emission-persistence-watchdog.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now minara-emission-persistence-watchdog.timer
systemctl start minara-emission-persistence-watchdog.service
sudo systemctl start gecko-pipeline
```

## Out Of Scope

- Operator paste UI
- Minara execution
- Changing Telegram alert eligibility/cooldown semantics
- Reconstructing historical command text from CoinGecko
