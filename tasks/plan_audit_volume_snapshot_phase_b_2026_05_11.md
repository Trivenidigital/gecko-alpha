**New primitives introduced:** `audit_volume_snapshot_phase_b` table (schema_version 20260518); `scout/audit/snapshot.py` module; `scripts/gecko_audit_snapshot.py` CLI; `scripts/gecko-audit-snapshot.sh` bash entrypoint; `scripts/gecko-audit-snapshot-watchdog.sh`; `systemd/gecko-audit-snapshot.service` + `.timer`; `systemd/gecko-audit-snapshot-watchdog.service` + `.timer`; heartbeat file at `/var/lib/gecko-alpha/audit-snapshot/snapshot-last-ok`.

# Audit Volume Snapshot — Phase B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a daily snapshot job that copies `volume_history_cg` rows for slow_burn-detected tokens into a non-pruned audit table, before the 7-day rolling prune deletes 2026-05-10 detection data. Source data is silently deleted by `scout/spikes/detector.py:55-57` 7 days after `recorded_at`; D+14 audit (2026-05-24) can't read it without snapshot infrastructure.

**Architecture:** Four-tier mirror of the existing `gecko-backup` pattern: (1) schema migration adds `audit_volume_snapshot_phase_b` table with composite UNIQUE (coin_id, recorded_at) for idempotency, (2) async snapshot module performs cross-table copy gated by slow_burn coin_ids in soak window, (3) systemd oneshot service + daily timer fires at 04:00 UTC, (4) separate watchdog service + timer fires at 10:00 UTC, checks heartbeat file freshness, alerts to Telegram via direct curl bypassing `scout.alerter` (per gecko-backup-watchdog R6 CRITICAL — alerter swallows aiohttp errors).

**Tech Stack:** aiosqlite (async SQLite, matches project convention), structlog (JSON logging), Pydantic v2 BaseSettings (config), systemd timers (Ubuntu 22.04 VPS), bash + curl + jq-equivalent Python (watchdog Telegram delivery), pytest-asyncio (TDD per project convention).

**Pre-registered spec (locked 2026-05-11, do NOT alter during implementation):**

- Cadence: daily at 04:00 UTC
- Scope: all `volume_history_cg` rows for slow_burn-detected coin_ids during soak window (2026-05-10 through 2026-05-25)
- Destination: new `audit_volume_snapshot_phase_b` table (separate from volume_history_cg, not subject to prune)
- Idempotency: `ON CONFLICT (coin_id, recorded_at) DO NOTHING` — multiple daily runs don't duplicate
- Failure mode: watchdog service emits Telegram alert via direct curl on heartbeat-file staleness (>30h). Do NOT use heartbeat counter pattern — in-memory-only persistence is the failure mode this design avoids.
- Verification: per-run structured logging of rows_captured + distinct_coin_ids_covered
- End-of-soak: final run 2026-05-25 (D+15), then operator disables timer. Table preserved as audit artifact.

**Hermes-first analysis:**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Async SQLite snapshot | none found | use in-tree pattern (`scout/spikes/detector.py:record_volume`) |
| systemd oneshot + timer | none found | mirror in-tree `systemd/gecko-backup.{service,timer}` |
| Heartbeat-file watchdog | none found | mirror in-tree `scripts/gecko-backup-watchdog.sh` |
| Telegram bot API delivery | none found | use direct curl per R6 CRITICAL on `scout.alerter` (already established pattern in watchdog) |
| Schema migration | none found | mirror in-tree `_migrate_*` pattern in `scout/db.py:3041+` |

awesome-hermes-agent ecosystem check: no relevant skill — domains are SQLite + systemd + bash, not LLM-agent territory. Verdict: build from in-tree patterns, no Hermes integration.

---

## File Structure

**Files to create:**

- `scout/audit/__init__.py` — package marker
- `scout/audit/snapshot.py` — async snapshot function
- `scripts/gecko_audit_snapshot.py` — Python CLI entrypoint
- `scripts/gecko-audit-snapshot.sh` — bash wrapper (consistent with gecko-backup-rotate.sh pattern)
- `scripts/gecko-audit-snapshot-watchdog.sh` — staleness check + Telegram alert
- `systemd/gecko-audit-snapshot.service` — oneshot service
- `systemd/gecko-audit-snapshot.timer` — daily 04:00 UTC timer
- `systemd/gecko-audit-snapshot-watchdog.service` — watchdog service
- `systemd/gecko-audit-snapshot-watchdog.timer` — daily 10:00 UTC watchdog timer
- `tests/audit/__init__.py`
- `tests/audit/test_snapshot.py` — unit tests for snapshot module
- `tests/audit/test_snapshot_script.py` — integration tests for CLI entrypoint
- `tests/scripts/test_audit_snapshot_watchdog.py` — watchdog bash script tests (using bash + subprocess + tempfiles)

**Files to modify:**

- `scout/db.py` — add `_migrate_audit_volume_snapshot_phase_b()` method (after the existing `_migrate_*` methods around line 3105+); append the call to the migration chain in `initialize()` at line 105, immediately after `await self._migrate_tg_alert_log_m1_5c_outcome()` (verified by grep on `scout/db.py` 2026-05-11: migrations live in `initialize()` lines 89-104, NOT inside `_create_tables()`)
- `tasks/audit_brief_phase_b_slow_burn_2026_05_11.md` — replace `price_cache` references with `audit_volume_snapshot_phase_b`; add cross-token gap analysis spec; add B2 tail validation spec; add data-source-lock + audit-runs-at-D+14-regardless clause

---

## Task 1: Schema migration — add `audit_volume_snapshot_phase_b` table

**Files:**
- Modify: `scout/db.py` (add `_migrate_audit_volume_snapshot_phase_b()` method + wire into migration chain)
- Test: `tests/test_db_migrations.py` (likely exists; if not, find appropriate test file via `Grep "_migrate_" tests/`)

- [ ] **Step 1: Locate migration chain in db.py and existing migration test file**

Run: `Grep "await self._migrate" scout/db.py -n` to find where migrations are invoked from `initialize()` (verified 2026-05-11: lines 89-104, NOT inside `_create_tables()` — that method is DDL-only).
Run: `Glob "tests/test_*migration*.py"` to find existing migration test file.
Expected: find the call site for `_migrate_tg_alert_log_m1_5c_outcome` at line 104 — new migration appends at line 105 inside `initialize()`.

- [ ] **Step 2: Write the failing test**

Add to `tests/audit/test_snapshot.py`:

```python
import pytest
import aiosqlite
from scout.db import Database


@pytest.mark.asyncio
async def test_migration_creates_audit_volume_snapshot_table(tmp_path):
    """Schema migration creates audit_volume_snapshot_phase_b with correct columns + UNIQUE constraint."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    # Verify table exists with expected columns
    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute("PRAGMA table_info(audit_volume_snapshot_phase_b)")
        cols = {row[1]: row[2] for row in await cur.fetchall()}
    assert "coin_id" in cols
    assert "symbol" in cols
    assert "name" in cols
    assert "volume_24h" in cols
    assert "market_cap" in cols
    assert "price" in cols
    assert "recorded_at" in cols
    assert "snapshotted_at" in cols

    # Verify schema_version row
    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT description FROM schema_version WHERE version = ?", (20260518,)
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "bl_audit_volume_snapshot_phase_b"

    await db.close()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/audit/test_snapshot.py::test_migration_creates_audit_volume_snapshot_table -v`
Expected: FAIL with "no such table: audit_volume_snapshot_phase_b" or NameError on the test module path.

- [ ] **Step 4: Create `tests/audit/__init__.py` (empty package marker)**

```python
# tests/audit/__init__.py — empty package marker for audit test suite
```

- [ ] **Step 5: Implement migration in scout/db.py**

Append after the last existing `_migrate_*` method (locate via Grep). Pattern mirrors `_migrate_slow_burn_v1`:

```python
async def _migrate_audit_volume_snapshot_phase_b(self) -> None:
    """BL-NEW-AUDIT-SNAPSHOT: Phase B audit-time snapshot of volume_history_cg.

    Schema version 20260518. Creates audit_volume_snapshot_phase_b table — a
    non-pruned mirror of volume_history_cg rows for slow_burn-detected coin_ids
    during the Phase B soak window (2026-05-10 through 2026-05-25). The source
    table volume_history_cg is rolling-pruned at 7 days by spikes/detector.py;
    this table preserves data through D+14 evaluation (2026-05-24).

    UNIQUE (coin_id, recorded_at) enables ON CONFLICT DO NOTHING idempotency
    for daily snapshot runs without duplicate rows.
    """
    import structlog

    _log = structlog.get_logger()
    if self._conn is None:
        raise RuntimeError("Database not initialized.")
    conn = self._conn
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        await conn.execute("BEGIN EXCLUSIVE")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_volume_snapshot_phase_b (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id         TEXT    NOT NULL,
                symbol          TEXT    NOT NULL,
                name            TEXT    NOT NULL,
                volume_24h      REAL    NOT NULL,
                market_cap      REAL,
                price           REAL,
                recorded_at     TEXT    NOT NULL,
                snapshotted_at  TEXT    NOT NULL,
                UNIQUE (coin_id, recorded_at)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_vol_snap_coin "
            "ON audit_volume_snapshot_phase_b(coin_id, recorded_at)"
        )
        await conn.execute(
            "INSERT OR IGNORE INTO schema_version "
            "(version, applied_at, description) VALUES (?, ?, ?)",
            (20260518, now_iso, "bl_audit_volume_snapshot_phase_b"),
        )
        await conn.commit()
    except Exception as e:
        _log.exception(
            "schema_migration_failed",
            migration="bl_audit_volume_snapshot_phase_b",
            err=str(e),
            err_type=type(e).__name__,
        )
        try:
            await conn.execute("ROLLBACK")
        except Exception as rb_err:
            _log.exception(
                "schema_migration_rollback_failed",
                migration="bl_audit_volume_snapshot_phase_b",
                err=str(rb_err),
                err_type=type(rb_err).__name__,
            )
        _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_audit_volume_snapshot_phase_b")
        raise

    cur = await conn.execute(
        "SELECT description FROM schema_version WHERE version = ?", (20260518,)
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError(
            "bl_audit_volume_snapshot_phase_b schema_version row missing after migration"
        )
    if row[0] != "bl_audit_volume_snapshot_phase_b":
        raise RuntimeError(
            f"bl_audit_volume_snapshot_phase_b schema_version description mismatch — "
            f"expected 'bl_audit_volume_snapshot_phase_b', got {row[0]!r}"
        )
```

- [ ] **Step 6: Wire migration into `initialize()` migration chain**

The migration chain lives in `Database.initialize()` at lines 88-104 of `scout/db.py` (verified by grep 2026-05-11). `_create_tables()` (line 88) is DDL-only; migrations are sibling calls AFTER it. The last migration in the chain is `_migrate_tg_alert_log_m1_5c_outcome()` at line 104. Append the new migration call at line 105:

```python
await self._migrate_audit_volume_snapshot_phase_b()
```

