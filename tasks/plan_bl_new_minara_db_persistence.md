**New primitives introduced:** New SQLite table `minara_alert_emissions`; new `Database.record_minara_alert_emission(...)` write helper; new post-Telegram-delivery `persist_minara_alert_emission(...)` helper; new deterministic `source_event_id` idempotency key for live and backfilled emissions; new mandatory backfill script `scripts/backfill_minara_alert_emissions.py` for journalctl JSON lines; new parity watchdog scripts `scripts/check_minara_emission_persistence.py` and `scripts/minara-emission-persistence-watchdog.sh`; new systemd watchdog service/timer units.

## Drift Check

| Check | Evidence | Verdict |
|---|---|---|
| Existing Minara command emit | `scout/trading/minara_alert.py` emits `minara_alert_command_emitted` with `coin_id`, `chain`, `amount_usd` only | Reuse emit point; add DB persistence |
| Existing DB persistence | Runtime/grep found no `minara_alert_emissions` table and no Minara-specific DB write helper | Net-new schema needed |
| Existing TG audit table | `tg_alert_log` records alert outcome/cooldown, not per-Minara command emission or paste acknowledgement | Do not overload outcome enum again |
| Existing backlog/design | `backlog.md` says BL-NEW-M1.5C shipped log-only and `bl_tg_alert_log_m1_5c_outcome` did not add per-emit rows | Backlog remains open |

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Minara command generation | Existing project code: `scout/trading/minara_alert.py`; Hermes/Minara is execution-side, not telemetry persistence | Reuse existing project helper; no new Hermes call |
| Project-specific SQLite telemetry persistence | None found in installed VPS skills/plugins; Hermes session storage is for Hermes internals, not gecko-alpha business telemetry | Build inline in `scout/db.py` |
| Journalctl backfill into gecko-alpha schema | None found in installed VPS skills/plugins or public hub | Build small project script |
| Journalctl-vs-DB persistence watchdog | None found in installed VPS skills/plugins or public hub | Build small local watchdog |
| Operator paste acknowledgement UI | None in this PR | Defer; table column only |

Awesome-Hermes ecosystem check: checked the public Hermes skills hub (`https://hermes-agent.nousresearch.com/docs/skills`), `0xNyk/awesome-hermes-agent`, and `NousResearch/hermes-agent-self-evolution`; no reusable skill covers Minara emission persistence into gecko-alpha's SQLite schema. Installed VPS Hermes skills/plugins were checked as a separate source; no DB telemetry persistence, journalctl backfill, or parity watchdog primitive was available.

One-sentence verdict: this is project-internal audit persistence for an existing gecko-alpha log event, below Hermes skill granularity; custom code is justified after drift + Hermes checks.

# BL-NEW-MINARA-DB-PERSISTENCE Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every `minara_alert_command_emitted` event to SQLite so the D+14 Minara kill-criterion remains evaluable even if journalctl rotates.

**Architecture:** Add a forward-only migration creating a sibling audit table, prepare the Minara command before Telegram send, then emit/persist the Minara command only after Telegram delivery succeeds. Persistence is best-effort and bounded: failures, missing context, or timeout occur after delivery and must emit a structured persisted/skipped/failed log tied to `source_event_id`.

**D+14 denominator decision:** the table counts Minara commands delivered to the operator in Telegram. `minara_alert_command_emitted` is emitted only after Telegram delivery succeeds, closing the prepared-but-undelivered ambiguity caught in PR review.

**Tech Stack:** Python 3.12, aiosqlite, structlog, pytest-asyncio, existing `Database` migration pattern.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `scout/db.py` | Modify | Migration + `record_minara_alert_emission` helper |
| `scout/trading/minara_alert.py` | Modify | Accept optional DB context; persist command emission after success log |
| `scout/trading/tg_alert_dispatch.py` | Modify | Pass `db`, `paper_trade_id`, `signal_type`, and `sent_row_id` into Minara helper |
| `scripts/backfill_minara_alert_emissions.py` | Create | Parse JSON journalctl lines and insert missing historical emission rows; required before marking shipped |
| `scripts/check_minara_emission_persistence.py` | Create | Compare journalctl Minara emissions against persisted DB rows; nonzero exit on deficit |
| `scripts/minara-emission-persistence-watchdog.sh` | Create | Ops wrapper to collect journalctl window and Telegram-alert on parity failure |
| `systemd/minara-emission-persistence-watchdog.{service,timer}` | Create | Hourly recurring parity watchdog wiring |
| `tests/test_minara_alert_persistence.py` | Create | Migration/write/helper/backfill tests |
| `tests/test_minara_alert.py` | Modify | Pin compatibility: helper still works without DB |
| `backlog.md` | Modify | Mark backlog item shipped with commit/PR reference |

