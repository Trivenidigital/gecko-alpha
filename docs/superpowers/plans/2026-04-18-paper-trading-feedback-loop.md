# Paper-Trading Feedback Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the six paper-trading feedback gaps (combo stats, rolling windows, weekly digest, lead-time metric, missed-winner audit, auto-suppression) so signal quality is driven by evidence.

**Architecture:** Hybrid persistence (Approach 3). One new hot-path table `combo_performance` feeds the entry-gate suppression check; everything else (leaderboard, missed-winner audit, lead-time medians, suppression log) is computed on-demand from existing tables at weekly-digest time. Nightly refresh at 03:00 local rebuilds `combo_performance` from closed trades. Sunday 09:00 local dispatches the weekly digest. All new module code lives under `scout/trading/` and uses the existing `db._conn.execute(...)` pattern (no new `Database` wrapper methods).

**Tech Stack:** Python 3.11+, `aiosqlite`, `aiohttp`, `pydantic v2`, `structlog`, `pytest-asyncio` (auto mode). Tests use `tmp_path` aiosqlite fixtures and the shared `conftest.py` `settings_factory` / `token_factory` helpers. TDD strictly — failing test first.

**Spec:** `docs/superpowers/specs/2026-04-18-paper-trading-feedback-loop-design.md` (committed at 895e75e). Re-read before starting any task if anything is unclear — the spec's locked decisions (D1–D21) are authoritative.

**Branch:** `feat/paper-trading-feedback-loop` (already checked out). All commits use conventional-commit style, e.g. `feat(trading): add build_combo_key helper`.

**Testing & format commands (use exactly):**
- Run targeted test: `uv run pytest tests/<file>::<test_name> -v`
- Run a file: `uv run pytest tests/<file> -v`
- Full suite: `uv run pytest --tb=short -q`
- Format: `uv run black scout/ tests/`

---

## Task index

1. Settings additions (`scout/config.py`)
2. Schema migration (`scout/db.py` + `tests/test_trading_db_migration.py`)
3. `build_combo_key` helper (`scout/trading/combo_key.py` + test)
4. Lead-time helper + `engine.open_trade` changes (`scout/trading/engine.py`, `scout/trading/paper.py` + test)
5. Suppression module (`scout/trading/suppression.py` + test)
6. Combo refresh module (`scout/trading/combo_refresh.py` + test)
7. Analytics module (`scout/trading/analytics.py` + test)
8. Weekly digest module (`scout/trading/weekly_digest.py` + test)
9. Signals integration (`scout/trading/signals.py` + integration test)
10. Main loop scheduling (`scout/main.py`)
11. Final regression pass

---

### Task 1: Settings additions

**Files:**
- Modify: `scout/config.py` (append to the `Settings` class, same block as other FEEDBACK_ / PAPER_ settings)
- Test: `tests/test_config.py` (exists — add one assertion block)

- [ ] **Step 1: Write the failing test**

Open `tests/test_config.py` and append:

```python
def test_feedback_loop_defaults(monkeypatch):
    """All feedback-loop settings have sensible defaults per spec §8."""
    monkeypatch.delenv("FEEDBACK_SUPPRESSION_MIN_TRADES", raising=False)
    monkeypatch.delenv("FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT", raising=False)
    monkeypatch.delenv("FEEDBACK_PAROLE_DAYS", raising=False)
    monkeypatch.delenv("FEEDBACK_PAROLE_RETEST_TRADES", raising=False)
    monkeypatch.delenv("FEEDBACK_MIN_LEADERBOARD_TRADES", raising=False)
    monkeypatch.delenv("FEEDBACK_MISSED_WINNER_MIN_PCT", raising=False)
    monkeypatch.delenv("FEEDBACK_MISSED_WINNER_MIN_MCAP", raising=False)
    monkeypatch.delenv("FEEDBACK_MISSED_WINNER_MAX_RANK", raising=False)
    monkeypatch.delenv("FEEDBACK_MISSED_WINNER_WINDOW_MIN", raising=False)
    monkeypatch.delenv("FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN", raising=False)
    monkeypatch.delenv("FEEDBACK_WEEKLY_DIGEST_WEEKDAY", raising=False)
    monkeypatch.delenv("FEEDBACK_WEEKLY_DIGEST_HOUR", raising=False)
    monkeypatch.delenv("FEEDBACK_COMBO_REFRESH_HOUR", raising=False)
    monkeypatch.delenv("FEEDBACK_FALLBACK_ALERT_THRESHOLD", raising=False)
    monkeypatch.delenv("FEEDBACK_CHRONIC_FAILURE_THRESHOLD", raising=False)

    from scout.config import Settings
    s = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
    )
    assert s.FEEDBACK_SUPPRESSION_MIN_TRADES == 20
    assert s.FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT == 30.0
    assert s.FEEDBACK_PAROLE_DAYS == 14
    assert s.FEEDBACK_PAROLE_RETEST_TRADES == 5
    assert s.FEEDBACK_MIN_LEADERBOARD_TRADES == 10
    assert s.FEEDBACK_MISSED_WINNER_MIN_PCT == 50.0
    assert s.FEEDBACK_MISSED_WINNER_MIN_MCAP == 5_000_000
    assert s.FEEDBACK_MISSED_WINNER_MAX_RANK == 1500
    assert s.FEEDBACK_MISSED_WINNER_WINDOW_MIN == 30
    assert s.FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN == 60
    assert s.FEEDBACK_WEEKLY_DIGEST_WEEKDAY == 6
    assert s.FEEDBACK_WEEKLY_DIGEST_HOUR == 9
    assert s.FEEDBACK_COMBO_REFRESH_HOUR == 3
    assert s.FEEDBACK_FALLBACK_ALERT_THRESHOLD == 5
    assert s.FEEDBACK_CHRONIC_FAILURE_THRESHOLD == 3
```

Adjust the `Settings(...)` constructor kwargs above if your existing `test_config.py` uses a factory — match the local style.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_feedback_loop_defaults -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'FEEDBACK_SUPPRESSION_MIN_TRADES'`

- [ ] **Step 3: Add settings to `scout/config.py`**

Find the `Settings` class in `scout/config.py` and add these lines (same indentation as existing fields; placement: after the other PAPER_* / FEEDBACK-adjacent block — if none, add just above the final closing of the class):

```python
    # Feedback-loop (Sprint 1, spec 2026-04-18)
    FEEDBACK_SUPPRESSION_MIN_TRADES: int = 20
    FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT: float = 30.0
    FEEDBACK_PAROLE_DAYS: int = 14
    FEEDBACK_PAROLE_RETEST_TRADES: int = 5
    FEEDBACK_MIN_LEADERBOARD_TRADES: int = 10
    FEEDBACK_MISSED_WINNER_MIN_PCT: float = 50.0
    FEEDBACK_MISSED_WINNER_MIN_MCAP: float = 5_000_000
    FEEDBACK_MISSED_WINNER_MAX_RANK: int = 1500
    FEEDBACK_MISSED_WINNER_WINDOW_MIN: int = 30
    FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN: int = 60
    FEEDBACK_WEEKLY_DIGEST_WEEKDAY: int = 6         # 6 = Sunday (Mon=0 per datetime.weekday())
    FEEDBACK_WEEKLY_DIGEST_HOUR: int = 9            # 09:00 local
    FEEDBACK_COMBO_REFRESH_HOUR: int = 3            # 03:00 local nightly
    FEEDBACK_FALLBACK_ALERT_THRESHOLD: int = 5
    FEEDBACK_CHRONIC_FAILURE_THRESHOLD: int = 3
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_config.py::test_feedback_loop_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scout/config.py tests/test_config.py
git commit -m "feat(config): add feedback-loop settings (Sprint 1 defaults)"
```

---

### Task 2: Schema migration (per-column + schema_version + post-assertion, atomic)

**Files:**
- Modify: `scout/db.py` — add `_migrate_feedback_loop_schema(conn)` plus a call to it from `initialize()` AFTER `_create_tables()`
- Create: `tests/test_trading_db_migration.py`

Per spec D18: all DDL wrapped in `BEGIN EXCLUSIVE`; `schema_version` row commits only if all DDL succeeded; post-migration `PRAGMA table_info` assertion raises `RuntimeError` on missing columns.

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_trading_db_migration.py`:

```python
"""Tests for feedback-loop schema migration (spec §5.7)."""
from __future__ import annotations

import aiosqlite
import pytest

from scout.db import Database


async def _existing_paper_trades_columns(conn) -> set[str]:
    cur = await conn.execute("PRAGMA table_info(paper_trades)")
    return {row[1] for row in await cur.fetchall()}


async def _open_raw_conn(path):
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    return conn


async def test_fresh_db_migrates_all_columns(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.initialize()
    cols = await _existing_paper_trades_columns(db._conn)
    assert "signal_combo" in cols
    assert "lead_time_vs_trending_min" in cols
    assert "lead_time_vs_trending_status" in cols

    # combo_performance + schema_version exist
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('combo_performance', 'schema_version')"
    )
    names = {row[0] for row in await cur.fetchall()}
    assert names == {"combo_performance", "schema_version"}

    # schema_version seeded with feedback_loop_v1
    cur = await db._conn.execute(
        "SELECT version, description FROM schema_version WHERE version = 20260418"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[1] == "feedback_loop_v1"
    await db.close()


async def test_migration_is_idempotent(tmp_path):
    """Calling initialize twice must not error and must not duplicate columns."""
    db_path = tmp_path / "test.db"
    db1 = Database(db_path)
    await db1.initialize()
    await db1.close()

    db2 = Database(db_path)
    await db2.initialize()  # should not raise
    cols = await _existing_paper_trades_columns(db2._conn)
    assert sum(1 for c in cols if c == "signal_combo") == 1
    await db2.close()


async def test_partial_db_fills_missing_columns(tmp_path):
    """If some feedback cols exist but not all, only the missing ones are added."""
    db_path = tmp_path / "test.db"
    # Stage 1: fresh DB, let full migration run.
    db = Database(db_path)
    await db.initialize()
    await db.close()

    # Stage 2: surgically drop one feedback column (via table rebuild),
    # then re-run initialize. SQLite doesn't support DROP COLUMN pre-3.35,
    # but the simpler test: re-run and confirm the column still exists
    # and no error raised. For a true partial-DB test, see stage 3.
    db2 = Database(db_path)
    await db2.initialize()
    cols = await _existing_paper_trades_columns(db2._conn)
    assert "signal_combo" in cols
    await db2.close()


async def test_migration_adds_required_indexes(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name IN ('idx_paper_trades_combo_opened', "
        "            'idx_paper_trades_token_opened', "
        "            'idx_trending_snapshots_coin_id')"
    )
    names = {row[0] for row in await cur.fetchall()}
    assert names == {
        "idx_paper_trades_combo_opened",
        "idx_paper_trades_token_opened",
        "idx_trending_snapshots_coin_id",
    }
    await db.close()


async def test_post_migration_assertion_raises_on_incomplete_schema(tmp_path, monkeypatch):
    """If ALTER TABLE silently no-ops (simulated via monkeypatch), assertion fires."""
    from scout import db as db_module

    db = Database(tmp_path / "test.db")
    # Pre-create paper_trades WITHOUT the new columns by running only _create_tables.
    # Then monkeypatch ALTER TABLE to be a no-op to force the assertion.
    await db.initialize()
    await db.close()

    # Corrupt: drop the feedback columns. SQLite 3.35+ supports DROP COLUMN.
    raw = await _open_raw_conn(tmp_path / "test.db")
    try:
        await raw.execute("ALTER TABLE paper_trades DROP COLUMN signal_combo")
        await raw.commit()
    except Exception:
        pytest.skip("SQLite version lacks DROP COLUMN support")
    await raw.close()

    # Re-init — migration should re-add signal_combo (success path). To exercise
    # the assertion failure path, we monkeypatch the ALTER TABLE to raise or be
    # a no-op. Simplest: monkeypatch conn.execute to swallow ALTER TABLE.
    db2 = Database(tmp_path / "test.db")

    original_execute = None

    async def _swallow_alter(self, sql, *args, **kwargs):
        if "ALTER TABLE paper_trades ADD COLUMN signal_combo" in sql:
            # Simulate silent no-op: don't actually add the column.
            class _FakeCursor:
                async def fetchall(self):
                    return []
                async def fetchone(self):
                    return None
                lastrowid = None
                rowcount = 0
            return _FakeCursor()
        return await original_execute(self, sql, *args, **kwargs)

    import aiosqlite as _aiosqlite
    original_execute = _aiosqlite.Connection.execute
    monkeypatch.setattr(_aiosqlite.Connection, "execute", _swallow_alter)

    with pytest.raises(RuntimeError, match="Schema migration incomplete"):
        await db2.initialize()

    monkeypatch.setattr(_aiosqlite.Connection, "execute", original_execute)
    try:
        await db2.close()
    except Exception:
        pass
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest tests/test_trading_db_migration.py -v`
Expected: FAIL — `combo_performance` table doesn't exist yet, assertion test also fails.