The line will sit immediately after `await self._migrate_tg_alert_log_m1_5c_outcome()` and before the end of `initialize()`. Do NOT insert inside `_create_tables()` — that method is for static DDL only and is called once at the top of `initialize()`.

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/audit/test_snapshot.py::test_migration_creates_audit_volume_snapshot_table -v`
Expected: PASS.

- [ ] **Step 8: Add idempotency test**

```python
@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path):
    """Running migration twice (simulating pipeline restart) does not error or duplicate schema_version row."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()
    await db.close()

    # Simulate pipeline restart: open a new Database against the same file.
    # The migration must be a no-op on second invocation (CREATE TABLE IF NOT EXISTS
    # + INSERT OR IGNORE on schema_version).
    db2 = Database(str(db_path))
    await db2.connect()

    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = ?", (20260518,)
        )
        count = (await cur.fetchone())[0]
    assert count == 1

    await db2.close()
```

Run: `uv run pytest tests/audit/test_snapshot.py::test_migration_is_idempotent -v`
Expected: PASS.

- [ ] **Step 9: Add UNIQUE constraint test**

```python
@pytest.mark.asyncio
async def test_unique_constraint_prevents_duplicate(tmp_path):
    """UNIQUE (coin_id, recorded_at) prevents duplicate rows."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    now_iso = "2026-05-11T12:00:00+00:00"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "INSERT INTO audit_volume_snapshot_phase_b "
            "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at, snapshotted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("test-coin", "TEST", "Test", 1000.0, 5000.0, 0.5, now_iso, now_iso),
        )
        await conn.commit()
        # Second insert with same (coin_id, recorded_at) must fail
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO audit_volume_snapshot_phase_b "
                "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at, snapshotted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("test-coin", "TEST", "Test", 2000.0, 5500.0, 0.6, now_iso, now_iso),
            )
            await conn.commit()

    await db.close()
```

Run: `uv run pytest tests/audit/test_snapshot.py::test_unique_constraint_prevents_duplicate -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add scout/db.py tests/audit/__init__.py tests/audit/test_snapshot.py
git commit -m "feat(audit): add audit_volume_snapshot_phase_b schema migration

Creates non-pruned mirror of volume_history_cg rows for slow_burn-detected
coin_ids during Phase B soak window. UNIQUE (coin_id, recorded_at) enables
ON CONFLICT DO NOTHING idempotency for daily snapshot runs.

Schema version 20260518. Source table volume_history_cg is rolling-pruned
at 7 days; this table preserves data through D+14 evaluation 2026-05-24."
```

---

## Task 2: Snapshot module (`scout/audit/snapshot.py`)

**Files:**
- Create: `scout/audit/__init__.py`
- Create: `scout/audit/snapshot.py`
- Test: `tests/audit/test_snapshot.py` (extend with snapshot-function tests)

- [ ] **Step 1: Create package marker**

```python
# scout/audit/__init__.py — Phase B audit infrastructure
```

- [ ] **Step 2: Write the failing test (empty cohort)**

Add to `tests/audit/test_snapshot.py`:

```python
from scout.audit.snapshot import snapshot_volume_history_for_phase_b


@pytest.mark.asyncio
async def test_snapshot_empty_cohort(tmp_path):
    """Snapshot with no slow_burn detections returns (0, 0)."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    rows, coin_ids = await snapshot_volume_history_for_phase_b(
        db,
        soak_start_iso="2026-05-10T00:00:00+00:00",
        soak_end_iso="2026-05-25T00:00:00+00:00",
    )
    assert rows == 0
    assert coin_ids == 0
    await db.close()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/audit/test_snapshot.py::test_snapshot_empty_cohort -v`
Expected: FAIL with ImportError (snapshot module doesn't exist yet).

- [ ] **Step 4: Implement snapshot module**

Create `scout/audit/snapshot.py`:

```python
"""BL-NEW-AUDIT-SNAPSHOT: Phase B audit-time snapshot of volume_history_cg.

Captures `volume_history_cg` rows for slow_burn-detected coin_ids before the
rolling 7-day prune in scout/spikes/detector.py:55-57 deletes them. Output
table audit_volume_snapshot_phase_b is not subject to prune; data preserved
through D+14 evaluation 2026-05-24.

Idempotent: ON CONFLICT (coin_id, recorded_at) DO NOTHING per UNIQUE constraint.
Multiple daily runs do not duplicate rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from scout.db import Database

logger = structlog.get_logger(__name__)


async def snapshot_volume_history_for_phase_b(
    db: "Database",
    soak_start_iso: str,
    soak_end_iso: str,
) -> tuple[int, int]:
    """Copy volume_history_cg rows for slow_burn-detected coin_ids into the
    non-pruned audit snapshot table.

    Args:
        db: Connected Database instance.
        soak_start_iso: ISO-8601 UTC timestamp marking start of soak window
            (used to filter slow_burn_candidates.detected_at).
        soak_end_iso: ISO-8601 UTC timestamp marking end of soak window.

    Returns:
        (rows_captured, distinct_coin_ids_covered) tuple. rows_captured counts
        new rows actually inserted (ON CONFLICT DO NOTHING returns 0 for
        duplicates). distinct_coin_ids_covered is the size of the slow_burn
        cohort whose rows were attempted.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    snapshotted_at = datetime.now(timezone.utc).isoformat()

    # 1. Get the slow_burn cohort coin_ids for the soak window
    cur = await db._conn.execute(
        "SELECT DISTINCT coin_id FROM slow_burn_candidates "
        "WHERE datetime(detected_at) >= datetime(?) "
        "AND datetime(detected_at) < datetime(?)",
        (soak_start_iso, soak_end_iso),
    )
    coin_id_rows = await cur.fetchall()
    coin_ids = [row[0] for row in coin_id_rows]

    if not coin_ids:
        logger.info(
            "audit_snapshot_empty_cohort",
            soak_start=soak_start_iso,
            soak_end=soak_end_iso,
        )
        return (0, 0)

    # 2. Pre-INSERT estimate (R6 "log-before-rm" pattern from gecko-backup-rotate).
    # Compute expected row count BEFORE writing so partial-failure post-mortems can
    # reconstruct intent. Cheap: bounded by cohort size, runs once.
    placeholders_all = ",".join("?" * len(coin_ids))
    est_cur = await db._conn.execute(
        f"SELECT COUNT(*) FROM volume_history_cg WHERE coin_id IN ({placeholders_all})",
        coin_ids,
    )
    est_row = await est_cur.fetchone()
    estimated_source_rows = int(est_row[0]) if est_row else 0
    logger.info(
        "audit_snapshot_starting",
        coin_ids_count=len(coin_ids),
        estimated_source_rows=estimated_source_rows,
        soak_start=soak_start_iso,
        soak_end=soak_end_iso,
        snapshotted_at=snapshotted_at,
    )

    # 2b. Disk pre-flight (R2-C1 hard gate, post-script-start re-check).
    # The bash wrapper does a pre-run check (catches deploy-time disk-low). This
    # check catches mid-run drift: pipeline may have written a large batch in
    # the seconds between bash-wrapper-start and Python-INSERT-start, eating
    # the slack. Re-verify immediately before chunk loop. <10G free → abort
    # cleanly; heartbeat file is NOT updated → watchdog at 10:00 UTC alerts
    # via existing direct-curl Telegram path. Cost asymmetry (per locked
    # decision 2026-05-11): hard-gate false-positive = 1 day missed snapshot
    # (recoverable next run, ON CONFLICT DO NOTHING handles re-runs);
    # warn-and-proceed false-negative = partial-write or disk-full mid-run
    # corrupting audit data. Favor the safer gate.
    import shutil

    DISK_GATE_PATH = "/root"
    DISK_GATE_THRESHOLD_GB = 10
    free_bytes = shutil.disk_usage(DISK_GATE_PATH).free
    free_gb = free_bytes / 1_000_000_000
    if free_gb < DISK_GATE_THRESHOLD_GB:
        logger.error(
            "audit_snapshot_disk_gate_failed_at_insert_time",
            path=DISK_GATE_PATH,
            free_gb=round(free_gb, 2),
            threshold_gb=DISK_GATE_THRESHOLD_GB,
            coin_ids_count=len(coin_ids),
            estimated_source_rows=estimated_source_rows,
        )
        raise RuntimeError(
            f"Disk gate failed at INSERT time: {free_gb:.2f}G free at "
            f"{DISK_GATE_PATH}, need {DISK_GATE_THRESHOLD_GB}G. "
            f"Cohort={len(coin_ids)} coin_ids, estimated_rows={estimated_source_rows}. "
            f"Heartbeat not updated; watchdog at 10:00 UTC will alert."
        )

    # 3. Copy matching volume_history_cg rows with ON CONFLICT DO NOTHING.
    # SQLite parameter limit safety: chunk if cohort > 500 coin_ids.
    # Per-chunk commit (R2-M2 amendment): a single multi-chunk transaction
    # would hold SQLite's write lock for the duration of all chunks, blocking
    # the pipeline's `record_volume` writer (60s cadence) for seconds-to-minutes.
    # Per-chunk commit keeps each lock-hold to <1s, letting the pipeline
    # interleave its own writes. The CLI runs in a separate process; the
    # in-process `_txn_lock` pattern that the pipeline uses for in-process
    # coordination does NOT apply here (verified by grep on scout/ 2026-05-11:
    # _txn_lock is acquired by callers in scout/trading/ + scout/live/ + main.py,
    # never inside db.py itself). Cross-process serialization is SQLite's job.
    CHUNK = 500
    total_inserted = 0
    for i in range(0, len(coin_ids), CHUNK):
        chunk = coin_ids[i : i + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        # Use INSERT OR IGNORE for SQLite ON CONFLICT compatibility on
        # UNIQUE-constrained insert. changes() reports rows actually inserted
        # by the most recent INSERT (intervening SELECTs do NOT reset changes()).
        await db._conn.execute(
            f"""INSERT OR IGNORE INTO audit_volume_snapshot_phase_b
                (coin_id, symbol, name, volume_24h, market_cap, price,
                 recorded_at, snapshotted_at)
                SELECT coin_id, symbol, name, volume_24h, market_cap, price,
                       recorded_at, ?
                FROM volume_history_cg
                WHERE coin_id IN ({placeholders})""",
            (snapshotted_at, *chunk),
        )
        cur2 = await db._conn.execute("SELECT changes()")
        inserted_row = await cur2.fetchone()
        chunk_inserted = int(inserted_row[0]) if inserted_row else 0
        total_inserted += chunk_inserted
        # Per-chunk commit releases SQLite write lock between chunks.
        await db._conn.commit()
        logger.debug(
            "audit_snapshot_chunk_committed",
            chunk_index=i // CHUNK,
            chunk_size=len(chunk),
            chunk_inserted=chunk_inserted,
        )

    logger.info(
        "audit_snapshot_completed",
        rows_captured=total_inserted,
        coin_ids_covered=len(coin_ids),
        soak_start=soak_start_iso,
        soak_end=soak_end_iso,
        snapshotted_at=snapshotted_at,
    )
    return (total_inserted, len(coin_ids))
```

- [ ] **Step 5: Run test to verify empty cohort passes**

Run: `uv run pytest tests/audit/test_snapshot.py::test_snapshot_empty_cohort -v`
Expected: PASS.

- [ ] **Step 6: Add basic-capture test**

```python
@pytest.mark.asyncio
async def test_snapshot_basic_capture(tmp_path):
    """Snapshot captures volume_history_cg rows for slow_burn-detected coin_ids."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    # Seed: one slow_burn detection + 3 volume_history_cg rows for it
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "INSERT INTO slow_burn_candidates "
            "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("coin-a", "AAA", "AlphaCoin", 75.0, -1.5, "2026-05-10T03:50:00+00:00"),
        )
        for i, ts in enumerate([
            "2026-05-10T04:00:00+00:00",
            "2026-05-10T05:00:00+00:00",
            "2026-05-10T06:00:00+00:00",
        ]):
            await conn.execute(
                "INSERT INTO volume_history_cg "
                "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("coin-a", "AAA", "AlphaCoin", 100000.0 + i, 5e6, 0.1 + i * 0.01, ts),
            )
        await conn.commit()

    rows, coin_ids = await snapshot_volume_history_for_phase_b(
        db,
        soak_start_iso="2026-05-10T00:00:00+00:00",
        soak_end_iso="2026-05-25T00:00:00+00:00",
    )
    assert rows == 3
    assert coin_ids == 1
    await db.close()