## Task 1: Migration And DB Helper

**Files:**
- Modify: `scout/db.py`
- Create: `tests/test_minara_alert_persistence.py`

- [ ] **Step 1: Write failing migration test**

```python
@pytest.mark.asyncio
async def test_minara_alert_emissions_table_created(tmp_path):
    db = Database(tmp_path / "scout.db")
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='minara_alert_emissions'"
        )
        assert await cur.fetchone() is not None
        cur = await db._conn.execute("PRAGMA table_info(minara_alert_emissions)")
        cols = {row[1] for row in await cur.fetchall()}
        assert {
            "id",
            "paper_trade_id",
            "tg_alert_log_id",
            "signal_type",
            "coin_id",
            "chain",
            "amount_usd",
            "command_text",
            "command_hash",
            "command_text_observed",
            "source",
            "source_event_id",
            "emitted_at",
            "operator_paste_acknowledged_at",
        }.issubset(cols)
    finally:
        await db.close()
```

- [ ] **Step 2: Verify RED**

Run: `$env:UV_NATIVE_TLS='true'; uv run --extra dev pytest tests/test_minara_alert_persistence.py::test_minara_alert_emissions_table_created -q`

Expected: FAIL because `minara_alert_emissions` does not exist.

- [ ] **Step 3: Implement migration**

Add `_migrate_minara_alert_emissions_v1()` to `Database.initialize()` after `_migrate_narrative_scanner_v1()`, so schema migration order follows version order (`20260518` narrative scanner before `20260519` Minara emissions).

Schema:

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
CREATE INDEX IF NOT EXISTS idx_minara_alert_emissions_emitted_at
    ON minara_alert_emissions(emitted_at);