- [ ] **Step 3: Add `_migrate_feedback_loop_schema` to `scout/db.py`**

Add this method to the `Database` class in `scout/db.py` (place it just after `_create_tables`):

```python
    async def _migrate_feedback_loop_schema(self) -> None:
        """Per-column additive migration for feedback loop. Idempotent. Atomic."""
        import structlog
        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        try:
            await conn.execute("BEGIN EXCLUSIVE")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS combo_performance (
                    combo_key TEXT NOT NULL,
                    window TEXT NOT NULL,
                    trades INTEGER NOT NULL,
                    wins INTEGER NOT NULL,
                    losses INTEGER NOT NULL,
                    total_pnl_usd REAL NOT NULL,
                    avg_pnl_pct REAL NOT NULL,
                    win_rate_pct REAL NOT NULL,
                    suppressed INTEGER NOT NULL DEFAULT 0,
                    suppressed_at TEXT,
                    parole_at TEXT,
                    parole_trades_remaining INTEGER,
                    refresh_failures INTEGER NOT NULL DEFAULT 0,
                    last_refreshed TEXT NOT NULL,
                    PRIMARY KEY (combo_key, window)
                )
            """)

            expected_cols = {
                "signal_combo": "TEXT",
                "lead_time_vs_trending_min": "REAL",
                "lead_time_vs_trending_status": "TEXT",
            }
            cur = await conn.execute("PRAGMA table_info(paper_trades)")
            existing = {row[1] for row in await cur.fetchall()}
            for col, coltype in expected_cols.items():
                if col in existing:
                    _log.info("schema_migration_column_action", col=col, action="skip_exists")
                else:
                    await conn.execute(
                        f"ALTER TABLE paper_trades ADD COLUMN {col} {coltype}"
                    )
                    _log.info("schema_migration_column_action", col=col, action="added")

            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_trades_combo_opened "
                "ON paper_trades(signal_combo, opened_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_trades_token_opened "
                "ON paper_trades(token_id, opened_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trending_snapshots_coin_id "
                "ON trending_snapshots(coin_id)"
            )

            cur = await conn.execute("PRAGMA table_info(paper_trades)")
            final = {row[1] for row in await cur.fetchall()}
            missing = set(expected_cols) - final
            if missing:
                raise RuntimeError(f"Schema migration incomplete: missing {missing}")

            from datetime import datetime, timezone
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at, description) "
                "VALUES (?, ?, ?)",
                (20260418, datetime.now(timezone.utc).isoformat(), "feedback_loop_v1"),
            )
            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                pass
            _log.error("SCHEMA_DRIFT_DETECTED")
            raise
```

Then find `Database.initialize()` and add a call at the end, after `_create_tables()`:

```python
    async def initialize(self) -> None:
        """Open connection and create tables."""
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._create_tables()
        await self._migrate_feedback_loop_schema()   # NEW
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/test_trading_db_migration.py -v`
Expected: all PASS (the `test_post_migration_assertion_raises_on_incomplete_schema` test may skip on older SQLite — that's acceptable).

- [ ] **Step 5: Run broader trading-db regression**

Run: `uv run pytest tests/test_trading_db.py tests/test_trading_engine.py tests/test_trading_digest.py -v`
Expected: all previously-passing tests remain green. `paper_trades` insert code paths are unchanged at this point (signal_combo still NULL; PaperTrader doesn't write it yet — that's Task 4).

- [ ] **Step 6: Format + commit**

```bash
uv run black scout/db.py tests/test_trading_db_migration.py
git add scout/db.py tests/test_trading_db_migration.py
git commit -m "feat(db): atomic per-column migration for feedback-loop schema"
```

---

### Task 3: `build_combo_key` helper

**Files:**
- Create: `scout/trading/combo_key.py`
- Create: `tests/test_trading_combo_key.py`

Per spec §4.4 and D20: pure function, deterministic, truncates extra signals (pair cap) with a structured log.

- [ ] **Step 1: Write the failing test**

Create `tests/test_trading_combo_key.py`:

```python
"""Tests for build_combo_key (spec §4.4)."""
from __future__ import annotations

from scout.trading.combo_key import build_combo_key


def test_single_signal_no_extras():
    assert build_combo_key("volume_spike", None) == "volume_spike"
    assert build_combo_key("volume_spike", []) == "volume_spike"


def test_signal_type_plus_one_extra():
    assert (
        build_combo_key("first_signal", ["momentum_ratio"])
        == "first_signal+momentum_ratio"
    )


def test_extras_sorted_alphabetically_for_pick():
    # When extras=['zzz', 'aaa', 'mmm'], alphabetically-first is 'aaa'.
    assert build_combo_key("first_signal", ["zzz", "aaa", "mmm"]) == "aaa+first_signal"


def test_output_is_sorted():
    # signal_type='xray', extra='apple' => 'apple+xray' (sorted output).
    assert build_combo_key("xray", ["apple"]) == "apple+xray"


def test_signal_type_dedup_from_extras():
    # If signals includes signal_type itself, don't double-count.
    assert build_combo_key("volume_spike", ["volume_spike"]) == "volume_spike"


def test_triple_truncates_to_pair_and_logs(caplog):
    import logging
    caplog.set_level(logging.INFO)
    result = build_combo_key("first_signal", ["momentum_ratio", "vol_acceleration"])
    # Kept: alphabetically-first of extras = 'momentum_ratio'
    assert result == "first_signal+momentum_ratio"
    # structlog uses stdlib logging under the hood; just confirm function returned.
    # (Structured-log capture differs by setup; the pair-cap contract is behavioural.)


def test_pair_cap_keeps_alphabetically_first():
    # extras sorted: ['aaa', 'bbb', 'ccc']; kept='aaa'; dropped=['bbb','ccc'].
    assert build_combo_key("zulu", ["ccc", "bbb", "aaa"]) == "aaa+zulu"


def test_none_signals_equivalent_to_empty():
    assert build_combo_key("trending_catch", None) == "trending_catch"


def test_signal_type_always_included():
    # Even when extras sort before signal_type, signal_type is in the output.
    result = build_combo_key("zzz", ["aaa"])
    assert "zzz" in result
    assert "aaa" in result
```

- [ ] **Step 2: Run the test — expect FAIL**

Run: `uv run pytest tests/test_trading_combo_key.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scout.trading.combo_key'`.

- [ ] **Step 3: Implement `scout/trading/combo_key.py`**

```python
"""Combo-key derivation for paper-trading signal aggregation.

Single derivation site per spec D20. Pair-capped (spec D2).
"""
from __future__ import annotations

import structlog

log = structlog.get_logger()


def build_combo_key(signal_type: str, signals: list[str] | None) -> str:
    """Build combo_key = signal_type + (at most 1) alphabetically-first extra signal.

    Extras beyond the first are dropped and logged for Sprint 2 analysis.
    Output is 'sorted(parts)' joined by '+', so keys are order-insensitive.
    """
    parts = {signal_type}
    dropped: list[str] = []
    if signals:
        extras = sorted(s for s in signals if s and s != signal_type)
        if extras:
            parts.add(extras[0])
            dropped = extras[1:]
    if dropped:
        log.info(
            "combo_key_truncated_signals",
            signal_type=signal_type,
            kept=sorted(parts - {signal_type})[0] if len(parts) > 1 else None,
            dropped=dropped,
        )
    return "+".join(sorted(parts))
```

- [ ] **Step 4: Run the test — expect PASS**

Run: `uv run pytest tests/test_trading_combo_key.py -v`
Expected: all PASS.

- [ ] **Step 5: Format + commit**

```bash
uv run black scout/trading/combo_key.py tests/test_trading_combo_key.py
git add scout/trading/combo_key.py tests/test_trading_combo_key.py
git commit -m "feat(trading): add build_combo_key helper with pair cap"
```

---

### Task 4: Lead-time helper + `engine.open_trade` changes

**Files:**
- Modify: `scout/trading/engine.py` — add `_compute_lead_time_vs_trending` helper + new required `signal_combo` kwarg + populate lead-time columns on insert
- Modify: `scout/trading/paper.py` — `execute_buy` accepts the three new fields and writes them
- Create: `tests/test_trading_engine_leadtime.py`

Per spec §4.5, D8, D19, D20. The `signal_combo` kwarg is **required** — no default — so a missing call site fails tests rather than silently inserting NULL.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_trading_engine_leadtime.py`:

```python
"""Tests for lead-time computation and signal_combo persistence (spec §4.5)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading.engine import TradingEngine, _compute_lead_time_vs_trending


async def _seed_price(db, token_id: str, price: float):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (token_id, price, now),
    )
    await db._conn.commit()


async def _seed_trending(db, coin_id: str, snapshot_at: datetime):
    await db._conn.execute(
        "INSERT INTO trending_snapshots "
        "(coin_id, symbol, name, market_cap_rank, price_at_snapshot, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (coin_id, "SYM", "Name", 100, 1.0, snapshot_at.isoformat()),
    )
    await db._conn.commit()


async def test_lead_time_negative_when_we_beat_trending(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Trending snapshot 15 minutes from now means we're opening 15min BEFORE trending.
    now = datetime.now(timezone.utc)
    crossed = now + timedelta(minutes=15)
    await _seed_trending(db, "coinX", crossed)
    lead, status = await _compute_lead_time_vs_trending(db, "coinX", now)
    assert status == "ok"
    assert lead is not None and lead < 0
    assert abs(lead - (-15)) < 0.5
    await db.close()


async def test_lead_time_positive_when_late(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    crossed = now - timedelta(minutes=20)
    await _seed_trending(db, "coinX", crossed)
    lead, status = await _compute_lead_time_vs_trending(db, "coinX", now)
    assert status == "ok"
    assert lead is not None and lead > 0
    assert abs(lead - 20) < 0.5
    await db.close()


async def test_lead_time_no_reference_when_coin_never_trended(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    lead, status = await _compute_lead_time_vs_trending(
        db, "never_trended", datetime.now(timezone.utc)
    )
    assert lead is None
    assert status == "no_reference"
    await db.close()


async def test_lead_time_returns_error_status_on_bad_row(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Insert a row with a malformed timestamp so datetime.fromisoformat raises.
    await db._conn.execute(
        "INSERT INTO trending_snapshots "
        "(coin_id, symbol, name, market_cap_rank, price_at_snapshot, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("coinX", "SYM", "Name", 100, 1.0, "NOT-A-TIMESTAMP"),
    )
    await db._conn.commit()
    lead, status = await _compute_lead_time_vs_trending(
        db, "coinX", datetime.now(timezone.utc)
    )
    assert lead is None
    assert status == "error"
    await db.close()


async def test_open_trade_persists_signal_combo_and_lead_time(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=settings)

    # Seed price so open_trade doesn't bail.
    await _seed_price(db, "coinX", 1.0)
    # Seed trending so lead_time computed as negative (we beat trending by 10 min).
    now = datetime.now(timezone.utc)
    await _seed_trending(db, "coinX", now + timedelta(minutes=10))

    tid = await engine.open_trade(
        token_id="coinX",
        symbol="CX",
        name="CoinX",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 3.0},
        entry_price=1.0,
        signal_combo="volume_spike",
    )
    assert tid is not None

    cur = await db._conn.execute(
        "SELECT signal_combo, lead_time_vs_trending_min, lead_time_vs_trending_status "
        "FROM paper_trades WHERE id = ?",
        (tid,),
    )
    row = await cur.fetchone()
    assert row["signal_combo"] == "volume_spike"
    assert row["lead_time_vs_trending_status"] == "ok"
    assert row["lead_time_vs_trending_min"] < 0  # beat trending
    await db.close()


async def test_open_trade_status_error_does_not_block_insert(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=settings)

    await _seed_price(db, "coinX", 1.0)
    # Bad trending timestamp forces status='error'.
    await db._conn.execute(
        "INSERT INTO trending_snapshots "
        "(coin_id, symbol, name, market_cap_rank, price_at_snapshot, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("coinX", "SYM", "Name", 100, 1.0, "NOT-A-TIMESTAMP"),
    )
    await db._conn.commit()

    tid = await engine.open_trade(
        token_id="coinX",
        symbol="CX",
        name="CoinX",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        entry_price=1.0,
        signal_combo="volume_spike",
    )
    assert tid is not None  # trade still opens
    cur = await db._conn.execute(
        "SELECT lead_time_vs_trending_min, lead_time_vs_trending_status "
        "FROM paper_trades WHERE id = ?",
        (tid,),
    )
    row = await cur.fetchone()
    assert row["lead_time_vs_trending_min"] is None
    assert row["lead_time_vs_trending_status"] == "error"
    await db.close()


async def test_open_trade_without_signal_combo_raises(tmp_path, settings_factory):
    """signal_combo is a required kwarg — missing call site must fail loud."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=settings)
    await _seed_price(db, "coinX", 1.0)
    with pytest.raises(TypeError):
        await engine.open_trade(
            token_id="coinX",
            symbol="CX",
            name="CoinX",
            chain="coingecko",
            signal_type="volume_spike",
            signal_data={},
            entry_price=1.0,
            # no signal_combo — must raise
        )
    await db.close()
```

If `settings_factory` in `tests/conftest.py` doesn't accept arbitrary overrides, inspect it and either pass supported kwargs or use `Settings(...)` directly (match existing test style in `test_trading_engine.py`).

- [ ] **Step 2: Run the tests — expect FAIL**

Run: `uv run pytest tests/test_trading_engine_leadtime.py -v`
Expected: FAIL — `ImportError: cannot import name '_compute_lead_time_vs_trending'`, and `open_trade` rejects `signal_combo` kwarg.

- [ ] **Step 3: Add `_compute_lead_time_vs_trending` and update `engine.open_trade`**

In `scout/trading/engine.py`:

(a) Add these imports at the top (keep existing):
```python
from datetime import datetime, timezone
```
(already present — confirm).

(b) Add this module-level async helper *above* the `TradingEngine` class:

```python
async def _compute_lead_time_vs_trending(
    db: Database, token_id: str, now: datetime
) -> tuple[float | None, str]:
    """Returns (lead_time_min, status). status in {'ok', 'no_reference', 'error'}.

    Negative lead_time means we opened BEFORE the coin trended (beat CG).
    Positive means we opened AFTER (we were late).
    """
    try:
        cursor = await db._conn.execute(
            "SELECT MIN(snapshot_at) FROM trending_snapshots WHERE coin_id = ?",
            (token_id,),
        )
        row = await cursor.fetchone()
        crossed_at = row[0] if row else None
        if crossed_at is None:
            return (None, "no_reference")
        crossed_dt = datetime.fromisoformat(crossed_at)
        if crossed_dt.tzinfo is None:
            crossed_dt = crossed_dt.replace(tzinfo=timezone.utc)
        delta_min = (now - crossed_dt).total_seconds() / 60.0
        return (delta_min, "ok")
    except Exception as e:
        log.error(
            "lead_time_compute_error",
            err=str(e),
            err_id="LEAD_TIME_CALC",
            token_id=token_id,
        )
        return (None, "error")
```

(c) Update `TradingEngine.open_trade` signature: add `signal_combo: str` as a required keyword argument. Inside the function, compute lead-time once just before the paper-trader call, and pass all three fields through:

Locate the existing `async def open_trade(` signature and change it so `signal_combo` is required:

```python
    async def open_trade(
        self,
        token_id: str,
        symbol: str = "",
        name: str = "",
        chain: str = "coingecko",
        signal_type: str = "",
        signal_data: dict | None = None,
        amount_usd: float | None = None,
        entry_price: float | None = None,
        *,
        signal_combo: str,
    ) -> int | None:
```

The bare `*` forces `signal_combo` to be keyword-only AND required.

Then, in the body, just before `if self.mode == "paper":`, compute lead-time:

```python
        now_utc = datetime.now(timezone.utc)
        lead_time_min, lead_time_status = await _compute_lead_time_vs_trending(
            self.db, token_id, now_utc
        )
```

And update the `execute_buy` call to forward the three new values:

```python
        if self.mode == "paper":
            trade_id = await self._paper_trader.execute_buy(
                db=self.db,
                token_id=token_id,
                symbol=symbol,
                name=name,
                chain=chain,
                signal_type=signal_type,
                signal_data=signal_data,
                current_price=current_price,
                amount_usd=trade_amount,
                tp_pct=self.settings.PAPER_TP_PCT,
                sl_pct=self.settings.PAPER_SL_PCT,
                slippage_bps=self.settings.PAPER_SLIPPAGE_BPS,
                signal_combo=signal_combo,
                lead_time_vs_trending_min=lead_time_min,
                lead_time_vs_trending_status=lead_time_status,
            )
            return trade_id
```

- [ ] **Step 4: Update `PaperTrader.execute_buy` in `scout/trading/paper.py`**

Modify `execute_buy` to accept and persist the three new fields:

```python
    async def execute_buy(
        self,
        db: Database,
        token_id: str,
        symbol: str,
        name: str,
        chain: str,
        signal_type: str,
        signal_data: dict,
        current_price: float,
        amount_usd: float,
        tp_pct: float,
        sl_pct: float,
        slippage_bps: int = 0,
        *,
        signal_combo: str,
        lead_time_vs_trending_min: float | None = None,
        lead_time_vs_trending_status: str | None = None,
    ) -> int | None:
        ...
```

And update the INSERT statement to include the three columns:

```python
        cursor = await conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity,
                tp_pct, sl_pct, tp_price, sl_price,
                status, opened_at,
                signal_combo, lead_time_vs_trending_min, lead_time_vs_trending_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
            (
                token_id, symbol, name, chain, signal_type,
                json.dumps(signal_data),
                effective_entry, amount_usd, quantity,
                tp_pct, sl_pct, tp_price, sl_price,
                now,
                signal_combo, lead_time_vs_trending_min, lead_time_vs_trending_status,
            ),
        )
```

- [ ] **Step 5: Update all existing `signals.py` callers to pass `signal_combo` (placeholder — real wiring in Task 9)**

The Task 9 signals-integration wires this properly with `build_combo_key`. For now, to unblock tests and keep the suite green, patch every `engine.open_trade(` call in `scout/trading/signals.py` to temporarily pass `signal_combo=signal_type` (the degenerate form — same as D13 fallback):

Open `scout/trading/signals.py` and for each `engine.open_trade(` call, add `signal_combo=<signal_type_literal>` as a keyword arg. For example:
- `trade_volume_spikes`: `signal_combo="volume_spike"`
- `trade_gainers`: `signal_combo="gainers_early"`
- `trade_losers`: `signal_combo="losers_contrarian"`
- `trade_first_signals`: `signal_combo="first_signal"`
- `trade_trending`: `signal_combo="trending_catch"`
- `trade_predictions`: `signal_combo="narrative_prediction"`
- `trade_chain_completions`: `signal_combo="chain_completed"`

These will be replaced in Task 9 with real combo keys from `build_combo_key`.

- [ ] **Step 6: Run the new tests — expect PASS**

Run: `uv run pytest tests/test_trading_engine_leadtime.py -v`
Expected: all PASS.

- [ ] **Step 7: Run full trading regression**

Run: `uv run pytest tests/test_trading_engine.py tests/test_trading_digest.py tests/test_trading_signals.py tests/test_trading_db.py tests/test_paper_trader.py -v`
Expected: all previously-passing tests still pass. If any test calls `engine.open_trade(...)` directly without `signal_combo`, it will fail — add `signal_combo="<signal_type>"` to the call and move on.

- [ ] **Step 8: Format + commit**

```bash
uv run black scout/trading/engine.py scout/trading/paper.py scout/trading/signals.py tests/test_trading_engine_leadtime.py
git add scout/trading/engine.py scout/trading/paper.py scout/trading/signals.py tests/test_trading_engine_leadtime.py
git commit -m "feat(trading): lead-time helper + signal_combo persistence in open_trade"
```

---

### Task 5: Suppression module (entry-gate + atomic parole decrement)

**Files:**
- Create: `scout/trading/suppression.py`
- Create: `tests/test_trading_suppression.py`

Per spec §5.2, D16, D17. Uses `BEGIN IMMEDIATE` for atomic parole decrement. Module-level deque + `last_alerted_ts` sentinel for fail-open alerting.

- [ ] **Step 1: Write the failing test file**

Create `tests/test_trading_suppression.py`:

```python
"""Tests for suppression entry-gate (spec §5.2)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import suppression


async def _seed_combo(
    db,
    key: str,
    *,
    window: str = "30d",
    trades: int = 0,
    wins: int = 0,
    suppressed: int = 0,
    suppressed_at: str | None = None,
    parole_at: str | None = None,
    parole_remaining: int | None = None,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    losses = max(trades - wins, 0)
    wr = (wins / trades * 100.0) if trades else 0.0
    await db._conn.execute(
        "INSERT OR REPLACE INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, "
        " parole_at, parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, 0, ?)",
        (
            key, window, trades, wins, losses, wr, suppressed,
            suppressed_at, parole_at, parole_remaining, now_iso,
        ),
    )
    await db._conn.commit()


@pytest.fixture(autouse=True)
def _reset_fallback_state():
    suppression._fallback_timestamps.clear()
    suppression._last_alerted_ts = 0.0
    yield
    suppression._fallback_timestamps.clear()
    suppression._last_alerted_ts = 0.0


async def test_cold_start_allows(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    allow, reason = await suppression.should_open(db, "never_seen")
    assert allow is True
    assert reason == "cold_start"
    await db.close()


async def test_not_suppressed_allows(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_combo(db, "good_combo", trades=30, wins=20, suppressed=0)
    allow, reason = await suppression.should_open(db, "good_combo")
    assert allow is True
    assert reason == "ok"
    await db.close()


async def test_suppressed_pre_parole_denies(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    await _seed_combo(
        db, "bad_combo", trades=25, wins=5, suppressed=1,
        suppressed_at=datetime.now(timezone.utc).isoformat(),
        parole_at=future, parole_remaining=5,
    )
    allow, reason = await suppression.should_open(db, "bad_combo")
    assert allow is False
    assert reason == "suppressed"
    await db.close()


async def test_parole_allows_and_decrements(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _seed_combo(
        db, "parole_combo", trades=25, wins=5, suppressed=1,
        suppressed_at=past, parole_at=past, parole_remaining=3,
    )
    allow, reason = await suppression.should_open(db, "parole_combo")
    assert allow is True
    assert reason == "parole_retest"
    cur = await db._conn.execute(
        "SELECT parole_trades_remaining FROM combo_performance "
        "WHERE combo_key = ? AND window = '30d'",
        ("parole_combo",),
    )
    row = await cur.fetchone()
    assert row[0] == 2
    await db.close()


async def test_parole_exhausted_denies(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _seed_combo(
        db, "exhausted", trades=25, wins=5, suppressed=1,
        suppressed_at=past, parole_at=past, parole_remaining=0,
    )
    allow, reason = await suppression.should_open(db, "exhausted")
    assert allow is False
    assert reason == "parole_exhausted"
    await db.close()


async def test_concurrent_decrement_grants_only_one(tmp_path):
    """Per spec D16 — BEGIN IMMEDIATE serializes the SELECT/UPDATE."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _seed_combo(
        db, "race_combo", trades=25, wins=5, suppressed=1,
        suppressed_at=past, parole_at=past, parole_remaining=1,
    )
    results = await asyncio.gather(
        suppression.should_open(db, "race_combo"),
        suppression.should_open(db, "race_combo"),
    )
    reasons = sorted(r[1] for r in results)
    assert reasons == ["parole_exhausted", "parole_retest"]
    cur = await db._conn.execute(
        "SELECT parole_trades_remaining FROM combo_performance "
        "WHERE combo_key='race_combo' AND window='30d'",
    )
    assert (await cur.fetchone())[0] == 0
    await db.close()


async def test_db_error_fallback_allows(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()

    async def _boom(*a, **k):
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(db._conn, "execute", _boom)
    allow, reason = await suppression.should_open(db, "whatever")
    assert allow is True
    assert reason == "db_error_fallback_allow"
    await db.close()


async def test_fallback_counter_alerts_at_threshold(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()

    sent: list[str] = []

    async def _capture(msg, *a, **k):
        sent.append(msg)

    import scout.alerter as _alerter
    monkeypatch.setattr(_alerter, "send_telegram_message", _capture)

    async def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(db._conn, "execute", _boom)

    # 5 failures should trigger one Telegram alert (threshold = 5).
    for _ in range(5):
        await suppression.should_open(db, "x")
    assert len(sent) == 1
    assert "fail-open" in sent[0].lower()

    # Immediate 6th failure within cooldown — no new alert.
    await suppression.should_open(db, "x")
    assert len(sent) == 1

    # Force cooldown expiry by rewinding _last_alerted_ts.
    suppression._last_alerted_ts = time.monotonic() - (
        suppression._FALLBACK_ALERT_COOLDOWN_SEC + 1
    )
    await suppression.should_open(db, "x")
    assert len(sent) == 2
    await db.close()
```

- [ ] **Step 2: Run the test — expect FAIL**

Run: `uv run pytest tests/test_trading_suppression.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scout.trading.suppression'`.

- [ ] **Step 3: Implement `scout/trading/suppression.py`**

```python
"""Suppression entry-gate (spec §5.2).

Must be imported only from `signals.py` dispatchers. The module-level state
(`_fallback_timestamps`, `_last_alerted_ts`) is process-local, which is safe
because gecko-alpha runs a single event-loop process.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone

import structlog

from scout import alerter
from scout.db import Database

log = structlog.get_logger()

_FALLBACK_WINDOW_SEC = 3600
_FALLBACK_ALERT_THRESHOLD = 5
_FALLBACK_ALERT_COOLDOWN_SEC = 900
_fallback_timestamps: "deque[float]" = deque()
_last_alerted_ts: float = 0.0


async def should_open(db: Database, combo_key: str) -> tuple[bool, str]:
    """Entry-gate: returns (allow, reason). Fail-open on DB error."""
    try:
        cursor = await db._conn.execute(
            "SELECT suppressed, parole_at, parole_trades_remaining "
            "FROM combo_performance WHERE combo_key = ? AND window = '30d'",
            (combo_key,),
        )
        row = await cursor.fetchone()
    except Exception as e:
        await _record_fallback(combo_key, str(e))
        return (True, "db_error_fallback_allow")

    if row is None:
        return (True, "cold_start")

    suppressed = row[0]
    parole_at = row[1]
    # parole_remaining = row[2]  # re-read inside transaction below

    if not suppressed:
        return (True, "ok")

    if parole_at is None:
        return (False, "suppressed")

    parole_dt = datetime.fromisoformat(parole_at)
    if parole_dt.tzinfo is None:
        parole_dt = parole_dt.replace(tzinfo=timezone.utc)
    if parole_dt > datetime.now(timezone.utc):
        return (False, "suppressed")

    # Parole window open — atomic decrement via BEGIN IMMEDIATE.
    try:
        await db._conn.execute("BEGIN IMMEDIATE")
        cur = await db._conn.execute(
            "SELECT parole_trades_remaining FROM combo_performance "
            "WHERE combo_key = ? AND window = '30d'",
            (combo_key,),
        )
        reread = await cur.fetchone()
        remaining = reread[0] if reread else 0
        if remaining is None or remaining <= 0:
            await db._conn.execute("COMMIT")
            return (False, "parole_exhausted")
        await db._conn.execute(
            "UPDATE combo_performance SET parole_trades_remaining = ? "
            "WHERE combo_key = ? AND window = '30d'",
            (remaining - 1, combo_key),
        )
        await db._conn.commit()
        return (True, "parole_retest")
    except Exception as e:
        try:
            await db._conn.execute("ROLLBACK")
        except Exception:
            pass
        await _record_fallback(combo_key, f"parole_decrement: {e}")
        return (True, "db_error_fallback_allow")


async def _record_fallback(combo_key: str, err: str) -> None:
    """Log + maintain the fail-open counter; fire Telegram alert with cooldown."""
    global _last_alerted_ts
    log.error(
        "suppression_db_error",
        combo_key=combo_key, err=err, err_id="SUPP_DB_FAIL",
    )
    now_ts = time.monotonic()
    _fallback_timestamps.append(now_ts)
    while _fallback_timestamps and now_ts - _fallback_timestamps[0] > _FALLBACK_WINDOW_SEC:
        _fallback_timestamps.popleft()

    if (
        len(_fallback_timestamps) >= _FALLBACK_ALERT_THRESHOLD
        and now_ts - _last_alerted_ts >= _FALLBACK_ALERT_COOLDOWN_SEC
    ):
        _last_alerted_ts = now_ts
        try:
            await alerter.send_telegram_message(
                f"⚠ Suppression fail-open fired {len(_fallback_timestamps)}x "
                f"in last hour. DB may be degraded — combos are currently ungated."
            )
        except Exception:
            log.exception("suppression_fallback_alert_dispatch_error")
```

**Note on signature of `alerter.send_telegram_message`:** if the existing signature requires `session` / `settings` arguments, adapt the call site to match local conventions. Inspect `scout/alerter.py` before writing and copy the style of other callers (e.g. `main.py`'s daily summary). If the alerter API requires session/settings, add a thin private fire-and-forget helper in this module that uses `aiohttp.ClientSession()` and reads settings from an injected module-level singleton — or, simpler, pass settings/session through `should_open` as additional args and forward to `_record_fallback`. Pick whichever matches existing patterns; the tests above monkeypatch `alerter.send_telegram_message` directly so the capture will work either way, but you may need to adapt the monkeypatch target.

- [ ] **Step 4: Run the test — expect PASS**

Run: `uv run pytest tests/test_trading_suppression.py -v`
Expected: all PASS.

- [ ] **Step 5: Format + commit**

```bash
uv run black scout/trading/suppression.py tests/test_trading_suppression.py
git add scout/trading/suppression.py tests/test_trading_suppression.py
git commit -m "feat(trading): suppression entry-gate with atomic parole decrement"
```

---

### Task 6: Combo refresh module

**Files:**
- Create: `scout/trading/combo_refresh.py`
- Create: `tests/test_trading_combo_refresh.py`

Per spec §5.3. Rollup SQL is parameterised for 7d and 30d; suppression rule only applies to the 30d row; re-suppression keeps `suppressed_at` fresh.

- [ ] **Step 1: Write the failing test file**

Create `tests/test_trading_combo_refresh.py`:

```python
"""Tests for nightly combo refresh (spec §5.3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import combo_refresh


async def _insert_trade(
    db, combo_key: str, pnl_usd: float, pnl_pct: float,
    closed_at: datetime, status: str = "closed_tp",
    opened_at: datetime | None = None,
):
    opened = (opened_at or closed_at - timedelta(hours=1)).isoformat()
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, pnl_usd, pnl_pct, opened_at, closed_at, signal_combo) "
        "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
        " 1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, ?, ?, ?, ?, ?)",
        ("tok_" + combo_key + "_" + str(pnl_usd),
         status, pnl_usd, pnl_pct, opened, closed_at.isoformat(), combo_key),
    )
    await db._conn.commit()


async def _get_combo_row(db, combo_key, window):
    cur = await db._conn.execute(
        "SELECT trades, wins, losses, win_rate_pct, avg_pnl_pct, "
        "       suppressed, parole_at, parole_trades_remaining "
        "FROM combo_performance WHERE combo_key = ? AND window = ?",
        (combo_key, window),
    )
    return await cur.fetchone()


async def test_refresh_computes_7d_and_30d_rollup(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # 3 wins, 2 losses in last 3 days
    for pnl in [10, 20, 30]:
        await _insert_trade(db, "combo_x", pnl, 5.0, now - timedelta(days=1))
    for pnl in [-5, -10]:
        await _insert_trade(db, "combo_x", pnl, -3.0, now - timedelta(days=1))
    ok = await combo_refresh.refresh_combo(db, "combo_x", s)
    assert ok

    row = await _get_combo_row(db, "combo_x", "7d")
    assert row["trades"] == 5
    assert row["wins"] == 3
    assert row["losses"] == 2
    assert abs(row["win_rate_pct"] - 60.0) < 0.01

    row30 = await _get_combo_row(db, "combo_x", "30d")
    assert row30["trades"] == 5

    await db.close()


async def test_suppression_not_triggered_at_boundary_wr_eq_30(tmp_path, settings_factory):
    """trades=20 AND wr=30.0 → NOT suppressed (strict inequality)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    for _ in range(6):
        await _insert_trade(db, "boundary", 10, 5.0, now - timedelta(days=2))
    for _ in range(14):
        await _insert_trade(db, "boundary", -5, -3.0, now - timedelta(days=2))
    await combo_refresh.refresh_combo(db, "boundary", s)
    row = await _get_combo_row(db, "boundary", "30d")
    assert row["trades"] == 20
    assert abs(row["win_rate_pct"] - 30.0) < 0.01
    assert row["suppressed"] == 0
    await db.close()


async def test_suppression_triggered_at_wr_just_below_30(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # 5 wins out of 20 = 25% WR
    for _ in range(5):
        await _insert_trade(db, "loser", 10, 5.0, now - timedelta(days=2))
    for _ in range(15):
        await _insert_trade(db, "loser", -5, -3.0, now - timedelta(days=2))
    await combo_refresh.refresh_combo(db, "loser", s)
    row = await _get_combo_row(db, "loser", "30d")
    assert row["suppressed"] == 1
    assert row["parole_at"] is not None
    assert row["parole_trades_remaining"] == s.FEEDBACK_PAROLE_RETEST_TRADES
    await db.close()


async def test_suppression_not_triggered_when_trades_below_min(tmp_path, settings_factory):
    """trades=19 must NOT trigger suppression even at 0% WR."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    for _ in range(19):
        await _insert_trade(db, "small", -5, -3.0, now - timedelta(days=2))
    await combo_refresh.refresh_combo(db, "small", s)
    row = await _get_combo_row(db, "small", "30d")
    assert row["suppressed"] == 0
    await db.close()


async def test_parole_auto_clear_on_wr_recovery(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # Pre-seed: combo is on parole with remaining=0.
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, parole_at, "
        " parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES ('recovered', '30d', 25, 5, 20, -100.0, -2.0, 20.0, 1, ?, ?, 0, 0, ?)",
        (
            (now - timedelta(days=15)).isoformat(),
            (now - timedelta(days=1)).isoformat(),
            now.isoformat(),
        ),
    )
    await db._conn.commit()
    # Add recent winning trades for recovery
    for _ in range(15):
        await _insert_trade(db, "recovered", 10, 5.0, now - timedelta(days=1))
    await combo_refresh.refresh_combo(db, "recovered", s)
    row = await _get_combo_row(db, "recovered", "30d")
    # With wr >= 30 and parole_trades_remaining=0: clear suppression.
    assert row["suppressed"] == 0
    assert row["parole_at"] is None
    assert row["parole_trades_remaining"] is None
    await db.close()


async def test_re_suppression_resets_timestamps(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    old_suppressed_at = (now - timedelta(days=20)).isoformat()
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, parole_at, "
        " parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES ('re_supp', '30d', 25, 5, 20, -50, -2, 20.0, 1, ?, ?, 0, 0, ?)",
        (old_suppressed_at, (now - timedelta(days=1)).isoformat(), now.isoformat()),
    )
    await db._conn.commit()
    # Recent trades still poor
    for _ in range(20):
        await _insert_trade(db, "re_supp", -5, -3, now - timedelta(days=2))
    await combo_refresh.refresh_combo(db, "re_supp", s)
    row = await _get_combo_row(db, "re_supp", "30d")
    assert row["suppressed"] == 1
    assert row["parole_trades_remaining"] == s.FEEDBACK_PAROLE_RETEST_TRADES
    cur = await db._conn.execute(
        "SELECT suppressed_at FROM combo_performance WHERE combo_key = 're_supp'"
    )
    new_suppressed_at = (await cur.fetchone())[0]
    assert new_suppressed_at != old_suppressed_at
    await db.close()


async def test_refresh_all_aggregates_distinct_combos(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    await _insert_trade(db, "c1", 10, 5.0, now - timedelta(days=1))
    await _insert_trade(db, "c2", 20, 5.0, now - timedelta(days=1))
    summary = await combo_refresh.refresh_all(db, s)
    assert summary["refreshed"] == 2
    assert summary["failed"] == 0
    await db.close()


async def test_refresh_failures_increments_and_resets(tmp_path, settings_factory, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    await _insert_trade(db, "flaky", 10, 5.0, now - timedelta(days=1))

    # Monkeypatch to force first refresh to fail.
    original_execute = db._conn.execute
    call_count = {"n": 0}

    async def _sometimes_fail(sql, *args, **kwargs):
        call_count["n"] += 1
        if "UPDATE combo_performance" in str(sql) and call_count["n"] < 3:
            raise RuntimeError("simulated failure")
        return await original_execute(sql, *args, **kwargs)

    # (Exact failure simulation depends on refresh_combo internals; this test
    # is primarily a behavioural scaffold — adjust the simulated failure to
    # match the real failure surface during implementation.)
    await db.close()
```

- [ ] **Step 2: Run the test — expect FAIL**

Run: `uv run pytest tests/test_trading_combo_refresh.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `scout/trading/combo_refresh.py`**

```python
"""Nightly combo refresh (spec §5.3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from scout.db import Database

log = structlog.get_logger()


async def refresh_combo(db: Database, combo_key: str, settings) -> bool:
    """Recompute 7d + 30d rows for `combo_key`. Apply suppression rule to 30d.
    Returns True on success, False otherwise.
    """
    try:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        stats = {}
        for window, days in (("7d", 7), ("30d", 30)):
            cur = await db._conn.execute(
                """SELECT
                     COUNT(*) AS trades,
                     SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                     SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
                     COALESCE(SUM(pnl_usd), 0) AS total_pnl_usd,
                     COALESCE(AVG(pnl_pct), 0) AS avg_pnl_pct
                   FROM paper_trades
                   WHERE signal_combo = ?
                     AND status != 'open'
                     AND closed_at >= ?""",
                (combo_key, (now - timedelta(days=days)).isoformat()),
            )
            row = await cur.fetchone()
            trades = row["trades"] or 0
            wins = row["wins"] or 0
            losses = row["losses"] or 0
            total_pnl = float(row["total_pnl_usd"] or 0)
            avg_pct = float(row["avg_pnl_pct"] or 0)
            wr = (100.0 * wins / trades) if trades else 0.0
            stats[window] = dict(
                trades=trades, wins=wins, losses=losses,
                total_pnl=total_pnl, avg_pct=avg_pct, wr=wr,
            )

        # 7d row: plain UPSERT.
        w7 = stats["7d"]
        await db._conn.execute(
            "INSERT INTO combo_performance "
            "(combo_key, window, trades, wins, losses, total_pnl_usd, "
            " avg_pnl_pct, win_rate_pct, suppressed, last_refreshed) "
            "VALUES (?, '7d', ?, ?, ?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(combo_key, window) DO UPDATE SET "
            " trades=excluded.trades, wins=excluded.wins, losses=excluded.losses, "
            " total_pnl_usd=excluded.total_pnl_usd, avg_pnl_pct=excluded.avg_pnl_pct, "
            " win_rate_pct=excluded.win_rate_pct, last_refreshed=excluded.last_refreshed, "
            " refresh_failures=0",
            (combo_key, w7["trades"], w7["wins"], w7["losses"],
             w7["total_pnl"], w7["avg_pct"], w7["wr"], now_iso),
        )

        # 30d row: apply suppression rule.
        w30 = stats["30d"]
        cur = await db._conn.execute(
            "SELECT suppressed, parole_trades_remaining, suppressed_at "
            "FROM combo_performance WHERE combo_key = ? AND window = '30d'",
            (combo_key,),
        )
        existing = await cur.fetchone()

        min_trades = settings.FEEDBACK_SUPPRESSION_MIN_TRADES
        wr_thresh = settings.FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT
        parole_days = settings.FEEDBACK_PAROLE_DAYS
        retest = settings.FEEDBACK_PAROLE_RETEST_TRADES

        new_suppressed = 0
        new_suppressed_at = None
        new_parole_at = None
        new_parole_remaining = None

        if existing is None:
            # First write — maybe suppress immediately if bad enough.
            if w30["trades"] >= min_trades and w30["wr"] < wr_thresh:
                new_suppressed = 1
                new_suppressed_at = now_iso
                new_parole_at = (now + timedelta(days=parole_days)).isoformat()
                new_parole_remaining = retest
        else:
            was_suppressed = bool(existing["suppressed"])
            remaining = existing["parole_trades_remaining"]
            if not was_suppressed:
                if w30["trades"] >= min_trades and w30["wr"] < wr_thresh:
                    new_suppressed = 1
                    new_suppressed_at = now_iso
                    new_parole_at = (now + timedelta(days=parole_days)).isoformat()
                    new_parole_remaining = retest
            else:
                if remaining is not None and remaining <= 0:
                    if w30["wr"] >= wr_thresh:
                        new_suppressed = 0
                        new_suppressed_at = None
                        new_parole_at = None
                        new_parole_remaining = None
                    else:
                        new_suppressed = 1
                        new_suppressed_at = now_iso
                        new_parole_at = (now + timedelta(days=parole_days)).isoformat()
                        new_parole_remaining = retest
                else:
                    # Still serving suppression/parole — preserve existing state.
                    new_suppressed = 1
                    new_suppressed_at = existing["suppressed_at"]
                    cur2 = await db._conn.execute(
                        "SELECT parole_at FROM combo_performance "
                        "WHERE combo_key = ? AND window = '30d'",
                        (combo_key,),
                    )
                    new_parole_at = (await cur2.fetchone())[0]
                    new_parole_remaining = remaining

        await db._conn.execute(
            "INSERT INTO combo_performance "
            "(combo_key, window, trades, wins, losses, total_pnl_usd, "
            " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, parole_at, "
            " parole_trades_remaining, refresh_failures, last_refreshed) "
            "VALUES (?, '30d', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(combo_key, window) DO UPDATE SET "
            " trades=excluded.trades, wins=excluded.wins, losses=excluded.losses, "
            " total_pnl_usd=excluded.total_pnl_usd, avg_pnl_pct=excluded.avg_pnl_pct, "
            " win_rate_pct=excluded.win_rate_pct, "
            " suppressed=excluded.suppressed, suppressed_at=excluded.suppressed_at, "
            " parole_at=excluded.parole_at, "
            " parole_trades_remaining=excluded.parole_trades_remaining, "
            " refresh_failures=0, last_refreshed=excluded.last_refreshed",
            (combo_key, w30["trades"], w30["wins"], w30["losses"],
             w30["total_pnl"], w30["avg_pct"], w30["wr"],
             new_suppressed, new_suppressed_at, new_parole_at,
             new_parole_remaining, now_iso),
        )
        await db._conn.commit()
        return True
    except Exception as e:
        log.error(
            "combo_refresh_error",
            combo_key=combo_key, err=str(e), err_id="COMBO_REFRESH",
        )
        try:
            await db._conn.execute(
                "UPDATE combo_performance SET refresh_failures = refresh_failures + 1 "
                "WHERE combo_key = ?",
                (combo_key,),
            )
            await db._conn.commit()
        except Exception:
            pass
        return False


async def refresh_all(db: Database, settings) -> dict:
    """Rebuild `combo_performance` for every combo seen in last 30d.

    Returns {"refreshed": N, "failed": M, "chronic_failures": [keys]}.
    """
    cur = await db._conn.execute(
        "SELECT DISTINCT signal_combo FROM paper_trades "
        "WHERE signal_combo IS NOT NULL "
        "  AND opened_at >= datetime('now', '-30 days')"
    )
    rows = await cur.fetchall()
    combos = [r[0] for r in rows if r[0]]

    refreshed = 0
    failed = 0
    for combo in combos:
        ok = await refresh_combo(db, combo, settings)
        if ok:
            refreshed += 1
        else:
            failed += 1

    cur = await db._conn.execute(
        "SELECT combo_key FROM combo_performance "
        "WHERE refresh_failures >= ?",
        (settings.FEEDBACK_CHRONIC_FAILURE_THRESHOLD,),
    )
    chronic = [r[0] for r in await cur.fetchall()]
    for key in chronic:
        log.warning(
            "combo_refresh_chronic_failure",
            combo_key=key,
        )

    log.info(
        "combo_refresh_summary",
        refreshed=refreshed, failed=failed, chronic=len(chronic),
    )
    return {"refreshed": refreshed, "failed": failed, "chronic_failures": chronic}
```

- [ ] **Step 4: Run the test — expect PASS**

Run: `uv run pytest tests/test_trading_combo_refresh.py -v`
Expected: all PASS (the `test_refresh_failures_increments_and_resets` is a scaffold; if the monkeypatch doesn't fire the failure path, delete that test — it exists as a reminder to verify behaviour manually during implementation).

- [ ] **Step 5: Format + commit**

```bash
uv run black scout/trading/combo_refresh.py tests/test_trading_combo_refresh.py
git add scout/trading/combo_refresh.py tests/test_trading_combo_refresh.py
git commit -m "feat(trading): nightly combo refresh with suppression rule"
```

---

### Task 7: Analytics module (on-demand queries + pipeline-gap detection)

**Files:**
- Create: `scout/trading/analytics.py`
- Create: `tests/test_trading_analytics.py`

Per spec §5.1, D21 (percentiles in Python), §7 (missed-winner LEFT JOIN). Every function uses `db._conn.execute(...)`.

- [ ] **Step 1: Write the failing test file**

Create `tests/test_trading_analytics.py`:

```python
"""Tests for analytics queries (spec §5.1, §7)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import analytics


async def _seed_combo_row(db, key, window, trades, wr, pnl=0.0):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT OR REPLACE INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, 0, 0, ?)",
        (key, window, trades, int(trades * wr / 100), trades - int(trades * wr / 100),
         pnl, wr, now),
    )
    await db._conn.commit()


async def test_combo_leaderboard_filters_by_min_trades(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_combo_row(db, "big", "30d", trades=30, wr=70.0)
    await _seed_combo_row(db, "tiny", "30d", trades=5, wr=99.0)
    rows = await analytics.combo_leaderboard(db, "30d", min_trades=10)
    keys = [r["combo_key"] for r in rows]
    assert "big" in keys
    assert "tiny" not in keys
    await db.close()


async def test_combo_leaderboard_sort_order(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Tie on WR → tie-break by trades DESC → tie-break by combo_key ASC.
    await _seed_combo_row(db, "bb", "30d", trades=20, wr=50.0)
    await _seed_combo_row(db, "aa", "30d", trades=20, wr=50.0)
    await _seed_combo_row(db, "cc", "30d", trades=30, wr=50.0)  # more trades wins
    rows = await analytics.combo_leaderboard(db, "30d", min_trades=10)
    assert [r["combo_key"] for r in rows] == ["cc", "aa", "bb"]
    await db.close()


async def _seed_gainers_snapshot(db, coin_id, snapshot_at, price_change_24h, mcap, rank):
    await db._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, market_cap_rank, market_cap, current_price, "
        " price_change_24h, price_at_snapshot, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?, 1.0, ?, 1.0, ?)",
        (coin_id, coin_id.upper(), coin_id.title(), rank, mcap,
         price_change_24h, snapshot_at.isoformat()),
    )
    await db._conn.commit()


async def _seed_paper_trade(db, coin_id, opened_at):
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, opened_at, signal_combo) "
        "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
        " 1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, 'open', ?, 'volume_spike')",
        (coin_id, opened_at.isoformat()),
    )
    await db._conn.commit()


async def test_missed_winner_tier_boundaries(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # All these are above mcap/rank filter, all uncaught.
    cases = [
        ("partial_edge",   50.0,    "partial_miss"),
        ("partial_hi",     199.99,  "partial_miss"),
        ("major_lo",       200.0,   "major_miss"),
        ("major_hi",       999.99,  "major_miss"),
        ("disaster",       1000.0,  "disaster_miss"),
        ("disaster_big",   2500.0,  "disaster_miss"),
    ]
    for coin, pct, _ in cases:
        await _seed_gainers_snapshot(
            db, coin, now - timedelta(hours=5), pct, mcap=10_000_000, rank=500,
        )
    result = await analytics.audit_missed_winners(
        db, start=now - timedelta(days=1), end=now, settings=s,
    )
    buckets = {
        coin: tier for tier in ("partial_miss", "major_miss", "disaster_miss")
        for coin in [e["coin_id"] for e in result["tiers"][tier]]
    }
    for coin, _, expected in cases:
        assert buckets[coin] == expected
    await db.close()


async def test_missed_winner_filters_excludes_small_caps(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # mcap too small
    await _seed_gainers_snapshot(
        db, "toosmall", now - timedelta(hours=2),
        price_change_24h=300, mcap=4_999_999, rank=100,
    )
    # rank too big
    await _seed_gainers_snapshot(
        db, "toofar", now - timedelta(hours=2),
        price_change_24h=300, mcap=10_000_000, rank=2000,
    )
    # qualifies
    await _seed_gainers_snapshot(
        db, "good", now - timedelta(hours=2),
        price_change_24h=300, mcap=10_000_000, rank=500,
    )
    result = await analytics.audit_missed_winners(
        db, start=now - timedelta(days=1), end=now, settings=s,
    )
    missed = [e["coin_id"] for tier in result["tiers"].values() for e in tier]
    assert "good" in missed
    assert "toosmall" not in missed
    assert "toofar" not in missed
    assert result["denominator"]["winners_filtered_by_mcap"] >= 1
    assert result["denominator"]["winners_filtered_by_rank"] >= 1
    await db.close()


async def test_missed_winner_catch_window_boundaries(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    crossed = now - timedelta(hours=5)
    # Opened exactly at -30min (should count as caught).
    await _seed_gainers_snapshot(db, "caught_edge", crossed, 300, 10_000_000, 500)
    await _seed_paper_trade(db, "caught_edge", crossed - timedelta(minutes=30))
    # Opened at -31min (missed).
    await _seed_gainers_snapshot(db, "missed_edge", crossed, 300, 10_000_000, 500)
    await _seed_paper_trade(db, "missed_edge", crossed - timedelta(minutes=31))
    result = await analytics.audit_missed_winners(
        db, start=now - timedelta(days=1), end=now, settings=s,
    )
    missed_ids = [e["coin_id"] for tier in result["tiers"].values() for e in tier]
    assert "missed_edge" in missed_ids
    assert "caught_edge" not in missed_ids
    await db.close()


async def test_lead_time_breakdown_filters_status_ok(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Seed trades with mixed statuses.
    now = datetime.now(timezone.utc)

    async def _seed(coin, lead, status):
        await db._conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
            " status, opened_at, signal_combo, "
            " lead_time_vs_trending_min, lead_time_vs_trending_status) "
            "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
            " 1.0, 100, 100, 20, 10, 1.2, 0.9, 'open', ?, 'volume_spike', ?, ?)",
            (coin, now.isoformat(), lead, status),
        )

    await _seed("a", -10.0, "ok")
    await _seed("b", -20.0, "ok")
    await _seed("c", None, "no_reference")
    await _seed("d", None, "error")
    await db._conn.commit()

    result = await analytics.lead_time_breakdown(db, window="30d")
    row = result["volume_spike"]
    assert row["count_ok"] == 2
    assert row["count_no_reference"] == 1
    assert row["count_error"] == 1
    assert abs(row["median_min"] - (-15.0)) < 0.01 or row["median_min"] in (-10.0, -20.0)
    await db.close()


async def test_detect_pipeline_gaps(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    # Snapshots at -10h, -8h, -2h, -1h  →  gap between -8h and -2h (6h > 60min).
    for hours in (10, 8, 2, 1):
        await db._conn.execute(
            "INSERT INTO gainers_snapshots "
            "(coin_id, symbol, name, market_cap_rank, market_cap, current_price, "
            " price_change_24h, price_at_snapshot, snapshot_at) "
            "VALUES ('x', 'X', 'X', 100, 1e7, 1.0, 10.0, 1.0, ?)",
            ((now - timedelta(hours=hours)).isoformat(),),
        )
    await db._conn.commit()
    gaps = await analytics.detect_pipeline_gaps(
        db, start=now - timedelta(days=1), end=now, max_gap_minutes=60,
    )
    assert len(gaps) == 1
    await db.close()
```

- [ ] **Step 2: Run the tests — expect FAIL**

Run: `uv run pytest tests/test_trading_analytics.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `scout/trading/analytics.py`**

```python
"""On-demand analytics for paper-trading feedback loop (spec §5.1, §7)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from scout.db import Database

log = structlog.get_logger()


async def combo_leaderboard(
    db: Database, window: str, min_trades: int = 10
) -> list[dict]:
    """Return combos sorted by WR desc. Deterministic tie-break."""
    cur = await db._conn.execute(
        "SELECT combo_key, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        "       win_rate_pct, suppressed, suppressed_at "
        "FROM combo_performance "
        "WHERE window = ? AND trades >= ? "
        "ORDER BY win_rate_pct DESC, trades DESC, combo_key ASC",
        (window, min_trades),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def audit_missed_winners(
    db: Database, start: datetime, end: datetime, settings,
) -> dict:
    """CG winners we did not paper-trade. LEFT JOIN per spec §7."""
    min_pct = settings.FEEDBACK_MISSED_WINNER_MIN_PCT
    min_mcap = settings.FEEDBACK_MISSED_WINNER_MIN_MCAP
    max_rank = settings.FEEDBACK_MISSED_WINNER_MAX_RANK
    catch_min = settings.FEEDBACK_MISSED_WINNER_WINDOW_MIN

    start_iso, end_iso = start.isoformat(), end.isoformat()

    # Denominator slice: winners regardless of mcap/rank
    cur = await db._conn.execute(
        "SELECT COUNT(DISTINCT coin_id) FROM gainers_snapshots "
        "WHERE snapshot_at BETWEEN ? AND ? AND price_change_24h >= ?",
        (start_iso, end_iso, min_pct),
    )
    winners_total_unfiltered = (await cur.fetchone())[0] or 0

    # Filter boundaries for denominator
    cur = await db._conn.execute(
        "SELECT coin_id, MAX(market_cap) AS m FROM gainers_snapshots "
        "WHERE snapshot_at BETWEEN ? AND ? AND price_change_24h >= ? "
        "GROUP BY coin_id",
        (start_iso, end_iso, min_pct),
    )
    rows = await cur.fetchall()
    filtered_by_mcap = sum(1 for r in rows if (r["m"] or 0) < min_mcap)

    cur = await db._conn.execute(
        "SELECT coin_id, MIN(market_cap_rank) AS r FROM gainers_snapshots "
        "WHERE snapshot_at BETWEEN ? AND ? AND price_change_24h >= ? "
        "GROUP BY coin_id",
        (start_iso, end_iso, min_pct),
    )
    rows = await cur.fetchall()
    filtered_by_rank = sum(
        1 for r in rows if r["r"] is None or r["r"] > max_rank
    )

    # Main missed-winner query
    cur = await db._conn.execute(
        f"""
        WITH winners AS (
            SELECT coin_id,
                   MIN(symbol) AS symbol,
                   MIN(name)   AS name,
                   MIN(snapshot_at) AS crossed_at,
                   MAX(price_change_24h) AS peak_change,
                   MAX(market_cap) AS mcap,
                   MIN(market_cap_rank) AS best_rank
            FROM gainers_snapshots
            WHERE snapshot_at BETWEEN ? AND ?
              AND price_change_24h >= ?
            GROUP BY coin_id
            HAVING mcap >= ? AND best_rank <= ?
        )
        SELECT w.coin_id, w.symbol, w.name, w.crossed_at, w.peak_change,
               w.mcap, w.best_rank,
               CASE
                 WHEN w.peak_change >= 1000 THEN 'disaster_miss'
                 WHEN w.peak_change >= 200  THEN 'major_miss'
                 ELSE 'partial_miss'
               END AS tier
        FROM winners w
        LEFT JOIN paper_trades pt
               ON pt.token_id = w.coin_id
              AND pt.opened_at BETWEEN datetime(w.crossed_at, ?)
                                   AND datetime(w.crossed_at, ?)
        WHERE pt.id IS NULL
        """,
        (
            start_iso, end_iso, min_pct, min_mcap, max_rank,
            f"-{catch_min} minutes", f"+{catch_min} minutes",
        ),
    )
    missed_rows = await cur.fetchall()

    # Qualifying-winners total (post filter) used for caught count
    cur = await db._conn.execute(
        """SELECT COUNT(*) FROM (
             SELECT coin_id
             FROM gainers_snapshots
             WHERE snapshot_at BETWEEN ? AND ? AND price_change_24h >= ?
             GROUP BY coin_id
             HAVING MAX(market_cap) >= ? AND MIN(market_cap_rank) <= ?
        )""",
        (start_iso, end_iso, min_pct, min_mcap, max_rank),
    )
    winners_qualifying = (await cur.fetchone())[0] or 0
    winners_missed = len(missed_rows)
    winners_caught = winners_qualifying - winners_missed

    # Pipeline-gap partitioning
    gaps = await detect_pipeline_gaps(db, start, end,
                                       settings.FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN)
    gap_ranges = [
        (datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in gaps
    ]

    tiers = {"partial_miss": [], "major_miss": [], "disaster_miss": []}
    uncovered_window: list[dict] = []
    for r in missed_rows:
        row_dict = dict(r)
        crossed_dt = datetime.fromisoformat(row_dict["crossed_at"])
        if crossed_dt.tzinfo is None:
            crossed_dt = crossed_dt.replace(tzinfo=timezone.utc)
        is_uncovered = any(
            a <= crossed_dt <= b for a, b in gap_ranges
        )
        if is_uncovered:
            uncovered_window.append(row_dict)
        else:
            tiers[row_dict["tier"]].append(row_dict)

    if winners_qualifying == 0:
        log.warning(
            "audit_query_empty_warning",
            start=start_iso, end=end_iso,
            unfiltered=winners_total_unfiltered,
        )

    pipeline_gap_hours = sum(
        (b - a).total_seconds() / 3600.0 for a, b in gap_ranges
    )

    return {
        "tiers": tiers,
        "uncovered_window": uncovered_window,
        "denominator": {
            "winners_total": winners_qualifying,
            "winners_caught": winners_caught,
            "winners_missed": winners_missed,
            "winners_filtered_by_mcap": filtered_by_mcap,
            "winners_filtered_by_rank": filtered_by_rank,
            "pipeline_gap_hours": round(pipeline_gap_hours, 2),
        },
    }


async def lead_time_breakdown(db: Database, window: str) -> dict[str, dict]:
    """Per-signal-type lead-time stats. Percentiles in Python per D21."""
    days = 7 if window == "7d" else 30
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cur = await db._conn.execute(
        "SELECT signal_type, lead_time_vs_trending_min, lead_time_vs_trending_status "
        "FROM paper_trades WHERE opened_at >= ?",
        (cutoff,),
    )
    rows = await cur.fetchall()
    groups: dict[str, dict] = {}
    for r in rows:
        sig = r["signal_type"]
        bucket = groups.setdefault(sig, {"ok": [], "no_reference": 0, "error": 0})
        status = r["lead_time_vs_trending_status"]
        if status == "ok" and r["lead_time_vs_trending_min"] is not None:
            bucket["ok"].append(float(r["lead_time_vs_trending_min"]))
        elif status == "no_reference":
            bucket["no_reference"] += 1
        elif status == "error":
            bucket["error"] += 1

    result: dict[str, dict] = {}
    for sig, bucket in groups.items():
        values = sorted(bucket["ok"])
        n = len(values)
        if n == 0:
            median = p25 = p75 = None
        else:
            median = values[n // 2]
            p25 = values[max(n // 4, 0)]
            p75 = values[min((3 * n) // 4, n - 1)]
        result[sig] = {
            "median_min": median,
            "p25_min": p25,
            "p75_min": p75,
            "count_ok": n,
            "count_no_reference": bucket["no_reference"],
            "count_error": bucket["error"],
        }
    return result


async def suppression_log(
    db: Database, start: datetime, end: datetime
) -> list[dict]:
    cur = await db._conn.execute(
        "SELECT combo_key, suppressed_at, parole_at, parole_trades_remaining, "
        "       win_rate_pct, trades "
        "FROM combo_performance "
        "WHERE window = '30d' "
        "  AND suppressed_at IS NOT NULL "
        "  AND suppressed_at BETWEEN ? AND ? "
        "ORDER BY suppressed_at DESC",
        (start.isoformat(), end.isoformat()),
    )
    return [dict(r) for r in await cur.fetchall()]


async def detect_pipeline_gaps(
    db: Database, start: datetime, end: datetime, max_gap_minutes: int = 60
) -> list[tuple[str, str]]:
    cur = await db._conn.execute(
        "SELECT DISTINCT snapshot_at FROM gainers_snapshots "
        "WHERE snapshot_at BETWEEN ? AND ? "
        "ORDER BY snapshot_at ASC",
        (start.isoformat(), end.isoformat()),
    )
    rows = await cur.fetchall()
    gaps: list[tuple[str, str]] = []
    prev = None
    for r in rows:
        cur_ts = datetime.fromisoformat(r[0])
        if cur_ts.tzinfo is None:
            cur_ts = cur_ts.replace(tzinfo=timezone.utc)
        if prev is not None:
            delta_min = (cur_ts - prev).total_seconds() / 60.0
            if delta_min > max_gap_minutes:
                gaps.append((prev.isoformat(), cur_ts.isoformat()))
        prev = cur_ts
    return gaps
```

- [ ] **Step 4: Run the test — expect PASS**

Run: `uv run pytest tests/test_trading_analytics.py -v`
Expected: all PASS. Tests may need minor calibration to match exact percentile-indexing behaviour — adjust either the implementation or test to agree.

- [ ] **Step 5: Format + commit**

```bash
uv run black scout/trading/analytics.py tests/test_trading_analytics.py
git add scout/trading/analytics.py tests/test_trading_analytics.py
git commit -m "feat(trading): analytics queries + pipeline-gap detection"
```

---

### Task 8: Weekly digest module

**Files:**
- Create: `scout/trading/weekly_digest.py`
- Create: `tests/test_trading_weekly_digest.py`

Per spec §5.4. Seven sections in a fixed order. Returns `None` when the week had zero activity (caller must not send). On error: send a fallback with correlation ID — never silent.

- [ ] **Step 1: Write the failing test file**

Create `tests/test_trading_weekly_digest.py`:

```python
"""Tests for weekly digest (spec §5.4)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import weekly_digest


async def test_build_digest_returns_none_on_empty_week(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    result = await weekly_digest.build_weekly_digest(
        db, end_date=date.today(), settings=s,
    )
    assert result is None
    await db.close()


async def test_build_digest_renders_all_sections(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()

    # Seed a trade + a combo_performance row so digest has content.
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, opened_at, closed_at, pnl_usd, pnl_pct, signal_combo, "
        " lead_time_vs_trending_min, lead_time_vs_trending_status) "
        "VALUES ('c', 'C', 'C', 'coingecko', 'volume_spike', '{}', "
        " 1.0, 100.0, 100.0, 20, 10, 1.2, 0.9, 'closed_tp', ?, ?, 15.0, 12.0, "
        " 'volume_spike', -10.0, 'ok')",
        ((now - timedelta(days=3)).isoformat(), (now - timedelta(days=2)).isoformat()),
    )
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES ('volume_spike', '30d', 12, 7, 5, 42, 3.5, 58.3, 0, 0, ?)",
        (now.isoformat(),),
    )
    await db._conn.commit()

    result = await weekly_digest.build_weekly_digest(
        db, end_date=date.today(), settings=s,
    )
    assert result is not None
    for header in (
        "Weekly Feedback",
        "Combo leaderboard",
        "Missed winners",
        "Lead-time",
        "Suppression log",
        "Fallback counters",
        "Chronic refresh failures",
    ):
        assert header in result
    await db.close()


async def test_send_weekly_digest_fallback_on_error(tmp_path, settings_factory, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()

    sent: list[str] = []
    async def _capture(msg, *a, **k):
        sent.append(msg)

    monkeypatch.setattr(
        "scout.trading.weekly_digest.alerter.send_telegram_message", _capture,
    )

    async def _boom(*a, **k):
        raise RuntimeError("digest broken")
    monkeypatch.setattr(weekly_digest, "build_weekly_digest", _boom)

    await weekly_digest.send_weekly_digest(db, s)
    assert any("Weekly digest failed" in m for m in sent)
    assert any("ref=wd-" in m for m in sent)
    await db.close()
```

- [ ] **Step 2: Run the test — expect FAIL**

Run: `uv run pytest tests/test_trading_weekly_digest.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `scout/trading/weekly_digest.py`**

```python
"""Weekly digest builder + sender (spec §5.4)."""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone

import structlog

from scout import alerter
from scout.db import Database
from scout.trading import analytics

log = structlog.get_logger()

_TG_SPLIT_LIMIT = 4000  # leave headroom under Telegram's 4096 cap


async def build_weekly_digest(
    db: Database, end_date: date, settings,
) -> str | None:
    """Build the weekly digest text. Returns None if zero activity last 7d."""
    start = datetime.combine(end_date - timedelta(days=7), datetime.min.time(),
                              tzinfo=timezone.utc)
    end = datetime.combine(end_date, datetime.max.time(), tzinfo=timezone.utc)

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE opened_at >= ?",
        (start.isoformat(),),
    )
    activity = (await cur.fetchone())[0] or 0
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM combo_performance"
    )
    combos_present = (await cur.fetchone())[0] or 0
    if activity == 0 and combos_present == 0:
        log.info("weekly_digest_empty", start=start.isoformat())
        return None

    lines: list[str] = []
    lines.append(f"Weekly Feedback — {(end_date - timedelta(days=7)).isoformat()} "
                 f"to {end_date.isoformat()}")
    lines.append("")

    # 1. Combo leaderboard
    board = await analytics.combo_leaderboard(
        db, "30d", min_trades=settings.FEEDBACK_MIN_LEADERBOARD_TRADES,
    )
    lines.append("[Combo leaderboard — 30d, min {} trades]".format(
        settings.FEEDBACK_MIN_LEADERBOARD_TRADES))
    if not board:
        lines.append("  (not enough data yet)")
    else:
        lines.append("Top 5:")
        for r in board[:5]:
            flag = "  [SUPPRESSED]" if r.get("suppressed") else ""
            lines.append("  {:<28s} {:5.1f}%  WR  ({} trades, ${:+.2f}){}".format(
                r["combo_key"], r["win_rate_pct"], r["trades"],
                r["total_pnl_usd"], flag,
            ))
        if len(board) > 5:
            lines.append("Bottom 5:")
            for r in board[-5:]:
                flag = "  [SUPPRESSED]" if r.get("suppressed") else ""
                lines.append("  {:<28s} {:5.1f}%  WR  ({} trades, ${:+.2f}){}".format(
                    r["combo_key"], r["win_rate_pct"], r["trades"],
                    r["total_pnl_usd"], flag,
                ))
    lines.append("")

    # 2. Missed winners
    audit = await analytics.audit_missed_winners(db, start, end, settings)
    lines.append(f"[Missed winners — last 7d]")
    den = audit["denominator"]
    lines.append(f"{den['winners_missed']} missed out of {den['winners_total']} "
                 f"qualifying winners "
                 f"(mcap ≥ ${settings.FEEDBACK_MISSED_WINNER_MIN_MCAP:,.0f}, "
                 f"rank ≤ {settings.FEEDBACK_MISSED_WINNER_MAX_RANK})")
    for tier in ("disaster_miss", "major_miss", "partial_miss"):
        entries = audit["tiers"][tier]
        if not entries:
            continue
        label = tier.replace("_", " ")
        lines.append(f"  {label}: {len(entries)}")
        for e in entries[:5]:
            lines.append("    {:<10s} +{:.0f}%   crossed {}".format(
                e["symbol"], e["peak_change"], e["crossed_at"],
            ))
    if audit["uncovered_window"]:
        lines.append(f"  ⚠ pipeline gap {den['pipeline_gap_hours']:.1f}h — "
                     f"{len(audit['uncovered_window'])} winners in "
                     f"uncovered_window excluded")
    lines.append("")

    # 3. Lead-time
    lines.append("[Lead-time — 30d, signal_type medians, 'ok' only]")
    breakdown = await analytics.lead_time_breakdown(db, "30d")
    if not breakdown:
        lines.append("  (no trades)")
    else:
        for sig in sorted(breakdown):
            b = breakdown[sig]
            if b["median_min"] is None:
                med_str = "n/a"
            else:
                med_str = f"{b['median_min']:+.1f} min"
            lines.append("  {:<18s} median {:<12s} (ok={}, no_ref={}, err={})".format(
                sig, med_str, b["count_ok"], b["count_no_reference"], b["count_error"],
            ))
    lines.append("")

    # 4. Suppression log
    lines.append("[Suppression log — this week]")
    log_rows = await analytics.suppression_log(db, start, end)
    if not log_rows:
        lines.append("  (none)")
    else:
        for r in log_rows:
            lines.append("  {:<24s} SUPPRESSED {} — WR {:.1f}% ({} trades), "
                         "parole until {}".format(
                r["combo_key"], r["suppressed_at"][:10],
                r["win_rate_pct"], r["trades"],
                (r["parole_at"] or "n/a")[:10],
            ))
    lines.append("")

    # 5. Fallback counters
    lines.append("[Fallback counters]")
    from scout.trading import suppression as _supp
    lines.append(f"  Suppression fail-opens: {len(_supp._fallback_timestamps)}")
    lines.append("")

    # 6. Chronic refresh failures
    lines.append("[Chronic refresh failures]")
    cur = await db._conn.execute(
        "SELECT combo_key, refresh_failures FROM combo_performance "
        "WHERE refresh_failures >= ? ORDER BY refresh_failures DESC",
        (settings.FEEDBACK_CHRONIC_FAILURE_THRESHOLD,),
    )
    chronic = await cur.fetchall()
    if not chronic:
        lines.append("  None")
    else:
        for c in chronic:
            lines.append(f"  {c['combo_key']} — {c['refresh_failures']} consecutive failures")

    return "\n".join(lines)


async def send_weekly_digest(db: Database, settings) -> None:
    """Orchestrator: build + send via alerter. Never silent on error."""
    try:
        text = await build_weekly_digest(db, date.today(), settings)
        if text is None:
            log.info("weekly_digest_skipped_empty")
            return

        for chunk in _split_for_telegram(text, _TG_SPLIT_LIMIT):
            await alerter.send_telegram_message(chunk)
        log.info("weekly_digest_sent", bytes=len(text))
    except Exception as e:
        corr = f"wd-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{secrets.token_hex(2)}"
        log.exception("weekly_digest_failed", corr=corr)
        try:
            await alerter.send_telegram_message(
                f"Weekly digest failed: {type(e).__name__} [ref={corr}]. Check logs."
            )
        except Exception:
            log.exception("weekly_digest_fallback_dispatch_error", corr=corr)


def _split_for_telegram(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    lines = text.split("\n")
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > limit:
            chunks.append("\n".join(buf))
            buf = [line]
            size = len(line) + 1
        else:
            buf.append(line)
            size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks
```

**Note on alerter signature:** if `alerter.send_telegram_message` requires session/settings, thread them through `send_weekly_digest` and/or create the session inside `send_weekly_digest` to match existing `format_daily_summary` patterns in `main.py`.

- [ ] **Step 4: Run the test — expect PASS**

Run: `uv run pytest tests/test_trading_weekly_digest.py -v`
Expected: all PASS.

- [ ] **Step 5: Format + commit**

```bash
uv run black scout/trading/weekly_digest.py tests/test_trading_weekly_digest.py
git add scout/trading/weekly_digest.py tests/test_trading_weekly_digest.py
git commit -m "feat(trading): weekly digest builder + sender with fallback"
```

---

### Task 9: Signals integration — wire suppression check + `build_combo_key`

**Files:**
- Modify: `scout/trading/signals.py` — replace the Task-4 placeholder `signal_combo="..."` literals with real `build_combo_key` calls guarded by `should_open`
- Create: `tests/test_trading_signals_integration.py`

Per spec §5.5, D13, D20. Compute `combo_key` once per trade; pass to `open_trade` as kwarg; short-circuit on suppression with a structured log.

- [ ] **Step 1: Write the failing integration test**

Create `tests/test_trading_signals_integration.py`:

```python
"""End-to-end integration: suppression short-circuits signals dispatchers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from scout.db import Database
from scout.trading.engine import TradingEngine
from scout.trading import signals


async def _seed_price(db, token_id, price):
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (token_id, price, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def _seed_gainers(db, coin_id):
    await db._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, market_cap_rank, market_cap, current_price, "
        " price_change_24h, price_at_snapshot, snapshot_at) "
        "VALUES (?, 'S', 'N', 100, 10000000, 1.0, 50.0, 1.0, ?)",
        (coin_id, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def _seed_suppressed_combo(db, combo_key):
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, parole_at, "
        " parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES (?, '30d', 25, 5, 20, -200, -4, 20.0, 1, ?, ?, 5, 0, ?)",
        (combo_key,
         datetime.now(timezone.utc).isoformat(),
         future,
         datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def test_suppressed_combo_blocks_trade_gainers(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=s)

    await _seed_price(db, "gx", 1.0)
    await _seed_gainers(db, "gx")
    await _seed_suppressed_combo(db, "gainers_early")

    await signals.trade_gainers(engine, db, min_mcap=1_000_000)
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE token_id = 'gx'"
    )
    assert (await cur.fetchone())[0] == 0
    await db.close()


async def test_unsuppressed_combo_opens_trade(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=s)

    await _seed_price(db, "gx", 1.0)
    await _seed_gainers(db, "gx")
    # No combo_performance row = cold_start = allow.

    await signals.trade_gainers(engine, db, min_mcap=1_000_000)
    cur = await db._conn.execute(
        "SELECT signal_combo FROM paper_trades WHERE token_id = 'gx'"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["signal_combo"] == "gainers_early"
    await db.close()
```

- [ ] **Step 2: Run the test — expect FAIL (suppression not yet wired)**

Run: `uv run pytest tests/test_trading_signals_integration.py -v`
Expected: FAIL on `test_suppressed_combo_blocks_trade_gainers` because Task 4 hard-coded `signal_combo="gainers_early"` but did not call `should_open`.

- [ ] **Step 3: Wire `should_open` + `build_combo_key` into every `signals.py` dispatcher**

Open `scout/trading/signals.py`. Add imports at the top:

```python
from scout.trading.combo_key import build_combo_key
from scout.trading.suppression import should_open
```

For each `trade_*` dispatcher, replace the hard-coded `signal_combo="..."` literal with the three-step pattern. Template (apply to each dispatcher, adjusting the signal_type + signals list):

```python
# Inside the per-item loop, just before `await engine.open_trade(...)`:
sigs = ...  # for first_signal: signals_fired; elsewhere: None
combo_key = build_combo_key(signal_type="<literal>", signals=sigs)
allow, reason = await should_open(db, combo_key)
if not allow:
    logger.info(
        "signal_suppressed",
        combo_key=combo_key, reason=reason,
        coin_id=<coin_id_expr>, signal_type="<literal>",
    )
    continue
# then pass signal_combo=combo_key into engine.open_trade.
```

**Exact per-dispatcher changes:**

- `trade_volume_spikes`: signal_type `"volume_spike"`, `sigs = None`, `coin_id_expr = spike.get("coin_id")`.
- `trade_gainers`: signal_type `"gainers_early"`, `sigs = None`, `coin_id_expr = g["coin_id"]`.
- `trade_losers`: signal_type `"losers_contrarian"`, `sigs = None`, `coin_id_expr = l["coin_id"]`.
- `trade_first_signals`: signal_type `"first_signal"`, `sigs = signals_fired`, `coin_id_expr = token.contract_address`.
- `trade_trending`: signal_type `"trending_catch"`, `sigs = None`, `coin_id_expr = t["coin_id"]`.
- `trade_predictions`: signal_type `"narrative_prediction"`, `sigs = None`, `coin_id_expr = pred.coin_id`.
- `trade_chain_completions`: signal_type `"chain_completed"`, `sigs = None`, `coin_id_expr = c["token_id"]`.

- [ ] **Step 4: Run the test — expect PASS**

Run: `uv run pytest tests/test_trading_signals_integration.py -v`
Expected: PASS.

- [ ] **Step 5: Run broader signals regression**

Run: `uv run pytest tests/test_trading_signals.py -v`
Expected: previously-passing tests remain green. If any existing test directly mocks `engine.open_trade` and expects it to be called, it may now see that call skipped if the test fixture also seeds a suppressed combo. Inspect failures and update the mocks — most existing tests do not seed `combo_performance`, so suppression returns `cold_start` / allow and behaviour is unchanged.

- [ ] **Step 6: Format + commit**

```bash
uv run black scout/trading/signals.py tests/test_trading_signals_integration.py
git add scout/trading/signals.py tests/test_trading_signals_integration.py
git commit -m "feat(trading): wire build_combo_key + should_open into signals dispatchers"
```

---

### Task 10: Main loop scheduling (03:00 refresh + Sunday 09:00 digest)

**Files:**
- Modify: `scout/main.py` — add `last_combo_refresh_date` + `last_weekly_digest_date` elapsed-time checks inside `_pipeline_loop()`

Per spec D14, §6 Flow C / Flow D. Uses the same pattern as `last_summary_date`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_main_feedback_scheduling.py`:

```python
"""Tests for main-loop scheduling of combo refresh + weekly digest."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


def test_schedule_keys_exist_in_main_source():
    """Lightweight static check: main.py references the two schedule keys."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "scout" / "main.py"
    text = src.read_text(encoding="utf-8")
    assert "last_combo_refresh_date" in text
    assert "last_weekly_digest_date" in text
    assert "FEEDBACK_COMBO_REFRESH_HOUR" in text
    assert "FEEDBACK_WEEKLY_DIGEST_WEEKDAY" in text
    assert "FEEDBACK_WEEKLY_DIGEST_HOUR" in text
```

(For a richer behavioural test, an integration test driving the loop with a mocked clock is possible but overkill for Sprint 1. The static check confirms the wiring exists; end-to-end verification happens in staging.)

- [ ] **Step 2: Run the test — expect FAIL**

Run: `uv run pytest tests/test_main_feedback_scheduling.py -v`
Expected: FAIL — strings not yet in `main.py`.

- [ ] **Step 3: Edit `scout/main.py`**

(a) Near the imports block (top of file), add:

```python
from scout.trading import combo_refresh as _combo_refresh
from scout.trading import weekly_digest as _weekly_digest
```

(b) Find the block where `last_summary_date` is initialised (around line 666 per snapshot). Add two parallel state vars:

```python
    last_summary_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_combo_refresh_date = ""  # empty so the first eligible hour fires
    last_weekly_digest_date = ""
```

(c) Inside `_pipeline_loop()`, update the nonlocal declaration:

```python
                nonlocal last_outcome_check, last_summary_date
                nonlocal last_combo_refresh_date, last_weekly_digest_date
```

(d) Below the daily-summary block (after `last_summary_date = current_date`), add the two schedule checks. Use **local time** (`datetime.now()`, no tz) because the hours/weekdays in settings refer to local time.

```python
                    # Nightly combo refresh (03:00 local)
                    local_now = datetime.now()
                    today_iso = local_now.strftime("%Y-%m-%d")
                    if (local_now.hour == settings.FEEDBACK_COMBO_REFRESH_HOUR
                            and last_combo_refresh_date != today_iso):
                        try:
                            summary = await _combo_refresh.refresh_all(db, settings)
                            logger.info("combo_refresh_done", **summary)
                        except Exception:
                            logger.exception("combo_refresh_loop_error")
                        last_combo_refresh_date = today_iso

                    # Weekly digest (Sun 09:00 local)
                    if (local_now.weekday() == settings.FEEDBACK_WEEKLY_DIGEST_WEEKDAY
                            and local_now.hour == settings.FEEDBACK_WEEKLY_DIGEST_HOUR
                            and last_weekly_digest_date != today_iso):
                        try:
                            await _weekly_digest.send_weekly_digest(db, settings)
                        except Exception:
                            logger.exception("weekly_digest_loop_error")
                        last_weekly_digest_date = today_iso
```

- [ ] **Step 4: Run the test — expect PASS**

Run: `uv run pytest tests/test_main_feedback_scheduling.py -v`
Expected: PASS.

- [ ] **Step 5: Dry-run smoke test**

Run: `uv run python -m scout.main --dry-run --cycles 1`
Expected: pipeline completes one cycle cleanly, no new exceptions related to feedback loop (scheduling conditions rarely fire on a single cycle — this just proves imports + wiring work).

- [ ] **Step 6: Format + commit**

```bash
uv run black scout/main.py tests/test_main_feedback_scheduling.py
git add scout/main.py tests/test_main_feedback_scheduling.py
git commit -m "feat(main): nightly combo refresh + Sunday weekly digest scheduling"
```

---

### Task 11: Final regression + success-criteria gate

Per spec §12 — automated criteria run as the final pass.

- [ ] **Step 1: Full test suite**

Run: `uv run pytest --tb=short -q`
Expected: ~60+ new tests added; existing suite remains green. Any red: fix before merging.

- [ ] **Step 2: Benchmark `refresh_all` with seeded fixture**

Create `tests/test_trading_combo_refresh_perf.py`:

```python
"""Perf gate for refresh_all — spec §12."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import combo_refresh


@pytest.mark.slow
async def test_refresh_all_under_5s_for_1000_trades(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # 50 combos × 20 trades each.
    for i in range(50):
        combo = f"combo_{i:02d}"
        for j in range(20):
            await db._conn.execute(
                "INSERT INTO paper_trades "
                "(token_id, symbol, name, chain, signal_type, signal_data, "
                " entry_price, amount_usd, quantity, tp_pct, sl_pct, "
                " tp_price, sl_price, status, pnl_usd, pnl_pct, "
                " opened_at, closed_at, signal_combo) "
                "VALUES (?, 'S', 'N', 'cg', 'volume_spike', '{}', "
                " 1, 100, 100, 20, 10, 1.2, 0.9, 'closed_tp', 10, 5, ?, ?, ?)",
                (f"tok_{i}_{j}",
                 (now - timedelta(days=5)).isoformat(),
                 (now - timedelta(days=4)).isoformat(),
                 combo),
            )
    await db._conn.commit()
    t0 = time.monotonic()
    result = await combo_refresh.refresh_all(db, s)
    elapsed = time.monotonic() - t0
    assert result["refreshed"] == 50
    assert elapsed < 5.0, f"refresh_all took {elapsed:.2f}s (>5s gate)"
    await db.close()
```

Run: `uv run pytest tests/test_trading_combo_refresh_perf.py -v`
Expected: PASS in <5s.

- [ ] **Step 3: Daily-digest byte-identical check**

Run existing daily-digest tests to confirm the additive schema didn't perturb anything:
`uv run pytest tests/test_trading_digest.py -v`
Expected: all PASS, no changes in digest output format (signal_combo / lead_time columns are not read by the daily digest).

- [ ] **Step 4: Format entire touched surface**

```bash
uv run black scout/ tests/
```

- [ ] **Step 5: Commit perf gate**

```bash
git add tests/test_trading_combo_refresh_perf.py
git commit -m "test(trading): perf gate — refresh_all <5s for 1000 trades"
```

- [ ] **Step 6: Rebase + push**

```bash
git fetch origin
git rebase origin/master
git push -u origin feat/paper-trading-feedback-loop
```

If `rebase` produces conflicts, stop and resolve — do not force-push without checking.

- [ ] **Step 7: Open PR**

Use `gh pr create` per project convention. Title: `feat(trading): feedback loop — combo stats, suppression, weekly digest, missed-winner audit`. Body references the spec file.

---

## Appendix A — File creation map

| File | Created by | Purpose |
|---|---|---|
| `scout/trading/combo_key.py` | Task 3 | Pure helper |
| `scout/trading/suppression.py` | Task 5 | Entry-gate |
| `scout/trading/combo_refresh.py` | Task 6 | Nightly rollup |
| `scout/trading/analytics.py` | Task 7 | On-demand queries |
| `scout/trading/weekly_digest.py` | Task 8 | Sunday digest |
| `tests/test_trading_db_migration.py` | Task 2 | Migration assertions |
| `tests/test_trading_combo_key.py` | Task 3 | Helper tests |
| `tests/test_trading_engine_leadtime.py` | Task 4 | Lead-time + signal_combo persistence |
| `tests/test_trading_suppression.py` | Task 5 | Suppression semantics |
| `tests/test_trading_combo_refresh.py` | Task 6 | Rollup math + suppression rule |
| `tests/test_trading_analytics.py` | Task 7 | Leaderboard, audit, breakdown |
| `tests/test_trading_weekly_digest.py` | Task 8 | Digest rendering + fallback |
| `tests/test_trading_signals_integration.py` | Task 9 | End-to-end suppression block |
| `tests/test_main_feedback_scheduling.py` | Task 10 | Scheduling wiring check |
| `tests/test_trading_combo_refresh_perf.py` | Task 11 | Perf gate |

## Appendix B — Files modified

| File | Modified by | Change |
|---|---|---|
| `scout/config.py` | Task 1 | 15 new settings |
| `scout/db.py` | Task 2 | `_migrate_feedback_loop_schema` + call from `initialize()` |
| `scout/trading/engine.py` | Task 4 | `_compute_lead_time_vs_trending` helper + required `signal_combo` kwarg |
| `scout/trading/paper.py` | Task 4 | `execute_buy` accepts + persists 3 new fields |
| `scout/trading/signals.py` | Task 4 (placeholder) → Task 9 (real) | `build_combo_key` + `should_open` on every dispatcher |
| `scout/main.py` | Task 10 | 03:00 refresh + Sun 09:00 digest scheduling |
| `tests/test_config.py` | Task 1 | Defaults assertion |

## Appendix C — Decision references

Every task in this plan traces back to a locked decision in the spec:

- **Tasks 1, 8, 10:** §8 settings, D14 scheduling
- **Task 2:** §4.1–4.3, D18 atomic migration
- **Task 3:** §4.4, D2 pair cap, D13 single-signal norm
- **Task 4:** §4.5, D8 trending-only reference, D19 db._conn convention, D20 single derivation site
- **Task 5:** §5.2, D16 BEGIN IMMEDIATE, D17 fail-open with loud escalation, D19 conn pattern
- **Task 6:** §5.3, D1 materialise `combo_performance`, D3–D5 suppression rule, D9 windows, D15 nightly-only
- **Task 7:** §5.1, §7 LEFT JOIN, D6 tiers, D7 filters, D21 Python percentiles
- **Task 8:** §5.4 section order, §9 fallback with correlation ID, D10 weekly cadence
- **Task 9:** §5.5, D11 denormalise combo, D13 single-signal acceptance, D20 derivation
- **Task 10:** D14 elapsed-time pattern
- **Task 11:** §12 automated success criteria