```

Run: `uv run pytest tests/audit/test_snapshot.py::test_snapshot_basic_capture -v`
Expected: PASS.

- [ ] **Step 7: Add idempotency test**

```python
@pytest.mark.asyncio
async def test_snapshot_idempotent(tmp_path):
    """Running snapshot twice does not duplicate rows (ON CONFLICT DO NOTHING)."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "INSERT INTO slow_burn_candidates "
            "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("coin-b", "BBB", "BetaCoin", 60.0, 0.5, "2026-05-10T03:50:00+00:00"),
        )
        await conn.execute(
            "INSERT INTO volume_history_cg "
            "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("coin-b", "BBB", "BetaCoin", 50000.0, 3e6, 0.2, "2026-05-10T04:00:00+00:00"),
        )
        await conn.commit()

    rows1, _ = await snapshot_volume_history_for_phase_b(
        db,
        soak_start_iso="2026-05-10T00:00:00+00:00",
        soak_end_iso="2026-05-25T00:00:00+00:00",
    )
    rows2, _ = await snapshot_volume_history_for_phase_b(
        db,
        soak_start_iso="2026-05-10T00:00:00+00:00",
        soak_end_iso="2026-05-25T00:00:00+00:00",
    )
    assert rows1 == 1
    assert rows2 == 0  # ON CONFLICT DO NOTHING — no new inserts

    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM audit_volume_snapshot_phase_b WHERE coin_id = ?",
            ("coin-b",),
        )
        count = (await cur.fetchone())[0]
    assert count == 1  # single row, no duplicate

    await db.close()
```

Run: `uv run pytest tests/audit/test_snapshot.py::test_snapshot_idempotent -v`
Expected: PASS.

- [ ] **Step 8: Add soak-window-filter test (out-of-window detections excluded)**

```python
@pytest.mark.asyncio
async def test_snapshot_filters_out_of_window_detections(tmp_path):
    """Slow_burn detections outside soak window are excluded from cohort."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    async with aiosqlite.connect(str(db_path)) as conn:
        # In-window detection
        await conn.execute(
            "INSERT INTO slow_burn_candidates "
            "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("in-window", "IN", "In", 70.0, 0.0, "2026-05-12T00:00:00+00:00"),
        )
        # Pre-window detection (before 2026-05-10)
        await conn.execute(
            "INSERT INTO slow_burn_candidates "
            "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("pre-window", "PRE", "Pre", 70.0, 0.0, "2026-05-09T00:00:00+00:00"),
        )
        # Post-window detection (after 2026-05-25)
        await conn.execute(
            "INSERT INTO slow_burn_candidates "
            "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("post-window", "POST", "Post", 70.0, 0.0, "2026-05-26T00:00:00+00:00"),
        )
        # Volume rows for all three
        for cid in ("in-window", "pre-window", "post-window"):
            await conn.execute(
                "INSERT INTO volume_history_cg "
                "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cid, cid.upper(), cid, 1000.0, 1e6, 0.1, "2026-05-12T01:00:00+00:00"),
            )
        await conn.commit()

    rows, coin_ids = await snapshot_volume_history_for_phase_b(
        db,
        soak_start_iso="2026-05-10T00:00:00+00:00",
        soak_end_iso="2026-05-25T00:00:00+00:00",
    )
    assert coin_ids == 1  # only "in-window"
    assert rows == 1
    await db.close()
```

Run: `uv run pytest tests/audit/test_snapshot.py::test_snapshot_filters_out_of_window_detections -v`
Expected: PASS.

- [ ] **Step 8b: Add chunking boundary test (R1-M3)**

The snapshot module chunks at CHUNK=500 to stay safely under SQLite's 999-parameter default limit. A test at the 500/501 boundary verifies that chunking correctly walks the cohort without dropping the boundary coin_ids.

```python
@pytest.mark.asyncio
async def test_snapshot_chunking_boundary(tmp_path):
    """501 distinct coin_ids → 2 chunks (500 + 1), all rows captured."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    async with aiosqlite.connect(str(db_path)) as conn:
        for i in range(501):
            coin_id = f"coin-{i:04d}"
            await conn.execute(
                "INSERT INTO slow_burn_candidates "
                "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (coin_id, f"S{i:04d}", f"Name{i}", 60.0, 0.0, "2026-05-10T03:50:00+00:00"),
            )
            await conn.execute(
                "INSERT INTO volume_history_cg "
                "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (coin_id, f"S{i:04d}", f"Name{i}", 1000.0 + i, 1e6, 0.1, "2026-05-10T04:00:00+00:00"),
            )
        await conn.commit()

    rows, coin_ids = await snapshot_volume_history_for_phase_b(
        db,
        soak_start_iso="2026-05-10T00:00:00+00:00",
        soak_end_iso="2026-05-25T00:00:00+00:00",
    )
    assert rows == 501, f"Expected 501 rows captured across 2 chunks; got {rows}"
    assert coin_ids == 501

    # Verify rows actually landed in audit table (not silently dropped at boundary)
    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM audit_volume_snapshot_phase_b"
        )
        actual_count = (await cur.fetchone())[0]
    assert actual_count == 501, f"audit table has {actual_count} rows; expected 501"

    await db.close()
```

Run: `uv run pytest tests/audit/test_snapshot.py::test_snapshot_chunking_boundary -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add scout/audit/__init__.py scout/audit/snapshot.py tests/audit/test_snapshot.py
git commit -m "feat(audit): add snapshot_volume_history_for_phase_b module

Async function that copies volume_history_cg rows for slow_burn-detected
coin_ids in the Phase B soak window into the non-pruned audit table.
Idempotent via ON CONFLICT DO NOTHING on (coin_id, recorded_at) UNIQUE.

Returns (rows_captured, coin_ids_covered) for structured per-run logging."
```

---

## Task 3: CLI entrypoint (`scripts/gecko_audit_snapshot.py`)

**Files:**
- Create: `scripts/gecko_audit_snapshot.py`
- Create: `scripts/gecko-audit-snapshot.sh` (bash wrapper)
- Test: `tests/audit/test_snapshot_script.py`

- [ ] **Step 1: Write the failing test for CLI argparse**

Create `tests/audit/test_snapshot_script.py`:

```python
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import aiosqlite


def _seed_db(db_path: str) -> None:
    """Seed a test DB with one slow_burn detection + one volume_history_cg row.

    R1-M1 amendment: use Database.initialize() to build the schema rather than
    raw CREATE TABLE statements. This couples the CLI integration test to the
    real migration chain — if the migration's column list ever drifts, the test
    will fail at schema-creation time, not silently pass while production breaks.
    """
    from scout.db import Database

    async def _seed():
        # Build schema via the real migration chain (not duplicated raw DDL)
        db = Database(db_path)
        await db.connect()

        # Seed data via the connection that Database opened
        await db._conn.execute(
            "INSERT INTO slow_burn_candidates "
            "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("test-coin", "TEST", "Test", 75.0, 0.0, "2026-05-10T03:50:00+00:00"),
        )
        await db._conn.execute(
            "INSERT INTO volume_history_cg "
            "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("test-coin", "TEST", "Test", 1000.0, 5e6, 0.1, "2026-05-10T04:00:00+00:00"),
        )
        await db._conn.commit()
        await db.close()

    asyncio.run(_seed())


def test_cli_runs_and_writes_heartbeat(tmp_path):
    """CLI invocation captures rows + writes atomic heartbeat file."""
    db_path = tmp_path / "test.db"
    hb_path = tmp_path / "snapshot-last-ok"
    _seed_db(str(db_path))

    script = Path(__file__).parent.parent.parent / "scripts" / "gecko_audit_snapshot.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db-path", str(db_path),
            "--soak-start", "2026-05-10T00:00:00+00:00",
            "--soak-end", "2026-05-25T00:00:00+00:00",
            "--heartbeat-file", str(hb_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert hb_path.exists()
    hb_content = hb_path.read_text().strip()
    assert hb_content.isdigit()  # unix timestamp
    assert int(hb_content) > 1_700_000_000  # post-2023 sanity
```

- [ ] **Step 2: Run test to verify it fails (script doesn't exist)**

Run: `uv run pytest tests/audit/test_snapshot_script.py::test_cli_runs_and_writes_heartbeat -v`
Expected: FAIL with non-zero returncode (script doesn't exist).

- [ ] **Step 3: Implement scripts/gecko_audit_snapshot.py**

```python
#!/usr/bin/env python3
"""gecko_audit_snapshot — Phase B daily snapshot CLI.

Run by systemd timer gecko-audit-snapshot.timer at 04:00 UTC daily. Captures
volume_history_cg rows for slow_burn-detected coin_ids in the soak window
into the non-pruned audit_volume_snapshot_phase_b table.

On success: writes atomic heartbeat file (timestamp), exits 0. Watchdog
service gecko-audit-snapshot-watchdog.timer (10:00 UTC) checks heartbeat
freshness and alerts to Telegram if stale.

Exit codes:
    0 = success (rows captured + heartbeat written)
    2 = misconfiguration (DB path missing, bad ISO timestamps, etc.)
    3 = runtime error (DB error, lock contention, write failure)

Idempotency: ON CONFLICT DO NOTHING on (coin_id, recorded_at). Safe to run
multiple times per day.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog

# Make scout package importable when running from scripts/
sys.path.insert(0, str(Path(__file__).parent.parent))

from scout.audit.snapshot import snapshot_volume_history_for_phase_b
from scout.db import Database

logger = structlog.get_logger(__name__)


