**New primitives introduced:** `Database.probe_wal_state()` method returning `dict` with `wal_size_bytes`, `wal_pages`, `db_size_bytes`, `freelist_count`, `journal_mode`, `wal_autocheckpoint`. Structured log events `sqlite_wal_probe` (info, hourly) and `sqlite_wal_bloat_observed` (warning, threshold breach). New `SQLITE_WAL_PROFILE_ENABLED: bool = True` Settings flag + `SQLITE_WAL_BLOAT_BYTES: int = 50_000_000` threshold setting. Hourly hook in `_run_hourly_maintenance`. Operator helper `scripts/wal_summary.sh` (mirrors cycle 3's `tg_burst_summary.sh` shape). Filed follow-up `BL-NEW-SQLITE-WAL-TUNING-DECISION` with pre-registered criteria.

# Plan: BL-NEW-SQLITE-WAL-PROFILE — instrument SQLite WAL bloat

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Measure SQLite WAL file size + freelist bloat at gecko-alpha's ~17k writes/hr/table combined load (score_history + volume_snapshots + candidates upsert + cycle 2's 6 narrative-table prunes). Pre-registered decision: TUNE `wal_autocheckpoint` (or add explicit `PRAGMA wal_checkpoint(TRUNCATE)` cadence) if bloat exceeds threshold; ACCEPT otherwise.

**Architecture:** Lightweight `Database.probe_wal_state()` method called once per hour from `_run_hourly_maintenance` (next to the existing prune calls). Emits structured log with current WAL/DB sizes. Operator summary script analyzes journalctl + archive over the 4-week soak. Pattern mirrors cycle 3's TG-burst-profile exactly.

**Tech Stack:** aiosqlite (existing), structlog, Pydantic Settings.

## Decision criteria (pre-registered per V14 anchor)

After the 4-week measurement window (~2026-06-14):

| Condition | Action |
|---|---|
| `sqlite_wal_bloat_observed` fires sustained (≥12 consecutive hourly probes with wal_size_bytes > `SQLITE_WAL_BLOAT_BYTES`) | **TUNE** — lower `wal_autocheckpoint` from default 1000 pages OR add explicit `PRAGMA wal_checkpoint(TRUNCATE)` after each hourly prune |
| Any single probe shows wal_size_bytes > 500MB | **TUNE-IMMEDIATELY** — escalate, runaway WAL |
| freelist_count > 10% of total db pages sustained | **VACUUM scheduled** (separate scope) |
| Zero bloat events in 4 weeks | **ACCEPT** (default config sufficient at observed load) |

Filed `BL-NEW-SQLITE-WAL-TUNING-DECISION` with these criteria + memory checkpoint for 2026-06-14.

---

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| SQLite WAL size monitoring / autocheckpoint tuning | None — Hermes Skill Hub (DevOps + MLOps categories, 2026-05-17 probe). | Build in-tree. |
| Generic SQLite PRAGMA helpers | None — aiosqlite + raw PRAGMA calls suffice | Build in-tree. |

awesome-hermes-agent: 404 (consistent). **Verdict:** custom-code path; mirror cycle 3's measurement-layer pattern.

## Drift check (post-fetch)

- **Step 0 (per memory `feedback_drift_check_after_pull.md`):** `git fetch && git log -10 origin/master` → top is `a36acd6` cycle 3 merge. Branch off origin/master.
- `grep wal_size|wal_pages|wal_autocheckpoint scout/ scripts/` → no matches. Net-new instrumentation surface.
- `scout/db.py:86 PRAGMA journal_mode=WAL` already enabled at initialize. Default `wal_autocheckpoint = 1000 pages`; default `journal_size_limit = -1` (no limit). Net-new instrumentation surface.

Backlog: `BL-NEW-SQLITE-WAL-PROFILE` filed 2026-05-13 from BL-NEW-CYCLE-CHANGE-AUDIT. decision-by 8 weeks. This PR measures; the follow-up `BL-NEW-SQLITE-WAL-TUNING-DECISION` files the decision.

---

## File map

- **Modify:**
  - `scout/db.py` — add `Database.probe_wal_state()` method (~line 4900 next to other PRAGMA-using helpers)
  - `scout/main.py` — call `db.probe_wal_state()` + structured log in `_run_hourly_maintenance` (after the 6 narrative prunes from cycle 2)
  - `scout/config.py` — add `SQLITE_WAL_PROFILE_ENABLED: bool = True` + `SQLITE_WAL_BLOAT_BYTES: int = 50_000_000`
- **Create:**
  - `scripts/wal_summary.sh` — read journalctl `sqlite_wal_probe` + `sqlite_wal_bloat_observed` events; emit summary (max wal_size, % of probes over threshold, time-of-day histogram of bloat events)
  - `scripts/wal_archive.sh` — weekly cron dumping events to `/var/log/gecko-alpha/wal-archive/` (mirrors cycle 3 archive script)
  - `tests/test_wal_probe.py` — unit tests for `probe_wal_state()`
