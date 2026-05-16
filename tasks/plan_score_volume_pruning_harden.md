**New primitives introduced:** `SCORE_HISTORY_RETENTION_DAYS` Settings field (default 21), `VOLUME_SNAPSHOTS_RETENTION_DAYS` Settings field (default 21), Pydantic `@field_validator` enforcing retention ≥ `SECONDWAVE_COOLDOWN_MAX_DAYS`, `Database.prune_score_history(*, keep_days)` method, `Database.prune_volume_snapshots(*, keep_days)` method, two new SQLite indexes via migration (`idx_score_history_scanned_at`, `idx_volume_snapshots_scanned_at`), `_run_hourly_maintenance(db, settings, logger)` helper extracted from `scout/main.py` hourly block, `_run_extra_table_prune(db)` helper extracted from `scout/narrative/agent.py`, structured log events `score_history_pruned` / `volume_snapshots_pruned` / `score_history_prune_failed` / `volume_snapshots_prune_failed` / `extra_prune_table_error`.

# Plan: Harden score_history + volume_snapshots pruning

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close BL-NEW-SCORE-HISTORY-PRUNING + BL-NEW-VOLUME-SNAPSHOTS-PRUNING per §7a residual-gap rule. Existing 14-day pruning at `scout/narrative/agent.py:680-699` works, but has 4 defects (hardcoded threshold, narrative-loop coupling, silent except, no telemetry). Harden the existing implementation rather than build new.

**Architecture:** Mirror the in-tree pattern used by `prune_old_candidates` / `prune_perp_anomalies` / `prune_cryptopanic_posts` (methods on `Database`, called hourly from `scout/main.py`, retention via Settings). Move score_history + volume_snapshots out of narrative's daily loop; leave the other 6 tables (which are not in scope of these two backlog items) in narrative but fix the systemic `except Exception: pass` so they fail loudly.

**Tech Stack:** aiosqlite, Pydantic Settings, structlog, pytest-asyncio.

---

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| SQLite table pruning / retention | None — Hermes skill hub (689 skills as of 2026-05-16) lists no internal-DB-maintenance skills. Generic data-retention is project-internal. | Build in-tree. |
| Structured logging / observability for prune passes | None — Hermes Observability/MLOps skills cover external telemetry surfaces, not async-aiosqlite call-site logging. | Build in-tree. |
| Pydantic Settings field conventions | N/A — project already uses Pydantic Settings (`scout/config.py`). | Extend existing. |

**awesome-hermes-agent ecosystem check:** repo returned 404 on 2026-05-16; previous probes confirmed no overlap with internal-DB or async-Python-pruning. **One-sentence verdict:** project-internal hardening; no Hermes capability replaces this path.

---

## Drift check (post-audit)

- Existing implementation at `scout/narrative/agent.py:680-699` — 14-day pruning for 8 tables including score_history + volume_snapshots, runs inside narrative daily-learn loop.
- Existing pattern for other prunes — `Database.prune_old_candidates`, `Database.prune_perp_anomalies`, `Database.prune_cryptopanic_posts` (methods on Database class, hourly call from `scout/main.py:1702-1751`).
- Audit reference: `tasks/findings_backlog_drift_audit_2026_05_16.md` — BL-NEW-SCORE-HISTORY-PRUNING and BL-NEW-VOLUME-SNAPSHOTS-PRUNING categorized as Drift-partial.
- Backlog status reconciled in PR #135 (`docs/backlog-reconcile-2026-05-16` branch).

### §9a runtime-state verification on srilu-vps (2026-05-16T15:03Z)

Pre-plan runtime check (mandatory per CLAUDE.md §9a — proposed change depends on prod state):