def _atomic_heartbeat_write(heartbeat_path: Path) -> None:
    """Write unix timestamp to heartbeat file atomically (.tmp + mv -f pattern).

    Matches the discipline of scripts/gecko-backup-rotate.sh:95-101: truncate-
    then-write exposes a 0-byte file to concurrent readers; kernel-atomic
    rename within same filesystem is the safe path.
    """
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = heartbeat_path.with_suffix(heartbeat_path.suffix + f".tmp.{os.getpid()}")
    tmp_path.write_text(str(int(datetime.now(timezone.utc).timestamp())))
    os.replace(tmp_path, heartbeat_path)


async def _run(args: argparse.Namespace) -> int:
    db = Database(args.db_path)
    try:
        await db.connect()
        # NOTE: do NOT call create_tables() here. Schema migration is owned by
        # the long-running pipeline (scout/main.py). Snapshot script assumes
        # the schema is already in place; if it isn't, the snapshot insert will
        # fail loudly with "no such table" — that's the right signal.
        rows, coin_ids = await snapshot_volume_history_for_phase_b(
            db,
            soak_start_iso=args.soak_start,
            soak_end_iso=args.soak_end,
        )
        logger.info(
            "audit_snapshot_cli_completed",
            rows_captured=rows,
            coin_ids_covered=coin_ids,
            db_path=args.db_path,
            heartbeat_file=args.heartbeat_file,
        )
    finally:
        await db.close()

    _atomic_heartbeat_write(Path(args.heartbeat_file))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, help="Path to scout.db")
    parser.add_argument(
        "--soak-start",
        required=True,
        help="ISO-8601 UTC timestamp for soak window start (e.g. 2026-05-10T00:00:00+00:00)",
    )
    parser.add_argument(
        "--soak-end",
        required=True,
        help="ISO-8601 UTC timestamp for soak window end (e.g. 2026-05-25T00:00:00+00:00)",
    )
    parser.add_argument(
        "--heartbeat-file",
        required=True,
        help="Path to atomic heartbeat file (e.g. /var/lib/gecko-alpha/audit-snapshot/snapshot-last-ok)",
    )
    args = parser.parse_args()

    # Validate ISO timestamps
    try:
        datetime.fromisoformat(args.soak_start)
        datetime.fromisoformat(args.soak_end)
    except ValueError as e:
        print(f"ERROR: invalid ISO-8601 timestamp: {e}", file=sys.stderr)
        return 2

    if not Path(args.db_path).exists():
        print(f"ERROR: DB path does not exist: {args.db_path}", file=sys.stderr)
        return 2

    try:
        return asyncio.run(_run(args))
    except Exception as e:
        logger.exception("audit_snapshot_cli_failed", err=str(e), err_type=type(e).__name__)
        return 3


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/audit/test_snapshot_script.py::test_cli_runs_and_writes_heartbeat -v`
Expected: PASS.

- [ ] **Step 5: Add CLI failure-exit tests**

```python
def test_cli_exits_2_on_missing_db(tmp_path):
    """Missing DB path returns exit code 2 (misconfiguration)."""
    hb_path = tmp_path / "hb"
    script = Path(__file__).parent.parent.parent / "scripts" / "gecko_audit_snapshot.py"
    result = subprocess.run(
        [
            sys.executable, str(script),
            "--db-path", str(tmp_path / "nonexistent.db"),
            "--soak-start", "2026-05-10T00:00:00+00:00",
            "--soak-end", "2026-05-25T00:00:00+00:00",
            "--heartbeat-file", str(hb_path),
        ],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert not hb_path.exists()  # heartbeat NOT written on misconfig


def test_cli_exits_2_on_bad_iso(tmp_path):
    """Invalid ISO timestamp returns exit code 2."""
    db_path = tmp_path / "test.db"
    hb_path = tmp_path / "hb"
    _seed_db(str(db_path))
    script = Path(__file__).parent.parent.parent / "scripts" / "gecko_audit_snapshot.py"
    result = subprocess.run(
        [
            sys.executable, str(script),
            "--db-path", str(db_path),
            "--soak-start", "not-an-iso-timestamp",
            "--soak-end", "2026-05-25T00:00:00+00:00",
            "--heartbeat-file", str(hb_path),
        ],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
```

Run: `uv run pytest tests/audit/test_snapshot_script.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 6: Create bash wrapper scripts/gecko-audit-snapshot.sh**

```bash
#!/usr/bin/env bash
# gecko-audit-snapshot — bash entrypoint for systemd ExecStart=
#
# Wraps `uv run python scripts/gecko_audit_snapshot.py` with the env-driven
# config the systemd unit provides. Matches the pattern in
# scripts/gecko-backup-rotate.sh for consistency.
#
# Required env:
#   GECKO_REPO              — absolute path to gecko-alpha repo (must contain scripts/, scout/)
#   GECKO_DB_PATH           — absolute path to scout.db
#   GECKO_AUDIT_SOAK_START  — ISO-8601 UTC timestamp (Phase B soak start)
#   GECKO_AUDIT_SOAK_END    — ISO-8601 UTC timestamp (Phase B soak end)
#   GECKO_AUDIT_HEARTBEAT_FILE — absolute path to heartbeat file
#
# Exit codes propagate from the Python CLI (0=success, 2=misconfig, 3=runtime).

set -euo pipefail

: "${GECKO_REPO:?ERROR: GECKO_REPO must be set}"
: "${GECKO_DB_PATH:?ERROR: GECKO_DB_PATH must be set}"
: "${GECKO_AUDIT_SOAK_START:?ERROR: GECKO_AUDIT_SOAK_START must be set}"
: "${GECKO_AUDIT_SOAK_END:?ERROR: GECKO_AUDIT_SOAK_END must be set}"
: "${GECKO_AUDIT_HEARTBEAT_FILE:?ERROR: GECKO_AUDIT_HEARTBEAT_FILE must be set}"

# R2-C1 hard gate: pre-run disk check.
# Threshold = 10G free at $GECKO_DISK_GATE_PATH (default /root).
# Locked 2026-05-11 after R2 design review found cohort generates ~177K rows
# day-1 (2-3 orders of magnitude higher than initial plan estimate). On gate
# failure: abort + Telegram alert via direct-curl (same mechanism as
# gecko-audit-snapshot-watchdog.sh) so operator finds out at gate time, not
# 6 hours later at watchdog cycle. Per locked decision: hard gate, NOT
# warn-and-prompt — cron at 04:00 UTC is unattended; prompts go ignored.

DISK_GATE_PATH="${GECKO_DISK_GATE_PATH:-/root}"
DISK_GATE_THRESHOLD_GB="${GECKO_DISK_GATE_THRESHOLD_GB:-10}"

free_gb=$(df -BG "$DISK_GATE_PATH" 2>/dev/null | tail -1 | awk '{print $4}' | sed 's/G//')
if [[ ! "$free_gb" =~ ^[0-9]+$ ]]; then
    echo "ERROR: disk gate could not parse df output for $DISK_GATE_PATH (got: $free_gb)" >&2
    exit 9  # 9 = disk-gate-parse-failure
fi

if (( free_gb < DISK_GATE_THRESHOLD_GB )); then
    echo "DISK GATE FAILED: only ${free_gb}G free at $DISK_GATE_PATH, need ${DISK_GATE_THRESHOLD_GB}G" >&2

    # Direct-curl Telegram alert. Mirrors gecko-audit-snapshot-watchdog.sh
    # alert path (curl POST to bot API, check HTTP status, propagate non-200).
    # Skipped if env is missing creds; the watchdog path also fires at 10:00
    # UTC and catches heartbeat-not-updated separately.
    ENV_FILE="${GECKO_ENV_FILE:-$GECKO_REPO/.env}"
    if [[ -f "$ENV_FILE" ]]; then
        TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
        TELEGRAM_CHAT_ID="$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
        if [[ -n "$TELEGRAM_BOT_TOKEN" && "$TELEGRAM_BOT_TOKEN" != "placeholder" && -n "$TELEGRAM_CHAT_ID" && "$TELEGRAM_CHAT_ID" != "placeholder" ]]; then
            TEXT="⚠️ gecko-audit-snapshot: DISK GATE FAILED — only ${free_gb}G free at ${DISK_GATE_PATH}, need ${DISK_GATE_THRESHOLD_GB}G. Snapshot aborted; investigate disk pressure before next 04:00 UTC fire."
            PYTHON_BIN="$(command -v python3 || command -v python || true)"
            if [[ -n "$PYTHON_BIN" ]]; then
                PAYLOAD="$(GECKO_TG_TEXT="$TEXT" GECKO_TG_CHAT="$TELEGRAM_CHAT_ID" "$PYTHON_BIN" -c '
import json, os
print(json.dumps({"chat_id": os.environ["GECKO_TG_CHAT"], "text": os.environ["GECKO_TG_TEXT"]}))
')"
                curl -s -o /dev/null -w '' -X POST -H 'Content-Type: application/json' -d "$PAYLOAD" \
                    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" || true
            fi
        fi
    fi
    exit 8  # 8 = disk-gate-failure
fi

if [[ ! -d "$GECKO_REPO" ]]; then
    echo "ERROR: GECKO_REPO=$GECKO_REPO is not a directory" >&2
    exit 2
fi

cd "$GECKO_REPO"
exec uv run python scripts/gecko_audit_snapshot.py \
    --db-path "$GECKO_DB_PATH" \
    --soak-start "$GECKO_AUDIT_SOAK_START" \
    --soak-end "$GECKO_AUDIT_SOAK_END" \
    --heartbeat-file "$GECKO_AUDIT_HEARTBEAT_FILE"
```

Make executable: `chmod +x scripts/gecko-audit-snapshot.sh` (note: chmod is a runtime install step on VPS, but flagging the bit here for documentation).

- [ ] **Step 7: Commit**

```bash
git add scripts/gecko_audit_snapshot.py scripts/gecko-audit-snapshot.sh tests/audit/test_snapshot_script.py
git commit -m "feat(audit): add gecko_audit_snapshot CLI + bash wrapper

Python CLI with argparse (--db-path, --soak-start, --soak-end,
--heartbeat-file). Writes atomic heartbeat file (.tmp + os.replace) on
success. Exit codes: 0=success, 2=misconfig, 3=runtime.

Bash wrapper for systemd ExecStart= mirrors gecko-backup-rotate.sh pattern
with env-driven config and 'exec uv run python ...' tail."
```

---

## Task 4: systemd service + timer

**Files:**
- Create: `systemd/gecko-audit-snapshot.service`
- Create: `systemd/gecko-audit-snapshot.timer`

- [ ] **Step 1: Create service unit**

```ini
[Unit]
Description=Gecko-Alpha — Phase B audit-snapshot of volume_history_cg for slow_burn cohort

[Service]
Type=oneshot
User=root
Group=root
StateDirectory=gecko-alpha/audit-snapshot
StateDirectoryMode=0750
Environment=GECKO_REPO=/root/gecko-alpha
Environment=GECKO_DB_PATH=/root/gecko-alpha/scout.db
Environment=GECKO_AUDIT_SOAK_START=2026-05-10T00:00:00+00:00
Environment=GECKO_AUDIT_SOAK_END=2026-05-25T00:00:00+00:00
Environment=GECKO_AUDIT_HEARTBEAT_FILE=/var/lib/gecko-alpha/audit-snapshot/snapshot-last-ok
Environment=HOME=/root
Environment=PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStartPre=/usr/bin/test -x /usr/local/bin/gecko-audit-snapshot.sh
ExecStart=/usr/local/bin/gecko-audit-snapshot.sh
TimeoutStartSec=300
StandardOutput=journal
StandardError=journal
```

- [ ] **Step 2: Create timer unit**

```ini
[Unit]
Description=Gecko-Alpha — daily Phase B audit-snapshot timer (04:00 UTC)

[Timer]
OnCalendar=*-*-* 04:00:00
Persistent=true
AccuracySec=10m
Unit=gecko-audit-snapshot.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Commit**

```bash
git add systemd/gecko-audit-snapshot.service systemd/gecko-audit-snapshot.timer
git commit -m "feat(audit): add systemd service + timer for daily snapshot

Daily 04:00 UTC oneshot mirrors the gecko-backup pattern. Soak window dates
(2026-05-10 to 2026-05-25) baked into Environment= for clarity; revising
requires unit edit + daemon-reload, which is appropriate friction for
locked pre-registration.

Persistent=true catches the run if the timer was inactive during the
scheduled tick (reboot, downtime)."
```

---

## Task 5: Watchdog service + timer + bash script

**Files:**
- Create: `scripts/gecko-audit-snapshot-watchdog.sh`
- Create: `systemd/gecko-audit-snapshot-watchdog.service`
- Create: `systemd/gecko-audit-snapshot-watchdog.timer`
- Test: `tests/scripts/test_audit_snapshot_watchdog.py`

- [ ] **Step 1: Create watchdog bash script (mirrors gecko-backup-watchdog.sh exactly)**

```bash
#!/usr/bin/env bash
# gecko-audit-snapshot-watchdog — alert if snapshot hasn't run successfully in 30h.
#
# 30h window = daily timer at 04:00 UTC + 6h grace for late fires. Watchdog
# itself runs at 10:00 UTC, so worst case is heartbeat is 30h stale (yesterday
# 04:00 → today 10:00). If heartbeat is older than 30h, snapshot has missed
# at least one cycle.
#
# Telegram delivery is direct curl, NOT via scout.alerter, per R6 CRITICAL
# in gecko-backup-watchdog: alerter swallows aiohttp errors silently. Direct
# curl checks HTTP status and propagates non-200 as exit 7.

set -euo pipefail

HEARTBEAT_FILE="${GECKO_AUDIT_HEARTBEAT_FILE:-/var/lib/gecko-alpha/audit-snapshot/snapshot-last-ok}"
STALE_AFTER_SEC="${GECKO_AUDIT_STALE_AFTER_SEC:-108000}"  # 30h
GECKO_REPO="${GECKO_REPO:-/root/gecko-alpha}"
ENV_FILE="${GECKO_ENV_FILE:-$GECKO_REPO/.env}"
UV_BIN="${UV_BIN:-}"  # test seam

now=$(date +%s)
is_stale=0

if [[ ! -f "$HEARTBEAT_FILE" ]]; then
    age_msg="heartbeat file MISSING ($HEARTBEAT_FILE)"
    is_stale=1
else
    last_ok=$(cat "$HEARTBEAT_FILE" 2>/dev/null || true)
    if [[ ! "$last_ok" =~ ^[0-9]+$ ]]; then
        age_msg="heartbeat file CORRUPT ($HEARTBEAT_FILE: $(printf '%q' "$last_ok"))"
        is_stale=1
    else
        age_sec=$(( now - last_ok ))
        age_msg="last_ok=${age_sec}s ago"
        if (( age_sec > STALE_AFTER_SEC )); then
            is_stale=1
        fi
    fi
fi

if (( is_stale == 0 )); then
    echo "OK: gecko-audit-snapshot ran within ${STALE_AFTER_SEC}s ($age_msg)"
    exit 0
fi

echo "STALE: gecko-audit-snapshot has not run successfully — $age_msg"

if [[ -n "$UV_BIN" ]]; then
    "$UV_BIN" stub-audit-snapshot-watchdog-alert "$age_msg" || true
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: env file $ENV_FILE not found; alert NOT delivered" >&2
    exit 4
fi

TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
TELEGRAM_CHAT_ID="$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"

if [[ -z "$TELEGRAM_BOT_TOKEN" || "$TELEGRAM_BOT_TOKEN" == "placeholder" ]]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN missing/placeholder in $ENV_FILE; alert NOT delivered" >&2
    exit 5
fi
if [[ -z "$TELEGRAM_CHAT_ID" || "$TELEGRAM_CHAT_ID" == "placeholder" ]]; then
    echo "ERROR: TELEGRAM_CHAT_ID missing/placeholder in $ENV_FILE; alert NOT delivered" >&2
    exit 5
fi

TEXT="⚠️ gecko-audit-snapshot-watchdog: snapshot stale — ${age_msg}. Check journalctl -u gecko-audit-snapshot.service."

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "ERROR: no python available for JSON encoding; alert NOT delivered" >&2
    exit 6
fi

PAYLOAD="$(GECKO_TG_TEXT="$TEXT" GECKO_TG_CHAT="$TELEGRAM_CHAT_ID" "$PYTHON_BIN" -c '
import json, os
print(json.dumps({"chat_id": os.environ["GECKO_TG_CHAT"], "text": os.environ["GECKO_TG_TEXT"]}))
')"

HTTP_STATUS="$(curl -s -o /tmp/.gecko-audit-tg-resp.$$ -w '%{http_code}' \
    -X POST \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" || echo 000)"

if [[ "$HTTP_STATUS" != "200" ]]; then
    echo "ERROR: Telegram delivery failed (HTTP $HTTP_STATUS)" >&2
    if [[ -f "/tmp/.gecko-audit-tg-resp.$$" ]]; then
        echo "RESPONSE: $(cat /tmp/.gecko-audit-tg-resp.$$ | head -c 500)" >&2
        rm -f "/tmp/.gecko-audit-tg-resp.$$"
    fi
    exit 7
fi

rm -f "/tmp/.gecko-audit-tg-resp.$$"
echo "ALERT DELIVERED: HTTP $HTTP_STATUS"
exit 1
```

- [ ] **Step 2: Create watchdog service unit**

```ini
[Unit]
Description=Gecko-Alpha — alert if Phase B audit-snapshot is stale
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
Group=root
Environment=GECKO_AUDIT_STALE_AFTER_SEC=108000
Environment=GECKO_AUDIT_HEARTBEAT_FILE=/var/lib/gecko-alpha/audit-snapshot/snapshot-last-ok
Environment=GECKO_REPO=/root/gecko-alpha
Environment=HOME=/root
Environment=PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
ExecStartPre=/usr/bin/test -x /usr/local/bin/gecko-audit-snapshot-watchdog.sh
ExecStartPre=/usr/bin/test -x /usr/bin/curl
ExecStart=/usr/local/bin/gecko-audit-snapshot-watchdog.sh
# Exit 1 = "heartbeat stale + alert delivered HTTP 200" — designed behavior.
# Treat as success so systemctl status only shows `failed` for real errors
# (exit 4 env file missing, 5 placeholder creds, 6 no python, 7 HTTP non-200).
SuccessExitStatus=1
TimeoutStartSec=60
StandardOutput=journal
StandardError=journal
```

- [ ] **Step 3: Create watchdog timer unit**

```ini
[Unit]
Description=Gecko-Alpha — daily watchdog: Phase B audit-snapshot freshness

[Timer]
OnCalendar=*-*-* 10:00:00
Persistent=true
AccuracySec=30m
Unit=gecko-audit-snapshot-watchdog.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Write watchdog bash test (subprocess-based)**

Create `tests/scripts/__init__.py` (empty) and `tests/scripts/test_audit_snapshot_watchdog.py`:

```python
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest


WATCHDOG = Path(__file__).parent.parent.parent / "scripts" / "gecko-audit-snapshot-watchdog.sh"


def _make_uv_stub(tmp_path: Path) -> Path:
    """Create a stub UV_BIN that records its invocation args to a file."""
    stub = tmp_path / "uv-stub.sh"
    stub.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{tmp_path}/uv-stub.log"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


@pytest.mark.skipif(os.name == "nt", reason="bash watchdog runs on Linux/WSL only")
def test_watchdog_exits_0_when_heartbeat_fresh(tmp_path):
    """Fresh heartbeat (now) → exit 0 + no alert invocation."""
    hb = tmp_path / "hb"
    hb.write_text(str(int(time.time())))
    uv_stub = _make_uv_stub(tmp_path)

    env = {
        "GECKO_AUDIT_HEARTBEAT_FILE": str(hb),
        "GECKO_AUDIT_STALE_AFTER_SEC": "30",
        "UV_BIN": str(uv_stub),
        "PATH": os.environ.get("PATH", ""),
    }
    result = subprocess.run(
        ["bash", str(WATCHDOG)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0
    assert not (tmp_path / "uv-stub.log").exists()  # no alert fired


@pytest.mark.skipif(os.name == "nt", reason="bash watchdog runs on Linux/WSL only")
def test_watchdog_exits_1_and_alerts_when_stale(tmp_path):
    """Stale heartbeat → exit 1 + UV_BIN stub invoked with age message."""
    hb = tmp_path / "hb"
    hb.write_text(str(int(time.time()) - 200))  # 200s old
    uv_stub = _make_uv_stub(tmp_path)

    env = {
        "GECKO_AUDIT_HEARTBEAT_FILE": str(hb),
        "GECKO_AUDIT_STALE_AFTER_SEC": "30",  # 30s threshold; 200s > 30s
        "UV_BIN": str(uv_stub),
        "PATH": os.environ.get("PATH", ""),
    }
    result = subprocess.run(
        ["bash", str(WATCHDOG)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 1
    log = (tmp_path / "uv-stub.log").read_text()
    assert "stub-audit-snapshot-watchdog-alert" in log


@pytest.mark.skipif(os.name == "nt", reason="bash watchdog runs on Linux/WSL only")
def test_watchdog_exits_1_and_alerts_when_heartbeat_missing(tmp_path):
    """Missing heartbeat file → exit 1 + alert."""
    hb = tmp_path / "does-not-exist"
    uv_stub = _make_uv_stub(tmp_path)

    env = {
        "GECKO_AUDIT_HEARTBEAT_FILE": str(hb),
        "GECKO_AUDIT_STALE_AFTER_SEC": "30",
        "UV_BIN": str(uv_stub),
        "PATH": os.environ.get("PATH", ""),
    }
    result = subprocess.run(
        ["bash", str(WATCHDOG)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 1
    log = (tmp_path / "uv-stub.log").read_text()
    assert "MISSING" in log or "stub-audit-snapshot-watchdog-alert" in log


@pytest.mark.skipif(os.name == "nt", reason="bash watchdog runs on Linux/WSL only")
def test_watchdog_exits_1_and_alerts_when_heartbeat_corrupt(tmp_path):
    """Corrupt heartbeat content (non-numeric) → exit 1 + alert."""
    hb = tmp_path / "hb"
    hb.write_text("not-a-number")
    uv_stub = _make_uv_stub(tmp_path)

    env = {
        "GECKO_AUDIT_HEARTBEAT_FILE": str(hb),
        "GECKO_AUDIT_STALE_AFTER_SEC": "30",
        "UV_BIN": str(uv_stub),
        "PATH": os.environ.get("PATH", ""),
    }
    result = subprocess.run(
        ["bash", str(WATCHDOG)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 1
    log = (tmp_path / "uv-stub.log").read_text()
    assert "CORRUPT" in log or "stub-audit-snapshot-watchdog-alert" in log
```

- [ ] **Step 5: Run watchdog tests**

Run: `uv run pytest tests/scripts/test_audit_snapshot_watchdog.py -v`
Expected: 4 tests PASS on Linux/WSL; all SKIP on Windows.

(Note: on Windows-only development, these tests skip cleanly. Run them in WSL or on the VPS during deployment verification.)

- [ ] **Step 6: Commit**

```bash
git add scripts/gecko-audit-snapshot-watchdog.sh systemd/gecko-audit-snapshot-watchdog.service systemd/gecko-audit-snapshot-watchdog.timer tests/scripts/__init__.py tests/scripts/test_audit_snapshot_watchdog.py
git commit -m "feat(audit): add watchdog service + timer + tests

Daily 10:00 UTC staleness check (30h window = 04:00 daily fire + 6h grace).
On stale heartbeat: direct curl POST to Telegram bot API, bypassing
scout.alerter (which swallows aiohttp errors per R6 CRITICAL).

UV_BIN test seam allows pytest-driven verification without real Telegram
calls. 4 stub-driven tests cover fresh/stale/missing/corrupt heartbeat."
```

---

## Task 6: Audit brief revisions [✅ ABSORBED INTO BRIEF RECREATION 2026-05-11]

**Files:**
- Affected: `tasks/audit_brief_phase_b_slow_burn_2026_05_11.md` (commit 3f2de5d)

**Status:** All four originally-planned amendments + Q2 scope-limitation addition + end-of-soak runbook were baked in at brief recreation time (commit 3f2de5d on `feat/audit-volume-snapshot-phase-b`). The original brief was lost between branch switches; reconstruction from conversation history embedded the locked criteria (formerly §4.1/§4.2 of the lost findings doc) directly into the brief as §2 + §3, and applied all R1/R2 amendments inline. This task is therefore **complete via recreation, not via edit-existing-file**.

**Verification map — what's where in the recreated brief:**

| Original Task 6 amendment | Brief section in 3f2de5d | Verification grep |
|---|---|---|
| Step 1: `price_cache` → `audit_volume_snapshot_phase_b` | §4 (Data path correctness) | `grep -c "audit_volume_snapshot_phase_b" brief.md` should return 0 occurrences of `price_cache` and many of `audit_volume_snapshot_phase_b` |
| Step 2: B2 tail validation spec | §8 (B3 hybrid tail validation) | `grep "Tail validation" brief.md` |
| Step 3: Cross-token gap analysis spec | §7 (per-token dropout vs system-wide pause) | `grep "per_token_dropout" brief.md` |
| Step 4: Data-source-lock clause | §11 (audit runs at D+14 regardless) | `grep "Data-source lock" brief.md` |
| Q2 addition: scope limitation (no pre-detection ramp-up) | §10 (Scope limitations) | `grep "Pre-detection ramp-up" brief.md` |
| End-of-soak runbook | §14 (operator SSH commands) | `grep "End-of-soak runbook" brief.md` |
| Forward direction of Task 8 cross-reference | §15 (silent-failure-audit watchdog reference) | `grep "findings_silent_failure_audit" brief.md` |

The **fidelity check against conversation transcript** (verifying recreation matches locked language) is folded into Task 8's re-self-review (step E of the amendment trajectory). If any section's content drifted from conversation lock, that's a defect to fix via separate Edit on the brief — but the original Task 6 edit-existing-file work is no longer applicable.

- [x] **Step 1: Replace `price_cache` references** — done in recreation (§4 of brief)
- [x] **Step 2: Add B2 tail validation spec** — done in recreation (§8 of brief)
- [x] **Step 3: Add cross-token gap analysis spec** — done in recreation (§7 of brief)
- [x] **Step 4: Add data-source-lock clause** — done in recreation (§11 of brief)
- [x] **Step 5: Commit** — landed as commit 3f2de5d cherry-picked from d7ea51d after the recovery sequence; orphan reverted on `fix/tg-parse-mode-hygiene-audit` as b26be9f

---

## Task 7: Deployment to VPS

**Files:** runbook execution only — no code changes in this task.

- [ ] **Step 1: Push branch to GitHub**

```bash
git push -u origin feat/audit-volume-snapshot-phase-b
```

(Branch name: `feat/audit-volume-snapshot-phase-b`. Branch should have been created at the start of work per CLAUDE.md "ALWAYS create a new branch before starting work".)

- [ ] **Step 2: SSH to VPS, pull, install scripts + units**

Per global CLAUDE.md SSH constraint (Windows): use two-step pattern.

```bash
# Bash tool — redirect to file
ssh root@89.167.116.187 'cd /root/gecko-alpha && git fetch && git checkout feat/audit-volume-snapshot-phase-b && git pull && find . -name __pycache__ -exec rm -rf {} + 2>/dev/null; echo "--- repo state ---"; git log -1 --oneline' > .ssh_deploy_audit.txt 2>&1
```

Then Read `.ssh_deploy_audit.txt` to verify.

Expected: HEAD shows the most recent commit on `feat/audit-volume-snapshot-phase-b`; pycache cleared (memory rule from `feedback_clear_pycache_on_deploy.md`).

- [ ] **Step 3: Install scripts to /usr/local/bin/**

```bash
ssh root@89.167.116.187 'cp /root/gecko-alpha/scripts/gecko-audit-snapshot.sh /usr/local/bin/ && cp /root/gecko-alpha/scripts/gecko-audit-snapshot-watchdog.sh /usr/local/bin/ && chmod 0755 /usr/local/bin/gecko-audit-snapshot.sh /usr/local/bin/gecko-audit-snapshot-watchdog.sh && ls -l /usr/local/bin/gecko-audit-snapshot*' > .ssh_install_scripts.txt 2>&1
```

Read `.ssh_install_scripts.txt`. Verify both scripts present with 0755.

- [ ] **Step 4: Install systemd units**

```bash
ssh root@89.167.116.187 'cp /root/gecko-alpha/systemd/gecko-audit-snapshot.service /etc/systemd/system/ && cp /root/gecko-alpha/systemd/gecko-audit-snapshot.timer /etc/systemd/system/ && cp /root/gecko-alpha/systemd/gecko-audit-snapshot-watchdog.service /etc/systemd/system/ && cp /root/gecko-alpha/systemd/gecko-audit-snapshot-watchdog.timer /etc/systemd/system/ && systemctl daemon-reload && ls /etc/systemd/system/gecko-audit-snapshot*' > .ssh_install_units.txt 2>&1
```

Read `.ssh_install_units.txt`. Verify 4 units present.

- [ ] **Step 5: Run schema migration via pipeline restart**

The migration is wired into `initialize()` (line 105 of `scout/db.py`) which runs at pipeline startup AND on every snapshot-CLI invocation (`db.connect()` calls `initialize()`). Pipeline restart in this step is for **early observability of migration errors in pipeline logs** — not a correctness prerequisite for Step 6. Without this step, the snapshot CLI's first run would migrate the schema itself (idempotent), but operator wouldn't see the migration log line in pipeline journalctl.

**Deploy timing (R2-M3 amendment):** wait until M1.5c soak ends (post-2026-05-12T02:00Z) to avoid disrupting that observation window. Pipeline restart kills in-flight MiroFish fallbacks, paper-trade evaluators, BL-067 in-memory cohort state, M1.5b live-trading approval state, M1.5c minara-alert pre-emptive 'sent' clearing logic. The M1.5c 24h soak observation has its own pre-registered evaluation; respecting it costs ~12h, well within the audit's 2026-05-17 prune deadline.

```bash
ssh root@89.167.116.187 'systemctl restart gecko-pipeline && sleep 10 && journalctl -u gecko-pipeline -n 200 --no-pager | grep -E "(bl_audit_volume_snapshot_phase_b|schema_migration|SCHEMA_DRIFT)"' > .ssh_pipeline_restart.txt 2>&1
```

Read `.ssh_pipeline_restart.txt`. Verify:
- `bl_audit_volume_snapshot_phase_b schema_version row missing after migration` does NOT appear (post-assertion catches rollback)
- `SCHEMA_DRIFT_DETECTED` does NOT appear
- A successful run shows the migration's structured log lines (or silence — the migration only logs on failure path)

**Rollback runbook if migration fails:**
- Migration is wrapped in `BEGIN EXCLUSIVE` + `ROLLBACK`-on-exception (verified pattern from `_migrate_bl_slow_burn_v1`). On failure, the audit table is NOT created and `schema_version` row for 20260518 is NOT inserted.
- If you see `SCHEMA_DRIFT_DETECTED` in logs: do NOT proceed to Step 6. Capture the exception details, investigate (likely a transient lock from another writer), retry pipeline restart at a quieter time.
- Pipeline service status after restart should be `active (running)` — verify with `systemctl status gecko-pipeline` if log inspection is inconclusive.

- [ ] **Step 5.5: Pre-flight disk-space gate (R2-C1 hard gate, deploy-time check)**

The plan's R2-C1 amendment locked a hard gate at 10G free disk space before any snapshot write. The bash wrapper does this on every run; the deploy-time check here is the operator's explicit pre-flight before invoking the bootstrap. The reason for both: empirical disk consumption is 2-3 orders of magnitude higher than the initial plan estimate (cohort generates ~177K rows day-1 per R2 verification); ongoing visibility matters as much as one-time check.

```bash
ssh root@89.167.116.187 'echo "--- df check ---"; df -h /root; echo "--- backup directory ---"; ls -lh /root/gecko-alpha/scout.db.bak* 2>/dev/null | head -5 || echo "(no backups visible)"; echo "--- estimated cohort size ---"; sqlite3 /root/gecko-alpha/scout.db "SELECT COUNT(DISTINCT coin_id) FROM slow_burn_candidates WHERE datetime(detected_at) >= datetime(\"2026-05-10T00:00:00\")"; echo "--- estimated source rows ---"; sqlite3 /root/gecko-alpha/scout.db "SELECT COUNT(*) FROM volume_history_cg WHERE coin_id IN (SELECT DISTINCT coin_id FROM slow_burn_candidates WHERE datetime(detected_at) >= datetime(\"2026-05-10T00:00:00\"))"' > .ssh_disk_preflight.txt 2>&1
```

Read `.ssh_disk_preflight.txt`. Verify:
- `df -h /root` shows **at least 10G free** in the `Avail` column. If below 10G: ABORT this deploy. Cleanup backups (per `feedback_vps_backup_rotation.md`), wait for `gecko-backup.timer` to rotate, OR free space manually before retrying. Do NOT proceed.
- Backup directory size is reasonable (no orphan multi-GB files from interrupted backups).
- Cohort size and estimated source rows are within expectations (cohort grows during soak; check that the numbers are plausible for the time-since-2026-05-10).

**This gate is intentionally strict.** Per locked decision 2026-05-11: hard gate at <10G free, abort with visibility. Cron at 04:00 UTC is unattended; warn-and-prompt assumes operator presence that won't exist. Cost asymmetry favors the safer gate.

- [ ] **Step 6: Bootstrap snapshot (manual run before scheduled timer fires)**

Critical: this captures all CURRENTLY-EXISTING `volume_history_cg` rows for the 56-detection cohort before any get pruned. If we wait for the next scheduled 04:00 UTC fire, rows recorded today (2026-05-11) that get pruned on 2026-05-18 are still safe; but rows from earliest 2026-05-10 detections are at risk (prune at 2026-05-17). Bootstrap-now eliminates the timing risk.

```bash
ssh root@89.167.116.187 'systemctl start gecko-audit-snapshot.service && sleep 3 && journalctl -u gecko-audit-snapshot.service -n 100 --no-pager' > .ssh_bootstrap_snapshot.txt 2>&1
```

Read `.ssh_bootstrap_snapshot.txt`. Expected:
- `audit_snapshot_completed` structured log with `rows_captured` > 0 and `coin_ids_covered` matching the slow_burn cohort size at time of run
- `audit_snapshot_cli_completed` log
- systemd shows `Finished Gecko-Alpha — Phase B audit-snapshot...` and `(success)`

- [ ] **Step 6.5: Expected-vs-actual coverage verification (R2-M1 amendment)**

After bootstrap, verify which detections actually got their pre-detection window captured. Any detection whose earliest snapshotted row is AFTER its `detected_at` indicates data was already pruned before bootstrap could capture it. This is informational — for the audit's locked `[detected_at, +48h]` forward window, pre-detection data isn't load-bearing — but the coverage report is the operator's signal of how much pre-detection context was lost to prune-timing.

```bash
ssh root@89.167.116.187 'sqlite3 -header -column /root/gecko-alpha/scout.db "
  WITH cohort AS (
    SELECT coin_id, MIN(detected_at) AS first_detected
    FROM slow_burn_candidates
    WHERE datetime(detected_at) >= datetime(\"2026-05-10T00:00:00\")
    GROUP BY coin_id
  ),
  snapshot_coverage AS (
    SELECT coin_id, MIN(recorded_at) AS earliest_in_audit, MAX(recorded_at) AS latest_in_audit, COUNT(*) AS row_count
    FROM audit_volume_snapshot_phase_b
    GROUP BY coin_id
  )
  SELECT
    c.coin_id,
    c.first_detected,
    s.earliest_in_audit,
    s.row_count,
    CASE
      WHEN s.earliest_in_audit IS NULL THEN \"NO COVERAGE\"
      WHEN datetime(s.earliest_in_audit) > datetime(c.first_detected) THEN \"PRE-DETECTION TRUNCATED\"
      ELSE \"FULL COVERAGE\"
    END AS coverage_status
  FROM cohort c LEFT JOIN snapshot_coverage s USING (coin_id)
  ORDER BY coverage_status, c.first_detected
"' > .ssh_coverage_verify.txt 2>&1
```

Read `.ssh_coverage_verify.txt`. Tally the three `coverage_status` buckets:
- **FULL COVERAGE**: pre-detection context preserved (snapshot's earliest row is AT OR BEFORE first detected_at). Best outcome.
- **PRE-DETECTION TRUNCATED**: snapshot's earliest row is AFTER detected_at — pre-detection ramp-up rows were already pruned before bootstrap captured the cohort. Acceptable per §10 of audit brief (pre-detection scope explicitly NOT load-bearing for the locked audit gate).
- **NO COVERAGE**: snapshot has zero rows for this coin_id — token must have dropped out of CG markets response entirely between detection and bootstrap. Flag for investigation; possibly an audit-coverage hole that affects the forward `[detected_at, +48h]` window too.

If any detection lands in NO COVERAGE: investigate before proceeding. If many lands in PRE-DETECTION TRUNCATED: expected and consistent with §10 scope limitation. If all in FULL COVERAGE: bootstrap was timely.

- [ ] **Step 7: Verify heartbeat file written**

```bash
ssh root@89.167.116.187 'ls -l /var/lib/gecko-alpha/audit-snapshot/snapshot-last-ok && cat /var/lib/gecko-alpha/audit-snapshot/snapshot-last-ok && date +%s' > .ssh_verify_heartbeat.txt 2>&1
```

Read `.ssh_verify_heartbeat.txt`. Verify file exists; content is a unix timestamp within last few seconds of current time.

- [ ] **Step 8: Verify rows in audit table**

```bash
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "SELECT COUNT(*), COUNT(DISTINCT coin_id) FROM audit_volume_snapshot_phase_b"' > .ssh_verify_rows.txt 2>&1
```

Read `.ssh_verify_rows.txt`. Verify row count > 0; coin_id count matches the slow_burn cohort size (56 at time of plan-writing; will be higher at deploy time as more detections accumulate).

- [ ] **Step 9: Enable timers (start daily schedule)**

```bash
ssh root@89.167.116.187 'systemctl enable --now gecko-audit-snapshot.timer && systemctl enable --now gecko-audit-snapshot-watchdog.timer && systemctl list-timers gecko-audit-snapshot* --no-pager' > .ssh_enable_timers.txt 2>&1
```

Read `.ssh_enable_timers.txt`. Verify both timers active with next-trigger times.

- [ ] **Step 10: Watchdog smoke test (delete heartbeat, fire watchdog, verify Telegram delivery)**

This is a destructive smoke test: temporarily delete heartbeat file, fire watchdog manually, verify Telegram alert delivered, then re-create heartbeat from bootstrap timestamp.

```bash
ssh root@89.167.116.187 'mv /var/lib/gecko-alpha/audit-snapshot/snapshot-last-ok /tmp/snapshot-hb-backup && systemctl start gecko-audit-snapshot-watchdog.service ; sleep 3 ; journalctl -u gecko-audit-snapshot-watchdog.service -n 20 --no-pager ; echo "--- restoring heartbeat ---" ; mv /tmp/snapshot-hb-backup /var/lib/gecko-alpha/audit-snapshot/snapshot-last-ok ; cat /var/lib/gecko-alpha/audit-snapshot/snapshot-last-ok' > .ssh_watchdog_smoke.txt 2>&1
```

Read `.ssh_watchdog_smoke.txt`. Expected:
- Watchdog logs `STALE: gecko-audit-snapshot has not run successfully — heartbeat file MISSING`
- Watchdog logs `ALERT DELIVERED: HTTP 200`
- Telegram message arrives at operator chat (manually verify via Telegram app)
- Heartbeat file restored to original timestamp

- [ ] **Step 11: Open PR**

```bash
gh pr create --title "feat(audit): Phase B daily snapshot job for volume_history_cg" --body "$(cat <<'EOF'
## Summary

Closes the data-pruning blocker for Phase B audit: `volume_history_cg` is rolling-pruned at 7d (`scout/spikes/detector.py:55-57`), so D+14 evaluation 2026-05-24 can't read 2026-05-10 detection data without snapshot infrastructure. This PR ships:

- Schema migration: new `audit_volume_snapshot_phase_b` table (schema_version 20260518) with `UNIQUE (coin_id, recorded_at)` for idempotency
- Snapshot module: async function copies `volume_history_cg` rows for slow_burn-detected coin_ids in soak window
- CLI entrypoint: `scripts/gecko_audit_snapshot.py` + bash wrapper
- systemd: daily 04:00 UTC oneshot service + timer
- Watchdog: daily 10:00 UTC service + timer + bash script (30h staleness threshold; direct curl Telegram delivery bypassing scout.alerter per R6 CRITICAL)
- Audit brief revisions: data-source correction + B2 tail validation spec + cross-token gap analysis spec + data-source-lock clause

## Test plan

- [x] Unit tests: 4 snapshot module tests (empty cohort, basic capture, idempotency, window filter)
- [x] CLI integration tests: 3 subprocess tests (success + 2 exit-code paths)
- [x] Watchdog bash tests: 4 subprocess tests (fresh/stale/missing/corrupt heartbeat) — SKIP on Windows, run on VPS/WSL
- [ ] VPS deployment verification: bootstrap snapshot captured > 0 rows, heartbeat written, timers enabled
- [ ] Watchdog smoke test: delete heartbeat, fire watchdog, verify Telegram alert delivered HTTP 200

## Pre-registration (locked 2026-05-11)

Per `tasks/plan_audit_volume_snapshot_phase_b_2026_05_11.md` — all spec elements (cadence, scope, idempotency, failure mode, end-of-soak behavior) are pre-registered; implementation does not deviate.

## End-of-soak

Final scheduled run: 2026-05-25 (D+15). Operator disables `gecko-audit-snapshot.timer` after that date; table preserved as audit artifact. Watchdog timer can stay enabled or be disabled at operator discretion.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 12: Capture deployment summary (with operator-disable runbook at top)**

Create `tasks/deployed_audit_snapshot_2026_05_11.md` (separate file, not appended to plan) with the following structure. The **operator-must-disable step lands at the TOP** so it's visible at the calendar moment without scrolling — per R2-M3 amendment, end-of-soak handoff must be in a doc the operator actually visits when D+14 arrives.

```markdown
# Phase B audit-snapshot deployed — runbook + summary

## ⚠️ OPERATOR ACTION REQUIRED on 2026-05-25 (D+15)

After the final scheduled run completes at 04:00 UTC on 2026-05-25, **disable the timers**:

\`\`\`bash
# Branch-verification not needed here (no commits); just SSH
ssh root@89.167.116.187 'systemctl disable --now gecko-audit-snapshot.timer && systemctl disable --now gecko-audit-snapshot-watchdog.timer && systemctl list-timers gecko-audit-snapshot* --no-pager' > .ssh_disable_audit.txt 2>&1
\`\`\`

Verify both timers show as `inactive` / `disabled` in the output. The audit table `audit_volume_snapshot_phase_b` is preserved indefinitely as the audit artifact — do NOT drop it. The D+14 audit reads from this table on 2026-05-24.

After D+14 audit completes and findings are written: optionally compress/archive the table if disk pressure rises. Otherwise leave in place.

---

## Deployment summary

**Date deployed:** YYYY-MM-DD HH:MM UTC
**PR:** #NNN (merged at commit HASH)
**Branch:** feat/audit-volume-snapshot-phase-b → master

### Bootstrap snapshot result

- **rows_captured:** N
- **coin_ids_covered:** M (slow_burn cohort size at bootstrap time)
- **estimated_source_rows:** K (from pre-INSERT log)
- **Coverage report (per Step 6.5):**
  - FULL COVERAGE: X coins
  - PRE-DETECTION TRUNCATED: Y coins (expected per §10 of brief)
  - NO COVERAGE: Z coins (Z must be 0; if not, investigation required)

### Verifications

- [x] Heartbeat file written at `/var/lib/gecko-alpha/audit-snapshot/snapshot-last-ok` with valid unix timestamp
- [x] Audit table row count > 0
- [x] Both timers enabled with next-fire times
- [x] Watchdog smoke test: deleted heartbeat → STALE detected → Telegram delivered HTTP 200 → heartbeat restored
- [x] Disk pre-flight passed (df -h /root showed at least 10G free at deploy time)

### First scheduled run

Next 04:00 UTC fire: YYYY-MM-DD 04:00 UTC. Will verify journalctl after that fire confirms `audit_snapshot_completed` log + heartbeat update.

### Deviations from plan

(Should be zero per pre-registration. Document any here.)

### Cross-references

- Plan: `tasks/plan_audit_volume_snapshot_phase_b_2026_05_11.md`
- Brief: `tasks/audit_brief_phase_b_slow_burn_2026_05_11.md` §14 (end-of-soak runbook — duplicate of the action-required block above)
- Parallel discipline: `tasks/findings_silent_failure_audit_2026_05_11.md` §4 Priority 6 (table-freshness watchdog daemon TODO)
- Backlog entry: BL-NEW-AUDIT-SNAPSHOT (added 2026-05-11 per Task 9 of plan)
```

The deployed-summary file lives alongside the plan + brief in `tasks/`. Operator sees it when they navigate to the audit work at any future point. The action-required block at the top is the load-bearing element — D+14 evaluation reads brief §14 and post-deploy memory file simultaneously; both have the same disable command.

---

---

## Task 8: Cross-reference amendment for parallel discipline workstreams [✅ BOTH DIRECTIONS APPLIED 2026-05-11]

**Files:**
- Affected: `tasks/audit_brief_phase_b_slow_burn_2026_05_11.md` §15 (forward direction)
- Affected: `tasks/findings_silent_failure_audit_2026_05_11.md` §4 Priority 6 (reverse direction)

**Rationale:** Two parallel discipline workstreams converging on related watchdog infrastructure on 2026-05-11:

- This plan: snapshot-job heartbeat watchdog (per-job-run staleness)
- `findings_silent_failure_audit_2026_05_11.md`: table-freshness watchdog daemon (per-table-write staleness — deferred to Priority 6 of that doc)

Different concerns, no functional overlap. Stacked-failure mode (snapshot job silently dies AND the snapshot-job watchdog also fails) would let the audit data source silently degrade without alert. Adding `audit_volume_snapshot_phase_b` to the silent-failure-audit watchdog's monitored-tables list provides defense-in-depth.

Third recent instance of parallel discipline workstreams converging on shared primitives without cross-reference (closed-trades-pagination + live-trading-m1-5b WIP contention; BL-075 Phase A drift-check; now this).

- [x] **Step 1: Forward direction — audit brief §15 references silent-failure-audit watchdog**

Landed during brief recreation 2026-05-11 (commit 3f2de5d). The §15 entry reads:

> `tasks/findings_silent_failure_audit_2026_05_11.md` (same date) — parallel discipline workstream proposing a table-freshness watchdog daemon. When that ships, `audit_volume_snapshot_phase_b` should be on its monitored-tables list — provides defense-in-depth against stacked-failure mode (snapshot job dies + snapshot-job watchdog also dies).

- [x] **Step 2: Reverse direction — TODO in `findings_silent_failure_audit_2026_05_11.md`**

Landed at the end of **§4 Priority 6 (Watchdog daemon)** of that doc, immediately after the "Estimated build" line. Target section identified by grep on `^### Priority 6` 2026-05-11. The TODO content:

> **TODO (added 2026-05-11):** When implementing the watchdog daemon's monitored-tables list, include `audit_volume_snapshot_phase_b` per `tasks/plan_audit_volume_snapshot_phase_b_2026_05_11.md`. The snapshot-job's own watchdog (`gecko-audit-snapshot-watchdog.timer`) covers heartbeat-file staleness (per-run cessation); this daemon would cover table-write-staleness (per-table-row cessation) — siblings, not redundant. Stacked-failure mode the cross-reference prevents: snapshot job dies + snapshot-job-watchdog also dies → audit table silently stops receiving rows. With both watchdogs in place, either failure alerts independently.

- [ ] **Step 3: Commit (pending — bundled with other plan amendments in scope F)**

```bash
# Branch-verification pattern (locked 2026-05-11 after fix/tg-parse-mode-hygiene-audit incident):
CURRENT=$(git branch --show-current)
[[ "$CURRENT" == "feat/audit-volume-snapshot-phase-b" ]] || { echo "WRONG BRANCH: $CURRENT"; exit 1; }

git add tasks/findings_silent_failure_audit_2026_05_11.md tasks/plan_audit_volume_snapshot_phase_b_2026_05_11.md
git commit -m "docs(audit): cross-reference snapshot-job watchdog and table-freshness watchdog

Reverse-direction TODO lands at §4 Priority 6 of findings_silent_failure_audit_2026_05_11.md
(forward direction already in audit brief §15 via recreation commit 3f2de5d).

Two parallel discipline workstreams converging on related watchdog
infrastructure on 2026-05-11: this plan's snapshot-job heartbeat watchdog
(per-job-run staleness) and findings_silent_failure_audit's deferred-Priority-6
table-freshness watchdog daemon (per-table-write staleness).

Different concerns, no functional overlap, but stacked failures would
let the audit data source silently degrade. Cross-reference adds the
snapshot table to the silent-failure-audit watchdog's monitored-tables
list — defense-in-depth, not redundant."
```

---

## Task 9: Backlog entry for BL-NEW-AUDIT-SNAPSHOT [✅ APPLIED 2026-05-11]

**Files:**
- Modify: `backlog.md` (added BL-NEW-AUDIT-SNAPSHOT entry under "P2 — Infrastructure & Reliability" after BL-NEW-INGEST-WATCHDOG)

**Rationale (R2-M4):** Without a backlog entry, when the snapshot job lands as `BL-NEW-AUDIT-SNAPSHOT shipped` in next-session memory, it maps to nothing in backlog.md. Memory entries follow the pattern `BL-NNN deployed YYYY-MM-DD` → memory file; the backlog entry is the anchor for post-deploy summary writing. Adding it pre-merge is the cheap fix.

- [x] **Step 1: Insert BL-NEW-AUDIT-SNAPSHOT entry in backlog.md**

Landed 2026-05-11 (commit pending in scope F). Entry placed between BL-NEW-INGEST-WATCHDOG (line 233) and BL-034 (line 245), in the "P2 — Infrastructure & Reliability" section. Mirrors BL-NEW-INGEST-WATCHDOG's structure (Status / Tag / Files (planned) / Why / Drift verdict / Hermes verdict / Effect / Risks / Pre-registration discipline / Cross-references / Estimate).

- [x] **Step 2: Update entry status on deploy (post-merge, post-bootstrap)**

After successful bootstrap snapshot + timer enable (Task 7 Steps 6-9), update the entry's **Status** line from `PLANNED` to `SHIPPED YYYY-MM-DD (commit HASH)`. Use the branch-verification pattern for any commit on backlog.md:

```bash
CURRENT=$(git branch --show-current)
[[ "$CURRENT" == "feat/audit-volume-snapshot-phase-b" ]] || { echo "WRONG BRANCH: $CURRENT"; exit 1; }
# Edit backlog.md status line, then:
git add backlog.md
git commit -m "docs(backlog): BL-NEW-AUDIT-SNAPSHOT SHIPPED YYYY-MM-DD"
```

- [ ] **Step 3: Commit pre-deploy entry (bundled with other plan amendments in scope F)**

Initial entry commit (PLANNED status); status flip to SHIPPED happens post-deploy per Step 2.

---

## Self-Review

**Spec coverage check:**

- ✅ Cadence daily at 04:00 UTC — Task 4 timer
- ✅ Scope = all volume_history_cg rows for slow_burn-detected coin_ids in soak window — Task 2 snapshot function
- ✅ Destination = new audit_volume_snapshot_phase_b table — Task 1 migration
- ✅ Idempotency ON CONFLICT DO NOTHING — Task 1 UNIQUE constraint + Task 2 INSERT OR IGNORE
- ✅ Failure mode = watchdog Telegram alert (NOT heartbeat counter) — Task 5
- ✅ Verification = per-run structured logging — Task 2 structlog `audit_snapshot_completed`
- ✅ End-of-soak = final run 2026-05-25, operator disables timer — Task 7 Step 12 documentation + Task 6 brief update
- ✅ Bootstrap-now (capture pre-prune 2026-05-10 data before 2026-05-17) — Task 7 Step 6
- ✅ Audit brief revisions (4 updates) — Task 6 all 4 steps
- ✅ Cross-reference amendment for parallel watchdog workstreams — Task 8

**Placeholder scan:** no "TBD", no "implement later", no "similar to Task N" — all steps have concrete code or commands. ✓

**Type consistency:** function name `snapshot_volume_history_for_phase_b` is consistent across Tasks 2, 3, and tests. Table name `audit_volume_snapshot_phase_b` is consistent across Tasks 1, 2, 3, 6, 7. ✓

**Cross-task consistency:** Task 3 imports `scout.audit.snapshot` (created in Task 2) and `scout.db.Database` (modified in Task 1). Order is correct. Watchdog (Task 5) depends on heartbeat file written by snapshot CLI (Task 3). Order is correct.

---

## Notes for executor

- **Branch**: create `feat/audit-volume-snapshot-phase-b` before Task 1 if not already on a feature branch
- **Test discovery**: project uses `pytest-asyncio` auto mode (asyncio_mode = "auto" in pyproject.toml). `@pytest.mark.asyncio` decorator is technically redundant but included for explicitness — does not break.
- **Database import in tests**: `from scout.db import Database` requires that `scout/__init__.py` exists; the project uses src-layout via `scout/` package, so this should work directly.
- **VPS Python version**: VPS uses `uv` for dependency management; `uv run python scripts/gecko_audit_snapshot.py` is the canonical invocation. The bash wrapper uses this pattern.
- **systemd `StateDirectory=`**: creates `/var/lib/gecko-alpha/audit-snapshot/` automatically with correct permissions on first service start. No manual `mkdir` needed before deploy.
- **Schema migration timing (R1-M2 amendment)**: the migration is wired into `Database.initialize()` (line 105 of `scout/db.py`), which runs at pipeline startup AND on every snapshot-CLI invocation via `db.connect()`. The CLI is therefore **self-migrating** — pipeline restart is NOT a correctness prerequisite for the snapshot to work. Task 7 Step 5 (pipeline restart) exists for **early observability** of migration errors in pipeline journalctl (a failure there is easier to diagnose than the same failure on the first CLI run). Step 5 is also where deploy timing is checked against in-flight soak observations (M1.5c, BL-067, M1.5b).
- **PYCACHE invalidation**: `feedback_clear_pycache_on_deploy.md` mandates clearing `__pycache__` after `git pull`. Task 7 Step 2 includes this.

---

**End of plan.**