- **Backlog:** close BL-NEW-SQLITE-WAL-PROFILE + file BL-NEW-SQLITE-WAL-TUNING-DECISION
- **Memory:** `project_sqlite_wal_tuning_checkpoint_2026_06_14.md` (cycle 1/3 pattern)

---

## Tasks

### Task 1: Settings + probe method

**Files:** `scout/config.py`, `scout/db.py`, `tests/test_config.py`, `tests/test_wal_probe.py`

- [ ] **Step 1.1: Failing tests**

```python
# tests/test_config.py — append
def test_sqlite_wal_profile_enabled_default_true():
    s = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.SQLITE_WAL_PROFILE_ENABLED is True


def test_sqlite_wal_bloat_bytes_default_50mb():
    s = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.SQLITE_WAL_BLOAT_BYTES == 50_000_000
```

```python
# tests/test_wal_probe.py (new)
import pytest
from pathlib import Path
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "wal_test.db"))
    await database.initialize()
    yield database
    await database.close()


async def test_probe_wal_state_returns_required_fields(db):
    state = await db.probe_wal_state()
    assert "wal_size_bytes" in state
    assert "wal_pages" in state
    assert "db_size_bytes" in state
    assert "freelist_count" in state
    assert "journal_mode" in state
    assert state["journal_mode"] == "wal"  # PRAGMA journal_mode=WAL set in initialize
    assert "wal_autocheckpoint" in state
    assert isinstance(state["wal_autocheckpoint"], int)


async def test_probe_wal_state_after_writes(db, token_factory):
    # Initial probe — small/empty
    initial = await db.probe_wal_state()
    initial_pages = initial["wal_pages"]

    # Force some writes
    for i in range(100):
        token = token_factory(contract_address=f"0xtest_{i}", quant_score=50.0)
        await db.upsert_candidate(token)

    after = await db.probe_wal_state()
    # wal_pages may or may not grow (depends on autocheckpoint firing);
    # db_size_bytes definitely grows
    assert after["db_size_bytes"] > initial["db_size_bytes"]


async def test_probe_wal_state_wal_file_size_matches_stat(db):
    """If a .db-wal sidecar exists, its size should equal wal_size_bytes."""
    state = await db.probe_wal_state()
    wal_path = Path(db._db_path + "-wal")
    if wal_path.exists():
        assert state["wal_size_bytes"] == wal_path.stat().st_size
    else:
        assert state["wal_size_bytes"] == 0
```

- [ ] **Step 1.2: Run → FAIL**

- [ ] **Step 1.3: Add settings fields in `scout/config.py`** (near other observability flags from cycle 3):

```python
    # BL-NEW-SQLITE-WAL-PROFILE: hourly SQLite WAL size probe.
    # Default True for 4-week measurement window; pre-registered TUNE
    # threshold at SQLITE_WAL_BLOAT_BYTES (50MB default — well above
    # SQLite's default 1000-page = ~4MB autocheckpoint trigger).
    SQLITE_WAL_PROFILE_ENABLED: bool = True
    SQLITE_WAL_BLOAT_BYTES: int = 50_000_000
```

- [ ] **Step 1.4: Add `Database.probe_wal_state()`** in `scout/db.py` (near `prune_score_history`):

```python
    async def probe_wal_state(self) -> dict:
        """Read SQLite WAL + DB size pragmas for observability.

        BL-NEW-SQLITE-WAL-PROFILE cycle 4. Called hourly from
        scout.main._run_hourly_maintenance to detect WAL bloat. Returns
        structured dict for log emission; does NOT trigger checkpoints
        (measurement only — operator decides TUNE-vs-ACCEPT after 4-week
        soak per BL-NEW-SQLITE-WAL-TUNING-DECISION).
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        import os

        async def _pragma(name: str) -> object:
            cur = await self._conn.execute(f"PRAGMA {name}")
            row = await cur.fetchone()
            return row[0] if row else None

        journal_mode = await _pragma("journal_mode")
        page_count = int(await _pragma("page_count") or 0)
        page_size = int(await _pragma("page_size") or 4096)
        freelist_count = int(await _pragma("freelist_count") or 0)
        wal_autocheckpoint = int(await _pragma("wal_autocheckpoint") or 0)

        # WAL file size from filesystem (sidecar `<db>-wal`)
        wal_path = self._db_path + "-wal"
        wal_size_bytes = (
            os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
        )
        wal_pages = wal_size_bytes // page_size if page_size else 0

        return {
            "wal_size_bytes": wal_size_bytes,
            "wal_pages": wal_pages,
            "db_size_bytes": page_count * page_size,
            "freelist_count": freelist_count,
            "journal_mode": str(journal_mode) if journal_mode else None,
            "wal_autocheckpoint": wal_autocheckpoint,
        }
```

- [ ] **Step 1.5: Run → PASS. Commit.**

---

### Task 2: Hourly wiring in `_run_hourly_maintenance`

**Files:** `scout/main.py`, `tests/test_hourly_maintenance.py`

- [ ] **Step 2.1: Failing integration test** — assert `_run_hourly_maintenance` calls `db.probe_wal_state()` when enabled and emits `sqlite_wal_probe` log:

```python
async def test_run_hourly_maintenance_emits_wal_probe_when_enabled(tmp_path):
    settings = _make_settings(tmp_path)  # SQLITE_WAL_PROFILE_ENABLED defaults True
    db = _make_db_mock()
    db.probe_wal_state = AsyncMock(return_value={
        "wal_size_bytes": 1024, "wal_pages": 0, "db_size_bytes": 4096,
        "freelist_count": 0, "journal_mode": "wal", "wal_autocheckpoint": 1000,
    })
    session = MagicMock()
    import structlog
    with structlog.testing.capture_logs() as logs:
        await _run_hourly_maintenance(db, session, settings, MagicMock())
    probe_events = [e for e in logs if e.get("event") == "sqlite_wal_probe"]
    assert len(probe_events) == 1
    assert probe_events[0]["wal_size_bytes"] == 1024


async def test_run_hourly_maintenance_emits_bloat_observed_above_threshold(tmp_path):
    settings = _make_settings(tmp_path, SQLITE_WAL_BLOAT_BYTES=1000)
    db = _make_db_mock()
    db.probe_wal_state = AsyncMock(return_value={
        "wal_size_bytes": 1_000_000, "wal_pages": 244, "db_size_bytes": 4096,
        "freelist_count": 0, "journal_mode": "wal", "wal_autocheckpoint": 1000,
    })
    session = MagicMock()
    import structlog
    with structlog.testing.capture_logs() as logs:
        await _run_hourly_maintenance(db, session, settings, MagicMock())
    bloat = [e for e in logs if e.get("event") == "sqlite_wal_bloat_observed"]
    assert len(bloat) == 1
    assert bloat[0]["wal_size_bytes"] == 1_000_000


async def test_run_hourly_maintenance_skips_wal_probe_when_disabled(tmp_path):
    settings = _make_settings(tmp_path, SQLITE_WAL_PROFILE_ENABLED=False)
    db = _make_db_mock()
    db.probe_wal_state = AsyncMock()
    session = MagicMock()
    await _run_hourly_maintenance(db, session, settings, MagicMock())
    db.probe_wal_state.assert_not_called()
```

(_make_settings extended with kwargs override; _make_db_mock extended with probe_wal_state default returning empty dict.)

- [ ] **Step 2.2: Run → FAIL**

- [ ] **Step 2.3: Wire in `_run_hourly_maintenance`** after the cycle 2 narrative-prune loop:

```python
    # BL-NEW-SQLITE-WAL-PROFILE cycle 4: hourly WAL state probe.
    if settings.SQLITE_WAL_PROFILE_ENABLED:
        try:
            state = await db.probe_wal_state()
            logger.info("sqlite_wal_probe", **state)
            if state.get("wal_size_bytes", 0) > settings.SQLITE_WAL_BLOAT_BYTES:
                logger.warning(
                    "sqlite_wal_bloat_observed",
                    threshold_bytes=settings.SQLITE_WAL_BLOAT_BYTES,
                    **state,
                )
        except Exception:
            logger.exception("sqlite_wal_probe_failed")
```

- [ ] **Step 2.4: Run → PASS. Commit.**

---

### Task 3: Operator helper scripts

**Files:** `scripts/wal_summary.sh`, `scripts/wal_archive.sh`

Mirror cycle 3's `tg_burst_summary.sh` + `tg_burst_archive.sh` shape. Archive: weekly cron, dated filename rotation, same-day re-run appends `.N` suffix, 8-week retention. Summary: time-of-day histogram, max WAL size, percent-of-probes-over-threshold.

- [ ] Implement + chmod +x + commit

---

### Task 4: Memory checkpoint + backlog

**Files:** `~/.claude/projects/.../memory/project_sqlite_wal_tuning_checkpoint_2026_06_14.md`, `backlog.md`

- [ ] Mirror cycle 3 memory file shape with: criteria, `wal_summary.sh 672` invocation, archive dir pointer
- [ ] Close BL-NEW-SQLITE-WAL-PROFILE; file BL-NEW-SQLITE-WAL-TUNING-DECISION with criteria

---

## Test plan summary

- 2 config tests
- 3 probe-method tests (required fields, post-write growth, wal-file-size match)
- 3 hourly-maintenance integration tests (emits, bloat-threshold, disabled-skip)
- Full regression must pass

Total: 8 new tests.

---

## Deployment verification (autonomous)

1. journalctl retention probe (informational; archive cron runs regardless)
2. Install `wal_archive.sh` cron unconditionally + mkdir/chmod
3. Restart + verify `sqlite_wal_probe` fires within first hour
4. `wal_summary.sh 1` smoke test
5. Memory checkpoint already filed pre-merge
6. Pre-registered review at 2026-06-14

---

## Out of scope

- Active WAL tuning (lower `wal_autocheckpoint`, explicit `PRAGMA wal_checkpoint(TRUNCATE)`) — measurement first; decision per `BL-NEW-SQLITE-WAL-TUNING-DECISION`
- DB-side VACUUM scheduling — separate concern; `freelist_count` metric surfaces the need
- §12a watchdog on WAL probe — covered by deferred §12a daemon item