| Assumption | Verified? | Evidence |
|---|---|---|
| `NARRATIVE_ENABLED=true` (existing prune is firing in prod) | ✅ Yes | `.env` on srilu: `NARRATIVE_ENABLED=true` |
| `SECONDWAVE_ENABLED=true` (race risk V2#1 is live) | ✅ Yes | `.env`: `SECONDWAVE_ENABLED=true` |
| `SECONDWAVE_COOLDOWN_MAX_DAYS` value | ✅ Default 14 | Not set in `.env`; uses `scout/config.py` default |
| Existing pruning is keeping tables at 14d | ✅ Yes | `score_history`: 5,892,842 rows, oldest=2026-05-02T01:36 (exactly 14d ago); `volume_snapshots`: 5,877,693 rows, same pattern |
| First prune pass risk (V2#2: lock storm if retention=14d on a never-pruned table) | ✅ Refuted | Tables already at steady-state 14d; first pass at retention=21d will delete 0 rows, retention=14d unchanged |
| Audit's "DRIFT-PARTIAL" categorization | ✅ Correct | Existing code path active in prod; PR is genuinely "harden existing" not "build new" |

**Key implication:** V2 review's MUST-FIX #2 (theoretical-pruning concern) is refuted by runtime evidence. V2 review's MUST-FIX #1 (secondwave race) and #3 (silent retention-vs-cooldown ordering) remain valid and are folded below.

**Residual gaps this plan closes:**
1. Hardcoded `14` retention — violates project CLAUDE.md "No hardcoded thresholds (must come from Settings / .env)"
2. Coupling to narrative daily-learn loop — disabling narrative silently disables pruning for these 2 tables
3. `except Exception: pass` at `agent.py:695-696` — Class 1 silent failure (CLAUDE.md §12a)
4. No row-count telemetry per pass
5. Race with `secondwave` JOIN at retention boundary (V2#1) — `get_secondwave_scan_candidates` reads up to `SECONDWAVE_COOLDOWN_MAX_DAYS`; prune at exactly that bound truncates evidence window
6. No structural prevention of retention-vs-cooldown silent mis-config (V2#3) — operator could set `SECONDWAVE_COOLDOWN_MAX_DAYS=30` without bumping retention, silently undercounting peaks
7. `DELETE WHERE scanned_at <= ?` against a 6M-row table without a `scanned_at`-leading index (existing indexes are `(contract_address, scanned_at)` — leading-column mismatch forces table scan)

**Residual gaps this plan does NOT close (deferred follow-up):**
- The other 6 tables in `agent.py:681-690` (`volume_spikes`, `momentum_7d`, `trending_snapshots`, `learn_logs`, `chain_matches`, `holder_snapshots`) remain in the narrative daily loop with hardcoded thresholds. They benefit from the silent-except fix (Class 1 surface) but parameterize+decouple is deferred. File `BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION` for follow-up after this PR lands.

---

## File map

- **Create:** none (extending existing files)
- **Modify:**
  - `scout/config.py` — add 2 Settings fields + 1 `@field_validator` enforcing retention ≥ `SECONDWAVE_COOLDOWN_MAX_DAYS`
  - `scout/db.py` — add 2 prune methods next to existing `prune_perp_anomalies` / `prune_cryptopanic_posts` (lines ~4692-4764); add 2 `CREATE INDEX IF NOT EXISTS` statements via the existing migration mechanism (NOT `_create_tables` — per memory `feedback_ddl_before_alter.md`)
  - `scout/main.py` — extract hourly block to `_run_hourly_maintenance(db, settings, logger)` helper; add 2 prune calls inside it (after `prune_perp_anomalies`)
  - `scout/narrative/agent.py` — extract loop at lines 680-699 to `_run_extra_table_prune(db)` helper; remove score_history + volume_snapshots from list; replace `except Exception: pass` with `logger.exception("extra_prune_table_error", table=table, days=days)`
- **Test (place in existing test files using established `db` fixture conventions):**
  - `tests/test_config.py` — 3 settings tests + 1 field-validator test (raise when retention < cooldown)
  - `tests/test_db.py` — 5 prune method tests; place at end of file inside existing `async def db(...)` fixture scope (see `tests/test_cryptopanic_db.py:12-17` for the canonical pattern if `test_db.py`'s fixture differs)
  - `tests/test_db.py` OR `tests/test_db_indexes.py` (existing) — 1 EXPLAIN QUERY PLAN test asserting `idx_*_scanned_at` is used
  - `tests/test_main.py` — 1 integration test asserting `_run_hourly_maintenance` calls both prune methods with Settings values
  - `tests/test_narrative_agent_prune.py` (new) — 1 test for `_run_extra_table_prune` silent-except fix using `structlog.testing.capture_logs()` context manager (NOT a `capture_logs` fixture — that does not exist in this codebase)

---

## Tasks

### Task 1: Add Settings fields

**Files:**
- Modify: `scout/config.py` (next to `CRYPTOPANIC_RETENTION_DAYS` ~line 288 OR `PERP_ANOMALY_RETENTION_DAYS` ~line 595 — pick whichever placement the existing file convention suggests)

- [ ] **Step 1.1: Write the failing tests**

```python
# tests/test_config.py
import pytest
from pydantic import ValidationError

def test_score_history_retention_default():
    from scout.config import Settings
    s = Settings(_env_file=None)
    assert s.SCORE_HISTORY_RETENTION_DAYS == 21

def test_volume_snapshots_retention_default():
    from scout.config import Settings
    s = Settings(_env_file=None)
    assert s.VOLUME_SNAPSHOTS_RETENTION_DAYS == 21

def test_score_history_retention_env_override(monkeypatch):
    monkeypatch.setenv("SCORE_HISTORY_RETENTION_DAYS", "30")
    from scout.config import Settings
    s = Settings(_env_file=None)
    assert s.SCORE_HISTORY_RETENTION_DAYS == 30

def test_retention_must_exceed_secondwave_cooldown(monkeypatch):
    """V2#3 fold: setting retention below SECONDWAVE_COOLDOWN_MAX_DAYS must raise."""
    monkeypatch.setenv("SCORE_HISTORY_RETENTION_DAYS", "7")
    monkeypatch.setenv("SECONDWAVE_COOLDOWN_MAX_DAYS", "14")
    from scout.config import Settings
    with pytest.raises(ValidationError, match="must be >= SECONDWAVE_COOLDOWN_MAX_DAYS"):
        Settings(_env_file=None)
```

- [ ] **Step 1.2: Run to verify failure**

```
uv run pytest tests/test_config.py::test_score_history_retention_default tests/test_config.py::test_volume_snapshots_retention_default tests/test_config.py::test_score_history_retention_env_override -v
```

Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'SCORE_HISTORY_RETENTION_DAYS'`.

- [ ] **Step 1.3: Add the Settings fields + field_validator**

In `scout/config.py`, in the appropriate section (likely after `CRYPTOPANIC_RETENTION_DAYS`):

```python
    SCORE_HISTORY_RETENTION_DAYS: int = 21
    VOLUME_SNAPSHOTS_RETENTION_DAYS: int = 21
```

And add a model_validator (Pydantic v2 idiom; `@model_validator(mode='after')`) at the bottom of the `Settings` class to enforce retention ≥ cooldown:

```python
    @model_validator(mode='after')
    def _validate_retention_covers_secondwave_window(self) -> "Settings":
        # V2#3 fold: prevent silent mis-config — retention shorter than
        # SECONDWAVE_COOLDOWN_MAX_DAYS truncates secondwave's score_history
        # evidence window for older alerts in the [3, MAX] cohort.
        # 7d buffer matches the new defaults (14 + 7 = 21).
        for field in ("SCORE_HISTORY_RETENTION_DAYS", "VOLUME_SNAPSHOTS_RETENTION_DAYS"):
            value = getattr(self, field)
            if value < self.SECONDWAVE_COOLDOWN_MAX_DAYS:
                raise ValueError(
                    f"{field}={value} must be >= SECONDWAVE_COOLDOWN_MAX_DAYS={self.SECONDWAVE_COOLDOWN_MAX_DAYS}"
                )
        return self
```

**Why default 21d not 14d** (V2#1 fold): srilu currently runs `SECONDWAVE_COOLDOWN_MAX_DAYS=14` (default). The `secondwave` detector at `scout/db.py:4285-4324` JOINs `score_history` for alerts in the `[3, MAX]` day window. At retention = cooldown exactly (14d), the hourly prune races the JOIN: rows just over the boundary get deleted before secondwave's scan reaches them. Bumping default retention to 21d (cooldown + 7d buffer) eliminates the race. Disk impact: ~6M rows → ~9M rows; ~600MB extra at full steady-state. Negligible vs cost of silent secondwave undercounts.

**Migration risk** (V2#2 refuted; documented for record): per §9a runtime verification, narrative loop has been pruning to 14d on srilu. Bumping retention to 21d means rows that would have been deleted by the next narrative pass survive an extra 7 days. First main.py prune pass at 21d deletes 0 rows. No backlog spike.

- [ ] **Step 1.4: Run to verify pass**

```
uv run pytest tests/test_config.py -k "retention" -v
```

Expected: PASS (3 new tests).

- [ ] **Step 1.5: Commit**

```
git add scout/config.py tests/test_config.py
git commit -m "feat(config): SCORE_HISTORY_RETENTION_DAYS + VOLUME_SNAPSHOTS_RETENTION_DAYS"
```

---

### Task 1.5: Migration — `scanned_at` indexes for prune coverage

**Why:** Existing indexes `idx_score_hist_addr (contract_address, scanned_at)` and `idx_volume_snap_addr (contract_address, scanned_at)` are leading-column-mismatched for `DELETE WHERE scanned_at <= ?`. SQLite cannot use them; the DELETE will table-scan 6M rows. Per memory `feedback_ddl_before_alter.md`, `CREATE INDEX IF NOT EXISTS` placed in `_create_tables` is a no-op on prod (tables already exist); must use a dedicated migration step.

**Files:**
- Modify: `scout/db.py` — add `_migrate_score_volume_prune_indexes` method; register it in `initialize()` after the existing `_migrate_*` calls (around line 108)
- Test: `tests/test_db_indexes.py` (existing per Glob check) OR `tests/test_db.py`

- [ ] **Step 1.5.1: Write the failing test (EXPLAIN QUERY PLAN)**

```python
# tests/test_db.py (or test_db_indexes.py if it exists)
@pytest.mark.asyncio
async def test_prune_score_history_uses_scanned_at_index(db):
    cur = await db._conn.execute(
        "EXPLAIN QUERY PLAN DELETE FROM score_history WHERE scanned_at <= ?",
        ("2026-01-01T00:00:00+00:00",),
    )
    plan = await cur.fetchall()
    plan_str = " ".join(str(row[3]) for row in plan)
    assert "idx_score_history_scanned_at" in plan_str, f"Index not used: {plan_str}"

@pytest.mark.asyncio
async def test_prune_volume_snapshots_uses_scanned_at_index(db):
    cur = await db._conn.execute(
        "EXPLAIN QUERY PLAN DELETE FROM volume_snapshots WHERE scanned_at <= ?",
        ("2026-01-01T00:00:00+00:00",),
    )
    plan = await cur.fetchall()
    plan_str = " ".join(str(row[3]) for row in plan)
    assert "idx_volume_snapshots_scanned_at" in plan_str, f"Index not used: {plan_str}"
```

- [ ] **Step 1.5.2: Run to verify failure**

```
uv run pytest tests/test_db.py -k "scanned_at_index" -v
```

Expected: FAIL — indexes don't exist yet.

- [ ] **Step 1.5.3: Add migration**

In `scout/db.py`, after `_migrate_minara_alert_emissions_v1` (~line 3432), add:

```python
    async def _migrate_score_volume_prune_indexes(self) -> None:
        """Add scanned_at indexes for hourly prune DELETE coverage.

        Existing idx_score_hist_addr / idx_volume_snap_addr have
        contract_address as leading column — unusable for time-only
        predicate in DELETE WHERE scanned_at <= ?.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "score_volume_prune_indexes_v1"

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?", (migration_name,)
            )
            row = await cur.fetchone()
            if row is not None:
                await conn.execute("COMMIT")
                return
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_score_history_scanned_at "
                "ON score_history(scanned_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_volume_snapshots_scanned_at "
                "ON volume_snapshots(scanned_at)"
            )
            await conn.execute(
                "INSERT INTO paper_migrations(name, cutover_ts) VALUES (?, ?)",
                (migration_name, datetime.now(timezone.utc).isoformat()),
            )
            await conn.execute("COMMIT")
            _log.info("score_volume_prune_indexes_migrated")
        except Exception:
            await conn.execute("ROLLBACK")
            _log.exception("score_volume_prune_indexes_migration_failed")
            raise
```

Then in `initialize()` after `await self._migrate_minara_alert_emissions_v1()` (~line 108), add:

```python
        await self._migrate_score_volume_prune_indexes()
```

- [ ] **Step 1.5.4: Run to verify pass**

```
uv run pytest tests/test_db.py -k "scanned_at_index" -v
```

Expected: PASS (2 tests).

- [ ] **Step 1.5.5: Verify migration idempotency**

```
uv run pytest tests/test_db.py -v  # full module
```

Expected: no regressions; migration runs once + skips on re-initialize.

- [ ] **Step 1.5.6: Commit**

```
git add scout/db.py tests/test_db.py
git commit -m "feat(db): scanned_at indexes for score/volume prune coverage"
```

**Migration deploy note:** on srilu, the migration will create the index on a 6M-row table. SQLite CREATE INDEX is O(N log N) — estimated ~30-60 seconds wall time. Single short write lock. Acceptable but should be flagged in PR description.

---

### Task 2: Database.prune_score_history method

**Files:**
- Modify: `scout/db.py` (next to `prune_perp_anomalies` ~line 4692)
- Test: `tests/test_db.py` — place tests using the file's existing `db` fixture. If `test_db.py` lacks one, mirror the `tests/test_cryptopanic_db.py:12-17` pattern: `@pytest.fixture async def db(tmp_path): ...`. Do NOT create a new file unless necessary; the existing pattern is the canonical entry-point.

- [ ] **Step 2.1: Write the failing tests**

```python
# tests/test_db.py — APPEND to existing file using its db fixture
import pytest
from datetime import datetime, timedelta, timezone

@pytest.mark.asyncio
async def test_prune_score_history_keeps_recent(db):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=5)).isoformat()
    old = (now - timedelta(days=20)).isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xRECENT", 50.0, recent),
    )
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xOLD", 50.0, old),
    )
    await db._conn.commit()

    deleted = await db.prune_score_history(keep_days=14)

    assert deleted == 1
    cur = await db._conn.execute("SELECT contract_address FROM score_history")
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["0xRECENT"]

@pytest.mark.asyncio
async def test_prune_score_history_empty_table_returns_zero(db):
    deleted = await db.prune_score_history(keep_days=14)
    assert deleted == 0

@pytest.mark.asyncio
async def test_prune_score_history_keep_days_zero_deletes_all(db):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xANY", 50.0, now),
    )
    await db._conn.commit()
    deleted = await db.prune_score_history(keep_days=0)
    assert deleted == 1

@pytest.mark.asyncio
async def test_prune_score_history_tie_on_cutoff_deletes(db):
    """V1#11 fold: lock in <= semantic. Row with scanned_at == cutoff must be pruned.

    Matches the cryptopanic_posts comment at db.py:4754-4758 (Windows
    clock-tie boundary). If someone "fixes" this to < in the future, this
    test catches it.
    """
    # Insert a row at exactly the cutoff timestamp
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=14)
    cutoff_iso = cutoff_dt.isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xTIE", 50.0, cutoff_iso),
    )
    await db._conn.commit()
    deleted = await db.prune_score_history(keep_days=14)
    assert deleted == 1
```

- [ ] **Step 2.2: Run to verify failure**

```
uv run pytest tests/test_db.py::test_prune_score_history_keeps_recent -v
```

Expected: FAIL with `AttributeError: 'Database' object has no attribute 'prune_score_history'`.

- [ ] **Step 2.3: Implement the method**

In `scout/db.py`, after `prune_perp_anomalies`:

```python
    async def prune_score_history(self, *, keep_days: int) -> int:
        """Delete score_history rows older than ``keep_days``. Returns rowcount.

        Uses <= so that scanned_at == cutoff prunes (matches prune_cryptopanic_posts
        semantics — keep_days=0 means "retain nothing as old as now").

        Return-type contract: ``cur.rowcount or 0`` — matches sibling
        ``prune_perp_anomalies`` (db.py:4699). Diverges from
        ``prune_cryptopanic_posts`` (db.py:4764, no ``or 0``) — that's an
        existing inconsistency in the codebase; we follow the safer
        coalesce-to-zero form. Per V1#3 review fold.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM score_history WHERE scanned_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0
```

- [ ] **Step 2.4: Run to verify pass**

```
uv run pytest tests/test_db.py -k "prune_score_history" -v
```

Expected: PASS (3 new tests).

- [ ] **Step 2.5: Commit**

```
git add scout/db.py tests/test_db.py
git commit -m "feat(db): prune_score_history method with TDD coverage"
```

---

### Task 3: Database.prune_volume_snapshots method

**Files:**
- Modify: `scout/db.py` (next to `prune_score_history`)
- Test: `tests/test_db.py`

- [ ] **Step 3.1: Write the failing test (mirror of Task 2)**

```python
@pytest.mark.asyncio
async def test_prune_volume_snapshots_keeps_recent(db):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=5)).isoformat()
    old = (now - timedelta(days=20)).isoformat()
    await db._conn.execute(
        "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) VALUES (?, ?, ?)",
        ("0xRECENT", 100000.0, recent),
    )
    await db._conn.execute(
        "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) VALUES (?, ?, ?)",
        ("0xOLD", 100000.0, old),
    )
    await db._conn.commit()

    deleted = await db.prune_volume_snapshots(keep_days=14)

    assert deleted == 1
    cur = await db._conn.execute("SELECT contract_address FROM volume_snapshots")
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["0xRECENT"]

@pytest.mark.asyncio
async def test_prune_volume_snapshots_empty_table_returns_zero(db):
    deleted = await db.prune_volume_snapshots(keep_days=14)
    assert deleted == 0
```

- [ ] **Step 3.2: Run to verify failure**

```
uv run pytest tests/test_db.py::test_prune_volume_snapshots_keeps_recent -v
```

Expected: FAIL.

- [ ] **Step 3.3: Implement**

```python
    async def prune_volume_snapshots(self, *, keep_days: int) -> int:
        """Delete volume_snapshots rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM volume_snapshots WHERE scanned_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0
```

- [ ] **Step 3.4: Run to verify pass**

```
uv run pytest tests/test_db.py -k "prune_volume_snapshots" -v
```

Expected: PASS.

- [ ] **Step 3.5: Commit**

```
git add scout/db.py tests/test_db.py
git commit -m "feat(db): prune_volume_snapshots method with TDD coverage"
```

---

### Task 4: Extract `_run_hourly_maintenance` + wire into main.py hourly loop

**V1#7 fold: commit to extraction.** The hand-wavy integration-test alternative ("OR call run_cycle with mocked time") is rejected. Extract first; test the helper directly. This mirrors the Task 5 extraction pattern (`_run_extra_table_prune`).

**Files:**
- Modify: `scout/main.py` (~lines 1702-1751, the hourly block)
- Test: `tests/test_main.py`

- [ ] **Step 4.1: Extract the hourly block (refactor commit — no behavior change)**

In `scout/main.py`, extract the block at lines 1702-1751 into a helper at module level (NOT inside `run_pipeline`):

```python
async def _run_hourly_maintenance(db, session, settings, args, logger) -> None:
    """Hourly maintenance tasks: outcome check + table prune.

    Extracted from inline run_pipeline loop for testability. No behavior
    change in this commit — purely structural.
    """
    try:
        outcomes_recorded = await check_outcomes(db, session)
        if outcomes_recorded:
            logger.info("Outcomes checked", recorded=outcomes_recorded)
    except Exception as e:
        logger.warning("Outcome check error", error=str(e))

    # Prune old candidates if DB > 500MB
    try:
        db_size = (
            settings.DB_PATH.stat().st_size
            if settings.DB_PATH.exists()
            else 0
        )
        if db_size > 500_000_000:
            pruned = await db.prune_old_candidates(keep_days=7)
            logger.info(
                "db_pruned",
                rows_deleted=pruned,
                db_size_mb=round(db_size / 1e6, 1),
            )
    except Exception as e:
        logger.warning("DB prune error", error=str(e))

    try:
        await db.prune_perp_anomalies(
            keep_days=settings.PERP_ANOMALY_RETENTION_DAYS
        )
    except Exception as e:
        logger.warning("perp_anomaly_prune_error", error=str(e))

    if settings.CRYPTOPANIC_ENABLED:
        try:
            pruned_cp = await db.prune_cryptopanic_posts(
                keep_days=settings.CRYPTOPANIC_RETENTION_DAYS
            )
            if pruned_cp:
                logger.info("cryptopanic_pruned", rows_deleted=pruned_cp)
        except Exception:
            logger.exception("cryptopanic_prune_failed")
```

Replace the inline block at line 1702-1751 with:

```python
                    if now - last_outcome_check >= outcome_check_interval:
                        await _run_hourly_maintenance(db, session, settings, args, logger)
                        last_outcome_check = now
```

Run the existing test suite to confirm no regression:

```
uv run pytest tests/test_main.py -v
```

Commit:

```
git add scout/main.py
git commit -m "refactor(main): extract _run_hourly_maintenance helper (no behavior change)"
```

- [ ] **Step 4.2: Write the failing integration test**

```python
# tests/test_main.py
import pytest
from unittest.mock import AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_run_hourly_maintenance_calls_score_history_prune(tmp_path):
    """V1#7 fold: assert _run_hourly_maintenance calls both new prune methods."""
    from scout.main import _run_hourly_maintenance
    from scout.config import Settings

    settings = Settings(_env_file=None, DB_PATH=tmp_path / "scout.db")
    db = MagicMock()
    db.prune_old_candidates = AsyncMock(return_value=0)
    db.prune_perp_anomalies = AsyncMock(return_value=0)
    db.prune_cryptopanic_posts = AsyncMock(return_value=0)
    db.prune_score_history = AsyncMock(return_value=0)
    db.prune_volume_snapshots = AsyncMock(return_value=0)
    session = MagicMock()
    args = MagicMock(dry_run=False)
    logger = MagicMock()

    # check_outcomes will fail (no real db) — caught by the outer try
    await _run_hourly_maintenance(db, session, settings, args, logger)

    db.prune_score_history.assert_awaited_once_with(
        keep_days=settings.SCORE_HISTORY_RETENTION_DAYS
    )
    db.prune_volume_snapshots.assert_awaited_once_with(
        keep_days=settings.VOLUME_SNAPSHOTS_RETENTION_DAYS
    )
```

- [ ] **Step 4.2: Run to verify failure**

```
uv run pytest tests/test_main.py -k "score_history_prune" -v
```

Expected: FAIL — prune call not wired.

- [ ] **Step 4.3: Wire the prune calls inside `_run_hourly_maintenance`**

After the existing cryptopanic block inside `_run_hourly_maintenance`, add:

```python
    try:
        pruned_sh = await db.prune_score_history(
            keep_days=settings.SCORE_HISTORY_RETENTION_DAYS
        )
        if pruned_sh:
            logger.info(
                "score_history_pruned",
                rows_deleted=pruned_sh,
                keep_days=settings.SCORE_HISTORY_RETENTION_DAYS,
            )
        else:
            logger.debug(
                "score_history_pruned",
                rows_deleted=0,
                keep_days=settings.SCORE_HISTORY_RETENTION_DAYS,
            )
    except Exception:
        logger.exception("score_history_prune_failed")

    try:
        pruned_vs = await db.prune_volume_snapshots(
            keep_days=settings.VOLUME_SNAPSHOTS_RETENTION_DAYS
        )
        if pruned_vs:
            logger.info(
                "volume_snapshots_pruned",
                rows_deleted=pruned_vs,
                keep_days=settings.VOLUME_SNAPSHOTS_RETENTION_DAYS,
            )
        else:
            logger.debug(
                "volume_snapshots_pruned",
                rows_deleted=0,
                keep_days=settings.VOLUME_SNAPSHOTS_RETENTION_DAYS,
            )
    except Exception:
        logger.exception("volume_snapshots_prune_failed")
```

**Transaction note** (V1#9 fold): each prune commits independently; the two consecutive prune calls are NOT wrapped in a single transaction. This is intentional — the operations are independent of each other, and concurrent INSERTs from the scorer loop should be allowed to interleave. SQLite WAL mode + per-DELETE commit handles writer contention without batched-txn complexity.

- [ ] **Step 4.4: Run to verify pass**

```
uv run pytest tests/test_main.py -k "prune" -v
```

Expected: PASS.

- [ ] **Step 4.5: Commit**

```
git add scout/main.py tests/test_main.py
git commit -m "feat(main): hourly prune of score_history + volume_snapshots via Settings"
```

---

### Task 5: Remove score_history + volume_snapshots from narrative loop + fix systemic silent-except

**Files:**
- Modify: `scout/narrative/agent.py:680-699`
- Test: extend existing narrative agent test OR add focused test for the silent-except fix

- [ ] **Step 5.1: Write the failing test**

**V1#1 fold: use `structlog.testing.capture_logs()` as a context manager — there is NO `capture_logs` pytest fixture in this codebase.** Verified via grep across `tests/` 2026-05-16.

**V1#8 fold: assert exactly 6 errors (one per remaining table), not just `>= 1`. Tighter assertion catches fault-isolation regressions.**

```python
# tests/test_narrative_agent_prune.py (new)
import pytest
import structlog
from unittest.mock import AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_narrative_extra_prune_logs_error_per_table():
    """When narrative's extra-prune loop hits a bad table, it must log a
    structured error PER TABLE (fault isolation), not silent pass.

    V1#1 + V1#8 review fold: 6 tables remain in the helper after
    score_history + volume_snapshots are extracted; all 6 should log.
    """
    db = MagicMock()
    db._conn = MagicMock()
    db._conn.execute = AsyncMock(side_effect=RuntimeError("simulated table missing"))
    db._conn.commit = AsyncMock()

    from scout.narrative.agent import _run_extra_table_prune

    with structlog.testing.capture_logs() as cap_logs:
        await _run_extra_table_prune(db)

    error_logs = [e for e in cap_logs if e.get("event") == "extra_prune_table_error"]
    assert len(error_logs) == 6, f"Expected 6 (one per table), got {len(error_logs)}"
    seen_tables = {e["table"] for e in error_logs}
    assert seen_tables == {
        "volume_spikes", "momentum_7d", "trending_snapshots",
        "learn_logs", "chain_matches", "holder_snapshots",
    }
```

- [ ] **Step 5.2: Run to verify failure**

```
uv run pytest tests/test_narrative_agent_prune.py -v
```

Expected: FAIL — helper doesn't exist OR `except Exception: pass` swallows the error.

- [ ] **Step 5.3: Refactor narrative/agent.py and fix the systemic silent-except**

Extract the in-line loop at `agent.py:680-699` to a helper function `_run_extra_table_prune(db)`, remove the 2 score/volume entries from the list, and replace `except Exception: pass` with structured logging:

```python
async def _run_extra_table_prune(db) -> None:
    """Prune the 6 narrative-owned tables on the daily loop.

    NOTE: score_history + volume_snapshots are pruned hourly from scout.main
    via db.prune_score_history / db.prune_volume_snapshots — they are NOT in
    this list. The other 6 tables (volume_spikes, momentum_7d,
    trending_snapshots, learn_logs, chain_matches, holder_snapshots) are
    pending deferred parameterize+decouple — see backlog
    BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION.
    """
    for table, col, days in [
        ("volume_spikes", "detected_at", 30),
        ("momentum_7d", "detected_at", 30),
        ("trending_snapshots", "snapshot_at", 7),
        ("learn_logs", "created_at", 90),
        ("chain_matches", "completed_at", 30),
        ("holder_snapshots", "scanned_at", 14),
    ]:
        try:
            await db._conn.execute(
                f"DELETE FROM {table} WHERE datetime({col}) < datetime('now', '-{days} days')"
            )
        except Exception:
            logger.exception("extra_prune_table_error", table=table, days=days)
    try:
        await db._conn.commit()
    except Exception:
        logger.exception("extra_prune_commit_error")
```

Replace the existing `for table, col, days in [...]: try: ... except Exception: pass` block at `agent.py:680-699` with `await _run_extra_table_prune(db)`.

**V1#2 fold: drop the outer `try: ... except Exception: logger.exception("extra_prune_error")` wrapper.** With the helper handling per-table errors AND a separate `try/except` around `commit()`, the outer wrapper becomes dead code — it can only catch import errors or attribute-resolution bugs that occur before the loop, which are not realistic prod paths. Removing it makes the call site cleaner. Replace the old `try/except` block entirely with the single helper call:

```python
                    await _run_extra_table_prune(db)
```

- [ ] **Step 5.4: Run to verify pass**

```
uv run pytest tests/test_narrative_agent_prune.py -v
```

Expected: PASS — structured log emitted.

- [ ] **Step 5.5: Run broader regression to verify no narrative behavior change**

```
uv run pytest tests/test_narrative*.py -v
```

Expected: existing narrative tests pass (no behavior change to narrative).

- [ ] **Step 5.6: Commit**

```
git add scout/narrative/agent.py tests/test_narrative_agent_prune.py
git commit -m "fix(narrative): structured logging for extra-prune errors + decouple score/volume"
```

---

### Task 6: File deferred follow-up + verify full regression

**Files:**
- Modify: `backlog.md` — add `BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION` entry
- Modify: `tasks/findings_backlog_drift_audit_2026_05_16.md` — note this PR closes the 2 drift-partial items

- [ ] **Step 6.1: File the deferred follow-up entry**

Append to `backlog.md` under the P2 section:

```markdown
### BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION: parameterize + decouple remaining 6 narrative-owned prunes
**Status:** PROPOSED 2026-05-16 — filed as residual from this PR's §7a partial-match reframe.
**Why:** The PR that closed BL-NEW-SCORE-HISTORY-PRUNING + BL-NEW-VOLUME-SNAPSHOTS-PRUNING extracted those 2 tables out of `scout/narrative/agent.py` and parameterized them via Settings. The remaining 6 tables in `_run_extra_table_prune` (`volume_spikes`, `momentum_7d`, `trending_snapshots`, `learn_logs`, `chain_matches`, `holder_snapshots`) still use hardcoded retention values and run only inside the narrative daily-learn loop. Same defect class.
**Action:** add 6 Settings fields + 6 prune methods + hourly wiring (mirror the score/volume pattern). Per-table retention can be tuned independently.
**decision-by:** 6 weeks (lower urgency than score/volume — these tables write at slower rates).
```

- [ ] **Step 6.2: Update audit doc**

Append to `tasks/findings_backlog_drift_audit_2026_05_16.md`:

```markdown
---
## 2026-05-16 closure note

BL-NEW-SCORE-HISTORY-PRUNING + BL-NEW-VOLUME-SNAPSHOTS-PRUNING closed by PR #<TBD>
(branch `feat/score-volume-pruning-harden`). Reframed scope per §7a residual-gap
rule. Filed BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION for the remaining 6 tables.
```

- [ ] **Step 6.3: Full regression**

```
uv run pytest --tb=short -q
```

Expected: full suite passes (or at minimum, no NEW failures vs main). Note any pre-existing baseline failures in PR description.

- [ ] **Step 6.4: Commit**

```
git add backlog.md tasks/findings_backlog_drift_audit_2026_05_16.md
git commit -m "docs(backlog): file BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION residual"
```

---

## Test plan summary

- 4 config tests (3 settings defaults/override + 1 field-validator)
- 2 index EXPLAIN tests (score + volume use the new `idx_*_scanned_at`)
- 6 db prune tests (prune_score_history: keeps_recent, empty, keep_days_zero, tie-on-cutoff; prune_volume_snapshots: keeps_recent, empty)
- 1 narrative test (silent-except → structured-log, asserts count=6 per-table)
- 1 integration test (`_run_hourly_maintenance` calls both prune methods with Settings values)
- Full regression must pass (or baseline-only failures called out in PR description)

Total: 14 new tests + full regression.

---

## Deployment verification (after PR review + merge — operator-gated, NOT autonomous)

After deploy on srilu-vps:

1. Verify settings load: `journalctl -u gecko-pipeline | grep SCORE_HISTORY_RETENTION_DAYS` — confirm setting visible at startup
2. Wait 1-2 hours for first hourly maintenance loop firing
3. Verify structured log emits: `journalctl -u gecko-pipeline --since "1 hour ago" | grep -E "score_history_pruned|volume_snapshots_pruned"` — expect at least 1 of each per hour with `rows_deleted` field
4. Verify no silent failures: `journalctl -u gecko-pipeline | grep "_prune_failed"` — should be empty unless real failure
5. Optionally: `sqlite3 scout.db "SELECT COUNT(*) FROM score_history WHERE scanned_at < datetime('now', '-14 days')"` — should return 0

**Revert path if needed:**
- `.env` override: `SCORE_HISTORY_RETENTION_DAYS=365` + `VOLUME_SNAPSHOTS_RETENTION_DAYS=365` effectively disables pruning without code rollback
- Full rollback: revert the PR's main.py block; narrative loop's score/volume pruning will resume via the agent.py removal being reverted

---

## Out of scope (explicit non-goals)

- **Per-token rolling retention** ("keep last N=10 per contract_address") — original backlog Action mentioned this as an alternative. **V2#4 review fold:** the rejection rationale was incomplete; `get_recent_scores` at `db.py:4157` uses `LIMIT N` (per-token semantics), so per-token retention would actually match one reader's semantics better than time-based. Time-based 21d is retained because (a) `secondwave` at `db.py:4285` needs time-bounded coverage (per-token would be ambiguous for it), (b) hourly time-bounded prune is simpler, (c) `get_recent_scores`'s `LIMIT N` is a read-side constraint, not a write-side requirement. Per-token retention is filed as a separate consideration if/when 21d proves insufficient.
- The other 6 narrative-pruned tables — filed as BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION (Task 6 step 6.1).
- §12a freshness SLO / watchdog for score_history + volume_snapshots — gated on §12a daemon being built (separate backlog item BL-NEW-SCORE-HISTORY-WATCHDOG-SLO + BL-NEW-VOLUME-SNAPSHOTS-WATCHDOG-SLO).
- Backfill / staged-delete for first prune pass — refuted by §9a verification: existing prune is firing, tables are already at 14d boundary, retention=21d default means first pass deletes 0 rows.
- VPS deployment — operator-gated per user "do not deploy until PR is reviewed by me" rule.