CREATE INDEX IF NOT EXISTS idx_minara_alert_emissions_coin_id
    ON minara_alert_emissions(coin_id, emitted_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_minara_alert_emissions_tg_alert_log_id
    ON minara_alert_emissions(tg_alert_log_id)
    WHERE tg_alert_log_id IS NOT NULL;
```

`tg_alert_log_id` is intentionally a logical indexed reference, not a physical FK, because `tg_alert_log` has a table-rebuild migration pattern for CHECK enum extension. A physical child FK would make future rebuilds risky or null the exact linkage this table preserves.

Stamp:

```python
("bl_minara_alert_emissions_v1", 20260519, "bl_minara_alert_emissions_v1")
```

Implementation details:
- Create `paper_migrations` if needed.
- Idempotency gate on `paper_migrations.name = 'bl_minara_alert_emissions_v1'`.
- Post-assert required columns and unique indexes before stamping success.
- Insert both `paper_migrations` and `schema_version` rows inside the same `BEGIN EXCLUSIVE` transaction.

- [ ] **Step 4: Run migration test GREEN**

Run: `$env:UV_NATIVE_TLS='true'; uv run --extra dev pytest tests/test_minara_alert_persistence.py::test_minara_alert_emissions_table_created -q`

Expected: PASS.

- [ ] **Step 5: Write failing helper insert test**

Seed valid parent rows first so SQLite foreign keys are exercised rather than bypassed:

```python
cur = await db._conn.execute(
    "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, signal_data, "
    "entry_price, amount_usd, quantity, tp_price, sl_price, opened_at, status) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    (
        "goblincoin",
        "GOBLIN",
        "Goblin",
        "solana",
        "gainers_early",
        "{}",
        0.1,
        300,
        3000,
        0.12,
        0.09,
        "2026-05-13T00:00:00Z",
        "open",
    ),
)
paper_trade_id = cur.lastrowid
cur = await db._conn.execute(
    "INSERT INTO tg_alert_log (paper_trade_id, signal_type, token_id, alerted_at, outcome) "
    "VALUES (?, ?, ?, ?, 'sent')",
    (paper_trade_id, "gainers_early", "goblincoin", "2026-05-13T00:00:01Z"),
)
tg_alert_log_id = cur.lastrowid
```

Then call `record_minara_alert_emission(...)` with those IDs and assert the row contains both IDs, `command_hash`, `command_text_observed=1`, `source="live"`, and `source_event_id="tg_alert_log:<id>"`.

Expected RED: `AttributeError: 'Database' object has no attribute 'record_minara_alert_emission'`.

- [ ] **Step 6: Implement helper**

Signature:

```python
async def record_minara_alert_emission(
    self,
    *,
    paper_trade_id: int | None,
    tg_alert_log_id: int | None,
    signal_type: str,
    coin_id: str,
    chain: str,
    amount_usd: float,
    command_text: str | None,
    emitted_at: str | None = None,
    source_event_id: str | None = None,
    source: str = "live",
) -> bool:
```

Behavior:
- Raise when DB is uninitialized.
- Acquire `self._txn_lock` before insert/commit.
- Compute `command_hash` only when `command_text` is not `None`; set `command_text_observed=1` for live rows with command text, else `0`.
- Default `emitted_at` to UTC ISO time.
- Default `source_event_id` to `tg_alert_log:<id>` when `tg_alert_log_id` is present.
- If `tg_alert_log_id` is absent, require explicit `source_event_id`; do not derive from wall-clock time.
- Use plain `INSERT`.
- Commit inside the lock.
- Return `True` for inserted row and `False` for duplicate ignored.
- Catch only known duplicate unique-key violations as idempotent duplicates. CHECK/NOT NULL/FK violations must raise after rollback, not be masked.
- Roll back before releasing `_txn_lock` on any insert/commit exception or cancellation.

## Task 2: Runtime Write Path

**Files:**
- Modify: `scout/trading/minara_alert.py`
- Modify: `scout/trading/tg_alert_dispatch.py`
- Modify: `tests/test_minara_alert.py`

- [ ] **Step 1: Write failing persistence test**

Use a fake DB object whose async `record_minara_alert_emission(**kwargs)` appends calls. Call `persist_minara_alert_emission(..., db=fake_db, paper_trade_id=42, signal_type="gainers_early", tg_alert_log_id=99)` and assert one persisted call with `coin_id`, `chain="solana"`, settings-derived `amount_usd=10`, and exact `command_text`.

Expected RED: `persist_minara_alert_emission()` does not exist.

- [ ] **Step 2: Implement optional DB context**

Add `persist_minara_alert_emission(...)` and call it from `notify_paper_trade_opened(...)` after Telegram send succeeds.

After the post-delivery `minara_alert_command_emitted` log:
- Compute `source_event_id = f"tg_alert_log:{tg_alert_log_id}"` when `tg_alert_log_id` is present.
- If `db` or `signal_type` is missing, emit `minara_alert_emission_persist_skipped` with `reason`, `coin_id`, `chain`, `amount_usd`, and the available `source_event_id`; return `cmd`.
- Otherwise bound `_txn_lock` acquisition to `persistence_lock_timeout_sec`; do not cancel an in-flight SQLite insert/commit after the lock is acquired.
- On success, emit `minara_alert_emission_persisted`.
- On duplicate ignored, emit `minara_alert_emission_persist_duplicate_ignored`.
- On lock timeout, emit `minara_alert_emission_persist_timeout` and return `cmd`.
- On other `Exception`, emit `minara_alert_emission_persist_failed` and return `cmd`.

Add tests proving Telegram send is not blocked by persistence failure/timeout, and cancellation during Telegram send demotes the pre-claimed row without writing a live Minara emission.

- [ ] **Step 3: Pass context from TG dispatch**

Change `notify_paper_trade_opened` to call `maybe_minara_command` only for command preparation, then call `persist_minara_alert_emission` after Telegram delivery with `db`, `paper_trade_id`, `signal_type`, and `sent_row_id`.

- [ ] **Step 4: Verify compatibility and new behavior**

Run:

```powershell
$env:UV_NATIVE_TLS='true'
uv run --extra dev pytest tests/test_minara_alert.py tests/test_tg_alert_dispatch.py tests/test_minara_alert_persistence.py -q
```

Expected: PASS.

## Task 3: Backfill Script

**Files:**
- Create: `scripts/backfill_minara_alert_emissions.py`
- Modify: `tests/test_minara_alert_persistence.py`

- [ ] **Step 1: Write parser tests first**

Test JSONRenderer lines with `event="minara_alert_command_emitted"`, `coin_id`, `chain`, `amount_usd`, and `timestamp`. Parser returns normalized row with `emitted_at`.

Expected RED: module/function missing.

- [ ] **Step 2: Implement script**

CLI:

```powershell
uv run python scripts/backfill_minara_alert_emissions.py --db scout.db --journal .minara_journal.jsonl --dry-run
uv run python scripts/backfill_minara_alert_emissions.py --db scout.db --journal .minara_journal.jsonl --apply
```

Behavior: read journalctl lines, keep only `minara_alert_command_emitted`, do not invent historical command text if not present, insert with `paper_trade_id=NULL`, `tg_alert_log_id=NULL`, `signal_type="unknown_historical_backfill"`, `command_text=NULL`, `command_hash=NULL`, `command_text_observed=0`, `source="journalctl_backfill"`, and `source_event_id` from the log when present or `journalctl:<timestamp>:<coin_id>:<chain>:<amount_usd>` for pre-PR historical rows so re-running the backfill is idempotent.

Backfill is not optional for this PR. If journalctl rows are unavailable at deploy time, record that explicitly in `backlog.md` as a partial-denominator caveat and include the first DB row timestamp in the shipped note.

## Task 3b: Freshness SLO And Watchdog

**Files:**
- Create: `scripts/check_minara_emission_persistence.py`
- Create: `scripts/minara-emission-persistence-watchdog.sh`
- Modify: `tests/test_minara_alert_persistence.py`

- [ ] **Step 1: Define the SLO**

Because Minara emits are sparse, "no rows in 24h" is not necessarily bad. The SLO is event-ID parity: for a checked journalctl window, every observed `minara_alert_command_emitted.source_event_id` must exist in `minara_alert_emissions`.

- [ ] **Step 2: Test the watchdog**

Add one test where two journal events and two DB rows pass, and one where two journal events but one DB row fails with `deficit=1`.

- [ ] **Step 3: Implement the watchdog**

`scripts/check_minara_emission_persistence.py --db scout.db --journal /tmp/minara_emissions.jsonl` prints JSON and exits `2` on deficit. `scripts/minara-emission-persistence-watchdog.sh` collects recent journalctl output, fails closed when journalctl fails, and sends a Telegram alert via existing `.env` credentials on failure. The systemd service/timer runs it hourly with `WorkingDirectory=/root/gecko-alpha`.

## Task 4: Documentation And Backlog Closure

**Files:**
- Modify: `backlog.md`
- Modify: `tasks/todo.md`

- [ ] **Step 1: Update backlog item status**

Change `BL-NEW-MINARA-DB-PERSISTENCE` from `PROPOSED` to `SHIPPED <date>` after tests, PR merge, deploy, and mandatory backfill/partial-denominator note. Include migration name, table name, backfill command/result, and the D+14 query.

- [ ] **Step 2: Update task checklist**

Mark plan/design/build/review/deploy checkboxes in `tasks/todo.md`.

## Verification Matrix

Run before PR:

```powershell
$env:UV_NATIVE_TLS='true'
uv run --extra dev pytest tests/test_minara_alert.py tests/test_tg_alert_dispatch.py tests/test_minara_alert_persistence.py -q
uv run --extra dev pytest tests/test_trading_db_migration.py tests/test_main_m1_5c_announcement.py -q
```

Expected: new table exists; migration is idempotent; helper hashes live command text and records contextual IDs; historical rows may have NULL command fields with `command_text_observed=0`; `source_event_id` prevents duplicate live/backfill inserts; command preparation remains no-DB compatible; TG alert dispatch persists only after delivery; persistence success/skip/fail/timeout logs are observable; persistence failure/timeout does not suppress Telegram command.

## Deployment Notes

1. Stop `gecko-pipeline` before broad backfill to avoid overlap between live DB rows and journalctl backfill rows.
2. Backup prod DB.
3. Deploy code.
4. Run migration via an offline one-shot command while the service remains stopped; do not start a normal pipeline cycle before broad backfill.
5. Verify:

```sql
SELECT name FROM sqlite_master WHERE type='table' AND name='minara_alert_emissions';
SELECT COUNT(*) FROM minara_alert_emissions;
```

6. Mandatory backfill:

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

7. Coverage verification:

```sql
SELECT source, command_text_observed, COUNT(*)
FROM minara_alert_emissions
WHERE emitted_at >= '2026-05-11T01:54:00Z'
GROUP BY source, command_text_observed;
```

If the journalctl backfill source count is zero, update `backlog.md` with a partial-denominator caveat before closing the item.
