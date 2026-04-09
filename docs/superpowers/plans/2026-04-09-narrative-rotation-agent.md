# Narrative Rotation Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an autonomous agent that detects accelerating crypto narratives on CoinGecko, picks undervalued tokens within hot categories, tracks outcomes, and self-adjusts its strategy — all running as a parallel module inside gecko-alpha.

**Architecture:** New `scout/narrative/` package runs alongside the existing pipeline via `asyncio.gather()`. Five-phase loop: OBSERVE → PREDICT → ALERT → EVALUATE → LEARN. Self-improving via agent_strategy table. Dashboard integration via new API endpoints + React tab.

**Tech Stack:** Python 3.12, aiohttp, aiosqlite, anthropic SDK, Pydantic v2, structlog, pytest + aioresponses, React + Vite (dashboard)

**Spec:** `docs/superpowers/specs/2026-04-09-narrative-rotation-agent-design.md`

---

## File Map

### New files (create)
| File | Responsibility |
|------|---------------|
| `scout/narrative/__init__.py` | Package init |
| `scout/narrative/models.py` | Pydantic models: CategorySnapshot, CategoryAcceleration, LaggardToken, NarrativePrediction, NarrativeSignal, StrategyValue, LearnLog |
| `scout/narrative/strategy.py` | Read/write agent_strategy table, defaults seeding, bounds enforcement, locked key protection, timestamp persistence |
| `scout/narrative/observer.py` | CoinGecko /coins/categories polling, snapshot storage, acceleration detection, market regime, survivorship bias |
| `scout/narrative/predictor.py` | Laggard selection, Claude narrative-fit scoring, control picks, A/B holdout routing, dedup/cooldown, alerts |
| `scout/narrative/evaluator.py` | Multi-checkpoint (6h/24h/48h) outcome evaluation, peak tracking, batch price fetching, classification |
| `scout/narrative/learner.py` | Daily reflection, weekly consolidation, strategy updates, circuit breaker, holdout comparison |
| `scout/narrative/prompts.py` | Static base prompts for scoring + reflection + consolidation |
| `scout/narrative/digest.py` | Daily/weekly Telegram summary builder |
| `tests/test_narrative_models.py` | Model validation tests |
| `tests/test_narrative_strategy.py` | Strategy CRUD, bounds, locks, defaults tests |
| `tests/test_narrative_observer.py` | Category polling, acceleration math, regime, survivorship tests |
| `tests/test_narrative_predictor.py` | Laggard filtering, scoring, control picks, dedup tests |
| `tests/test_narrative_evaluator.py` | Checkpoint eval, peak tracking, classification, batch fetch tests |
| `tests/test_narrative_learner.py` | Reflection parsing, strategy updates, bounds, circuit breaker, holdout tests |

### Modified files
| File | Changes |
|------|---------|
| `scout/config.py` | Add 9 NARRATIVE_* config fields |
| `scout/db.py` | Add 5 new tables to `_create_tables()` |
| `scout/main.py` | Add `narrative_agent_loop` to `asyncio.gather()` |
| `dashboard/api.py` | Add 7 narrative API endpoints |
| `dashboard/frontend/App.jsx` | Add Narrative tab |
| `.env.example` | Add NARRATIVE_* env vars |

---

## Task 1: Models + Config

**Files:**
- Create: `scout/narrative/__init__.py`
- Create: `scout/narrative/models.py`
- Modify: `scout/config.py`
- Modify: `.env.example`
- Test: `tests/test_narrative_models.py`

- [ ] **Step 1: Write failing tests for models**

```python
# tests/test_narrative_models.py
"""Tests for narrative rotation agent models."""
from datetime import datetime, timezone

from scout.narrative.models import (
    CategoryAcceleration,
    CategorySnapshot,
    LaggardToken,
    LearnLog,
    NarrativePrediction,
    NarrativeSignal,
    StrategyValue,
)


def test_category_snapshot_required_fields():
    snap = CategorySnapshot(
        category_id="ai",
        name="Artificial Intelligence",
        market_cap=1e9,
        market_cap_change_24h=5.2,
        volume_24h=1e8,
        coin_count=None,
        snapshot_at=datetime.now(timezone.utc),
    )
    assert snap.category_id == "ai"
    assert snap.coin_count is None  # optional per R1


def test_category_acceleration_is_heating():
    accel = CategoryAcceleration(
        category_id="ai",
        name="AI",
        current_velocity=12.0,
        previous_velocity=5.0,
        acceleration=7.0,
        volume_growth_pct=15.0,
        coin_count_change=-2,
        is_heating=True,
    )
    assert accel.is_heating is True
    assert accel.acceleration == 7.0


def test_narrative_prediction_defaults():
    pred = NarrativePrediction(
        category_id="ai",
        category_name="AI",
        coin_id="token1",
        symbol="TKN",
        name="Token One",
        market_cap_at_prediction=50e6,
        price_at_prediction=1.23,
        narrative_fit_score=75,
        staying_power="High",
        confidence="Medium",
        reasoning="Strong narrative fit",
        market_regime="BULL",
        trigger_count=3,
        strategy_snapshot={"hit_threshold_pct": 15.0},
        predicted_at=datetime.now(timezone.utc),
    )
    assert pred.is_control is False
    assert pred.is_holdout is False
    assert pred.outcome_class is None
    assert pred.peak_price is None


def test_narrative_prediction_control_pick():
    pred = NarrativePrediction(
        category_id="ai",
        category_name="AI",
        coin_id="ctrl1",
        symbol="CTL",
        name="Control",
        market_cap_at_prediction=30e6,
        price_at_prediction=0.5,
        narrative_fit_score=0,
        staying_power="",
        confidence="CONTROL",
        reasoning="",
        market_regime="CRAB",
        trigger_count=1,
        is_control=True,
        strategy_snapshot={},
        predicted_at=datetime.now(timezone.utc),
    )
    assert pred.is_control is True
    assert pred.confidence == "CONTROL"


def test_strategy_value_model():
    sv = StrategyValue(
        key="hit_threshold_pct",
        value="15.0",
        updated_at=datetime.now(timezone.utc),
        updated_by="init",
        reason="Initial default",
        locked=False,
        min_bound=5.0,
        max_bound=50.0,
    )
    assert sv.locked is False


def test_narrative_signal_trigger_count():
    sig = NarrativeSignal(
        category_id="ai",
        category_name="AI",
        acceleration=7.5,
        volume_growth_pct=20.0,
        coin_count_change=-1,
        trigger_count=4,
        detected_at=datetime.now(timezone.utc),
        cooling_down_until=datetime.now(timezone.utc),
    )
    assert sig.trigger_count == 4


def test_learn_log_model():
    log = LearnLog(
        cycle_number=1,
        cycle_type="daily",
        reflection_text="Test reflection",
        changes_made={"hit_threshold_pct": {"old": 15.0, "new": 12.0}},
        hit_rate_before=30.0,
        created_at=datetime.now(timezone.utc),
    )
    assert log.hit_rate_after is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_narrative_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scout.narrative'`

- [ ] **Step 3: Create package and models**

```python
# scout/narrative/__init__.py
"""Narrative Rotation Agent — autonomous category momentum detection."""
```

```python
# scout/narrative/models.py
"""Pydantic models for the Narrative Rotation Agent."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CategorySnapshot(BaseModel):
    category_id: str
    name: str
    market_cap: float
    market_cap_change_24h: float
    volume_24h: float
    coin_count: int | None = None
    market_regime: str | None = None
    snapshot_at: datetime


class CategoryAcceleration(BaseModel):
    category_id: str
    name: str
    current_velocity: float
    previous_velocity: float
    acceleration: float
    volume_24h: float              # absolute volume for prompt display
    volume_growth_pct: float
    coin_count_change: int | None = None
    is_heating: bool


class LaggardToken(BaseModel):
    coin_id: str
    symbol: str
    name: str
    market_cap: float
    price: float
    price_change_24h: float
    volume_24h: float
    category_id: str
    category_name: str


class NarrativePrediction(BaseModel):
    id: int | None = None
    category_id: str
    category_name: str
    coin_id: str
    symbol: str
    name: str
    market_cap_at_prediction: float
    price_at_prediction: float
    narrative_fit_score: int
    staying_power: str
    confidence: str
    reasoning: str
    market_regime: str
    trigger_count: int
    is_control: bool = False
    is_holdout: bool = False
    strategy_snapshot: dict
    strategy_snapshot_ab: dict | None = None
    predicted_at: datetime
    outcome_6h_price: float | None = None
    outcome_6h_change_pct: float | None = None
    outcome_6h_class: str | None = None
    outcome_24h_price: float | None = None
    outcome_24h_change_pct: float | None = None
    outcome_24h_class: str | None = None
    outcome_48h_price: float | None = None
    outcome_48h_change_pct: float | None = None
    outcome_48h_class: str | None = None
    peak_price: float | None = None
    peak_change_pct: float | None = None
    peak_at: datetime | None = None
    outcome_class: str | None = None
    outcome_reason: str | None = None
    evaluated_at: datetime | None = None


class NarrativeSignal(BaseModel):
    id: int | None = None
    category_id: str
    category_name: str
    acceleration: float
    volume_growth_pct: float
    coin_count_change: int | None = None
    trigger_count: int = 1
    detected_at: datetime
    cooling_down_until: datetime


class StrategyValue(BaseModel):
    key: str
    value: str
    updated_at: datetime
    updated_by: str
    reason: str
    locked: bool = False
    min_bound: float | None = None
    max_bound: float | None = None


class LearnLog(BaseModel):
    id: int | None = None
    cycle_number: int
    cycle_type: str
    reflection_text: str
    changes_made: dict
    hit_rate_before: float
    hit_rate_after: float | None = None
    created_at: datetime
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_narrative_models.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Add config fields to Settings**

Add to `scout/config.py` inside the `Settings` class, after the existing `ANTHROPIC_API_KEY` field:

```python
    # Narrative Rotation Agent
    NARRATIVE_POLL_INTERVAL: int = 1800
    NARRATIVE_EVAL_INTERVAL: int = 21600
    NARRATIVE_DIGEST_HOUR_UTC: int = 0
    NARRATIVE_LEARN_HOUR_UTC: int = 1
    NARRATIVE_WEEKLY_LEARN_DAY: int = 6
    NARRATIVE_ENABLED: bool = False
    NARRATIVE_SNAPSHOT_RETENTION_DAYS: int = 7
    NARRATIVE_SCORING_MODEL: str = "claude-haiku-4-5"
    NARRATIVE_LEARN_MODEL: str = "claude-sonnet-4-6"
```

Add to `.env.example` at the bottom:

```
# === Narrative Rotation Agent ===
NARRATIVE_ENABLED=false             # Set true to enable narrative agent
NARRATIVE_POLL_INTERVAL=1800        # 30 min observe cycle
NARRATIVE_EVAL_INTERVAL=21600       # 6 hour eval cycle
NARRATIVE_SCORING_MODEL=claude-haiku-4-5
NARRATIVE_LEARN_MODEL=claude-sonnet-4-6
```

- [ ] **Step 6: Run full test suite to verify no regressions**

Run: `uv run pytest --tb=short -q`
Expected: All existing tests + 7 new tests PASS

- [ ] **Step 7: Commit**

```bash
git add scout/narrative/__init__.py scout/narrative/models.py scout/config.py .env.example tests/test_narrative_models.py
git commit -m "feat(narrative): add Pydantic models and config for narrative rotation agent"
```

---

## Task 2: Database Tables

**Files:**
- Modify: `scout/db.py`
- Test: `tests/test_narrative_db.py` (create)

- [ ] **Step 1: Write failing test for table creation**

```python
# tests/test_narrative_db.py
"""Tests for narrative rotation agent database tables."""
import json
from datetime import datetime, timezone

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def test_narrative_tables_created(db):
    """All 5 narrative tables exist after initialize."""
    tables = []
    async with db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cursor:
        async for row in cursor:
            tables.append(row[0])
    for t in [
        "category_snapshots",
        "narrative_signals",
        "predictions",
        "agent_strategy",
        "learn_logs",
    ]:
        assert t in tables, f"Table {t} not found"


async def test_insert_category_snapshot(db):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO category_snapshots
           (category_id, name, market_cap, market_cap_change_24h, volume_24h,
            coin_count, market_regime, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("ai", "AI", 1e9, 5.2, 1e8, 150, "BULL", now),
    )
    await db._conn.commit()
    async with db._conn.execute(
        "SELECT * FROM category_snapshots WHERE category_id='ai'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["market_regime"] == "BULL"


async def test_insert_prediction_unique_constraint(db):
    now = datetime.now(timezone.utc).isoformat()
    vals = ("ai", "AI", "coin1", "TKN", "Token", 50e6, 1.0, 75,
            "High", "Medium", "reason", "BULL", 1, 0, 0,
            json.dumps({}), None, now)
    cols = """(category_id, category_name, coin_id, symbol, name,
              market_cap_at_prediction, price_at_prediction, narrative_fit_score,
              staying_power, confidence, reasoning, market_regime, trigger_count,
              is_control, is_holdout, strategy_snapshot, strategy_snapshot_ab,
              predicted_at)"""
    placeholders = ",".join(["?"] * len(vals))
    await db._conn.execute(
        f"INSERT INTO predictions {cols} VALUES ({placeholders})", vals
    )
    await db._conn.commit()
    # Duplicate should fail
    with pytest.raises(Exception):
        await db._conn.execute(
            f"INSERT INTO predictions {cols} VALUES ({placeholders})", vals
        )


async def test_insert_agent_strategy(db):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO agent_strategy (key, value, updated_at, updated_by, reason)
           VALUES (?, ?, ?, ?, ?)""",
        ("hit_threshold_pct", "15.0", now, "init", "default"),
    )
    await db._conn.commit()
    async with db._conn.execute(
        "SELECT * FROM agent_strategy WHERE key='hit_threshold_pct'"
    ) as cur:
        row = await cur.fetchone()
    assert row["value"] == "15.0"
    assert row["locked"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_narrative_db.py -v`
Expected: FAIL — tables don't exist

- [ ] **Step 3: Add narrative tables to db.py**

Add the following SQL to the end of the `_create_tables` method's `executescript` string in `scout/db.py`, before the closing `"""`):

```sql
            CREATE TABLE IF NOT EXISTS category_snapshots (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id           TEXT NOT NULL,
                name                  TEXT NOT NULL,
                market_cap            REAL,
                market_cap_change_24h REAL,
                volume_24h            REAL,
                coin_count            INTEGER,
                market_regime         TEXT,
                snapshot_at           TEXT NOT NULL,
                created_at            TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_cat_snap_category
                ON category_snapshots(category_id, snapshot_at);

            CREATE TABLE IF NOT EXISTS narrative_signals (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id       TEXT NOT NULL,
                category_name     TEXT NOT NULL,
                acceleration      REAL NOT NULL,
                volume_growth_pct REAL NOT NULL,
                coin_count_change INTEGER,
                trigger_count     INTEGER DEFAULT 1,
                detected_at       TEXT NOT NULL,
                cooling_down_until TEXT NOT NULL,
                created_at        TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_narr_sig_category
                ON narrative_signals(category_id, cooling_down_until);

            CREATE TABLE IF NOT EXISTS predictions (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id             TEXT NOT NULL,
                category_name           TEXT NOT NULL,
                coin_id                 TEXT NOT NULL,
                symbol                  TEXT NOT NULL,
                name                    TEXT NOT NULL,
                market_cap_at_prediction REAL NOT NULL,
                price_at_prediction     REAL NOT NULL,
                narrative_fit_score     INTEGER NOT NULL,
                staying_power           TEXT NOT NULL,
                confidence              TEXT NOT NULL,
                reasoning               TEXT NOT NULL,
                market_regime           TEXT,
                trigger_count           INTEGER,
                is_control              INTEGER DEFAULT 0,
                is_holdout              INTEGER DEFAULT 0,
                strategy_snapshot       TEXT NOT NULL,
                strategy_snapshot_ab    TEXT,
                predicted_at            TEXT NOT NULL,
                outcome_6h_price        REAL,
                outcome_6h_change_pct   REAL,
                outcome_6h_class        TEXT,
                outcome_24h_price       REAL,
                outcome_24h_change_pct  REAL,
                outcome_24h_class       TEXT,
                outcome_48h_price       REAL,
                outcome_48h_change_pct  REAL,
                outcome_48h_class       TEXT,
                peak_price              REAL,
                peak_change_pct         REAL,
                peak_at                 TEXT,
                outcome_class           TEXT,
                outcome_reason          TEXT,
                eval_retry_count        INTEGER DEFAULT 0,
                evaluated_at            TEXT,
                created_at              TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(category_id, coin_id, predicted_at)
            );
            CREATE INDEX IF NOT EXISTS idx_pred_category
                ON predictions(category_id);
            CREATE INDEX IF NOT EXISTS idx_pred_predicted
                ON predictions(predicted_at);
            CREATE INDEX IF NOT EXISTS idx_pred_outcome
                ON predictions(outcome_class);

            CREATE TABLE IF NOT EXISTS agent_strategy (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                reason     TEXT,
                locked     INTEGER DEFAULT 0,
                min_bound  REAL,
                max_bound  REAL
            );

            CREATE TABLE IF NOT EXISTS learn_logs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_number     INTEGER NOT NULL,
                cycle_type       TEXT NOT NULL,
                reflection_text  TEXT NOT NULL,
                changes_made     TEXT NOT NULL,
                hit_rate_before  REAL,
                hit_rate_after   REAL,
                created_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_narrative_db.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
git add scout/db.py tests/test_narrative_db.py
git commit -m "feat(narrative): add 5 database tables for narrative rotation agent"
```

---

## Task 3: Strategy Manager

**Files:**
- Create: `scout/narrative/strategy.py`
- Test: `tests/test_narrative_strategy.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_narrative_strategy.py
"""Tests for narrative agent strategy manager."""
import json
from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.narrative.strategy import STRATEGY_DEFAULTS, STRATEGY_BOUNDS, Strategy


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
async def strategy(db):
    s = Strategy(db)
    await s.load_or_init()
    return s


async def test_init_seeds_defaults(strategy):
    val = await strategy.get("hit_threshold_pct")
    assert val == 15.0


async def test_get_returns_typed_value(strategy):
    val = await strategy.get("category_accel_threshold")
    assert isinstance(val, float)
    assert val == 5.0


async def test_set_respects_bounds(strategy):
    with pytest.raises(ValueError, match="out of bounds"):
        await strategy.set("hit_threshold_pct", 999.0, "learn_cycle_1", "test")


async def test_set_within_bounds(strategy):
    await strategy.set("hit_threshold_pct", 20.0, "learn_cycle_1", "testing")
    val = await strategy.get("hit_threshold_pct")
    assert val == 20.0


async def test_locked_key_cannot_be_changed(strategy):
    await strategy.lock("hit_threshold_pct")
    with pytest.raises(ValueError, match="locked"):
        await strategy.set("hit_threshold_pct", 20.0, "learn_cycle_1", "test")


async def test_unlock_allows_change(strategy):
    await strategy.lock("hit_threshold_pct")
    await strategy.unlock("hit_threshold_pct")
    await strategy.set("hit_threshold_pct", 20.0, "learn_cycle_1", "test")
    assert await strategy.get("hit_threshold_pct") == 20.0


async def test_get_timestamp_default(strategy):
    ts = await strategy.get_timestamp("last_eval_at", datetime.min)
    assert ts == datetime.min


async def test_set_and_get_timestamp(strategy):
    now = datetime.now(timezone.utc)
    await strategy.set_timestamp("last_eval_at", now)
    ts = await strategy.get_timestamp("last_eval_at", datetime.min)
    assert abs((ts - now).total_seconds()) < 1


async def test_get_all_returns_dict(strategy):
    all_vals = await strategy.get_all()
    assert "hit_threshold_pct" in all_vals
    assert "category_accel_threshold" in all_vals


async def test_unbounded_key_accepts_any_value(strategy):
    await strategy.set("lessons_learned", "test lesson", "learn_cycle_1", "test")
    val = await strategy.get("lessons_learned")
    assert val == "test lesson"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_narrative_strategy.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement strategy manager**

```python
# scout/narrative/strategy.py
"""Agent strategy manager — read/write agent_strategy table with bounds and locks."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from scout.db import Database

logger = structlog.get_logger()

STRATEGY_DEFAULTS: dict[str, Any] = {
    "category_accel_threshold": 5.0,
    "category_volume_growth_min": 10.0,
    "laggard_max_mcap": 200_000_000,
    "laggard_max_change": 10.0,
    "laggard_min_change": -20.0,
    "laggard_min_volume": 100_000,
    "max_picks_per_category": 5,
    "hit_threshold_pct": 15.0,
    "miss_threshold_pct": -10.0,
    "signal_cooldown_hours": 4,
    "max_heating_per_cycle": 5,
    "min_learn_sample": 100,
    "min_trigger_count": 1,
    "lessons_learned": "",
    "lessons_version": 0,
    "narrative_alert_enabled": True,
}

STRATEGY_BOUNDS: dict[str, tuple[float, float]] = {
    "category_accel_threshold": (2.0, 15.0),
    "category_volume_growth_min": (5.0, 50.0),
    "laggard_max_mcap": (50_000_000, 1_000_000_000),
    "laggard_max_change": (5.0, 30.0),
    "laggard_min_change": (-50.0, 0.0),
    "laggard_min_volume": (10_000, 1_000_000),
    "hit_threshold_pct": (5.0, 50.0),
    "miss_threshold_pct": (-30.0, -5.0),
    "max_picks_per_category": (3, 10),
    "max_heating_per_cycle": (1, 10),
    "signal_cooldown_hours": (1, 12),
    "min_learn_sample": (50, 500),
    "min_trigger_count": (1, 10),
}


class Strategy:
    """Reads and writes agent_strategy rows with bounds enforcement."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._cache: dict[str, Any] = {}

    async def load_or_init(self) -> None:
        """Load existing strategy or seed defaults."""
        conn = self._db._conn
        async with conn.execute("SELECT key, value FROM agent_strategy") as cur:
            rows = await cur.fetchall()

        existing_keys = set()
        for row in rows:
            key = row["key"]
            existing_keys.add(key)
            self._cache[key] = json.loads(row["value"])

        now = datetime.now(timezone.utc).isoformat()
        for key, default in STRATEGY_DEFAULTS.items():
            if key not in existing_keys:
                lo, hi = STRATEGY_BOUNDS.get(key, (None, None))
                await conn.execute(
                    """INSERT INTO agent_strategy
                       (key, value, updated_at, updated_by, reason, locked, min_bound, max_bound)
                       VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                    (key, json.dumps(default), now, "init", "Initial default", lo, hi),
                )
                self._cache[key] = default
        await conn.commit()

    async def get(self, key: str) -> Any:
        """Get a strategy value. Returns typed Python value."""
        if key in self._cache:
            return self._cache[key]
        conn = self._db._conn
        async with conn.execute(
            "SELECT value FROM agent_strategy WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise KeyError(f"Strategy key not found: {key}")
        val = json.loads(row["value"])
        self._cache[key] = val
        return val

    async def set(
        self, key: str, value: Any, updated_by: str, reason: str
    ) -> None:
        """Set a strategy value with bounds and lock enforcement."""
        conn = self._db._conn
        async with conn.execute(
            "SELECT locked, min_bound, max_bound FROM agent_strategy WHERE key = ?",
            (key,),
        ) as cur:
            row = await cur.fetchone()

        if row is not None and row["locked"]:
            raise ValueError(f"Strategy key '{key}' is locked")

        if key in STRATEGY_BOUNDS and isinstance(value, (int, float)):
            lo, hi = STRATEGY_BOUNDS[key]
            if value < lo or value > hi:
                raise ValueError(
                    f"Value {value} for '{key}' out of bounds [{lo}, {hi}]"
                )

        now = datetime.now(timezone.utc).isoformat()
        serialized = json.dumps(value)
        if row is not None:
            await conn.execute(
                """UPDATE agent_strategy
                   SET value = ?, updated_at = ?, updated_by = ?, reason = ?
                   WHERE key = ?""",
                (serialized, now, updated_by, reason, key),
            )
        else:
            lo, hi = STRATEGY_BOUNDS.get(key, (None, None))
            await conn.execute(
                """INSERT INTO agent_strategy
                   (key, value, updated_at, updated_by, reason, locked, min_bound, max_bound)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                (key, serialized, now, updated_by, reason, lo, hi),
            )
        await conn.commit()
        self._cache[key] = value

    async def lock(self, key: str) -> None:
        conn = self._db._conn
        await conn.execute(
            "UPDATE agent_strategy SET locked = 1 WHERE key = ?", (key,)
        )
        await conn.commit()

    async def unlock(self, key: str) -> None:
        conn = self._db._conn
        await conn.execute(
            "UPDATE agent_strategy SET locked = 0 WHERE key = ?", (key,)
        )
        await conn.commit()

    async def get_timestamp(self, key: str, default: datetime) -> datetime:
        try:
            val = await self.get(key)
            if isinstance(val, str) and val:
                return datetime.fromisoformat(val)
            return default
        except KeyError:
            return default

    async def set_timestamp(self, key: str, value: datetime) -> None:
        await self.set(key, value.isoformat(), "system", "scheduling timestamp")

    async def get_all(self) -> dict[str, Any]:
        conn = self._db._conn
        async with conn.execute("SELECT key, value FROM agent_strategy") as cur:
            rows = await cur.fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_narrative_strategy.py -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scout/narrative/strategy.py tests/test_narrative_strategy.py
git commit -m "feat(narrative): add strategy manager with bounds, locks, and defaults"
```

---

## Task 4: Prompts (static base prompts)

**Files:**
- Create: `scout/narrative/prompts.py`

- [ ] **Step 1: Create prompts module**

```python
# scout/narrative/prompts.py
"""Static base prompts for narrative rotation agent. Never modified by the agent."""

NARRATIVE_FIT_SYSTEM = (
    "You are a crypto narrative analyst evaluating whether specific tokens "
    "fit an accelerating category trend. Score objectively based on data provided. "
    "Return ONLY valid JSON with no other text."
)

NARRATIVE_FIT_TEMPLATE = """\
Category "{category_name}" is accelerating: market cap {mcap_change}% in 24h \
(acceleration: {acceleration}%), volume ${volume:,.0f} (+{vol_growth}% in 6h).
Category leaders: {top_3_coins}.

Evaluate {token_name} ({symbol}, ${market_cap:,.0f} mcap, {price_change_24h:+.1f}% 24h):
Objective data: market regime: {market_regime}, \
category coin count change: {coin_count_change}, token volume/mcap ratio: {vol_mcap_ratio:.2f}.

1. Does this token genuinely belong to the {category_name} narrative?
2. Given the objective data above, is the volume/price trend consistent with genuine accumulation?
3. Cultural staying power: is this narrative a 1-day catalyst or multi-week trend?
4. Risk factors: any red flags in the data?

{lessons_appendix}\
Return ONLY JSON:
{{"narrative_fit": <int 0-100>, "staying_power": "<Low|Medium|High>", \
"confidence": "<Low|Medium|High>", "reasoning": "<2-3 sentences>"}}"""

DAILY_REFLECTION_TEMPLATE = """\
You are the strategy advisor for a crypto narrative rotation agent.
Review these predictions and their outcomes.

PREDICTIONS AND OUTCOMES (last {sample_size}):
{predictions_json}

CONTROL BASELINE: {control_hit_rate:.1f}% (random picks from same pool)
AGENT HIT RATE: {agent_hit_rate:.1f}%
TRUE ALPHA: {true_alpha:.1f}% (target: >10pp above baseline)

CURRENT STRATEGY:
{strategy_json}

MARKET REGIME BREAKDOWN:
{regime_breakdown}

Analyze:
1. Which categories produced the most HITs vs MISSes?
2. Did narrative_fit_score correlate with outcomes?
3. Are thresholds too tight or too loose?
4. Timing: do 6h outcomes differ from 48h? What's peak vs 48h?
5. Does trigger_count correlate with better outcomes?
6. Market regime: should thresholds differ in BULL vs BEAR vs CRAB?
7. Survivorship: did categories with negative coin_count_change produce more MISSes?

Suggest 0-3 strategy adjustments:
{{"key": "<strategy_key>", "new_value": <value>, "reason": "<citing data>"}}

IMPORTANT: Only suggest changes supported by data. "No changes" is valid.
Return JSON: {{"adjustments": [...], "reflection": "<3-5 sentences>", \
"true_alpha": <float>, "regime_insight": "<1 sentence>"}}"""

WEEKLY_CONSOLIDATION_TEMPLATE = """\
Here are the lessons appended to the narrative scoring prompt:
{current_lessons}

This week's daily reflections:
{weekly_reflections}

CONTRARIAN CHECK: Do not validate your own prior reasoning.
For each lesson, check hit rate BEFORE and AFTER introduction:
{hit_rate_per_lesson}
If a lesson did not improve hit rate by >3pp, REMOVE it.

Consolidate into max 10 lessons. Remove:
- Lessons where hit rate did not improve (data-driven)
- Contradictory lessons (keep one with better hit rate)
- Redundant lessons (merge)

Return JSON: {{"consolidated_lessons": "<max 10 bullet points>", \
"lessons_version": {next_version}, \
"removed": [{{"lesson": "<text>", "reason": "<why>", \
"hit_rate_before": 0, "hit_rate_after": 0}}]}}"""
```

- [ ] **Step 2: Commit**

```bash
git add scout/narrative/prompts.py
git commit -m "feat(narrative): add static base prompts for scoring, reflection, consolidation"
```

---

## Task 5: Observer (OBSERVE phase)

**Files:**
- Create: `scout/narrative/observer.py`
- Test: `tests/test_narrative_observer.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_narrative_observer.py
"""Tests for narrative OBSERVE phase."""
from datetime import datetime, timezone, timedelta

import pytest
from aioresponses import aioresponses
import aiohttp

from scout.db import Database
from scout.narrative.observer import (
    fetch_categories,
    parse_category_response,
    compute_acceleration,
    detect_market_regime,
    store_snapshot,
)
from scout.narrative.models import CategorySnapshot
from scout.narrative.strategy import Strategy


SAMPLE_CG_RESPONSE = [
    {
        "id": "artificial-intelligence",
        "name": "Artificial Intelligence",
        "market_cap": 5e10,
        "market_cap_change_24h": 8.5,
        "volume_24h": 2e9,
        "top_3_coins_id": ["fetch-ai", "render", "ocean-protocol"],
        "top_3_coins": [],
        "updated_at": "2026-04-09T00:00:00Z",
    },
    {
        "id": "meme-token",
        "name": "Meme",
        "market_cap": 3e10,
        "market_cap_change_24h": -2.1,
        "volume_24h": 1e9,
        "top_3_coins_id": ["doge", "shib", "pepe"],
        "top_3_coins": [],
        "updated_at": "2026-04-09T00:00:00Z",
    },
]


def test_parse_category_response_valid():
    snapshots = parse_category_response(SAMPLE_CG_RESPONSE, "BULL")
    assert len(snapshots) == 2
    assert snapshots[0].category_id == "artificial-intelligence"
    assert snapshots[0].market_regime == "BULL"


def test_parse_category_response_skips_null_fields():
    data = [{"id": "bad", "name": "Bad", "market_cap": None,
             "market_cap_change_24h": None, "volume_24h": None}]
    snapshots = parse_category_response(data, "CRAB")
    assert len(snapshots) == 0


def test_compute_acceleration_heating():
    now_snaps = [CategorySnapshot(
        category_id="ai", name="AI", market_cap=5e10,
        market_cap_change_24h=12.0, volume_24h=3e9,
        coin_count=100, snapshot_at=datetime.now(timezone.utc),
    )]
    old_snaps = [CategorySnapshot(
        category_id="ai", name="AI", market_cap=4e10,
        market_cap_change_24h=4.0, volume_24h=2e9,
        coin_count=102, snapshot_at=datetime.now(timezone.utc) - timedelta(hours=6),
    )]
    results = compute_acceleration(now_snaps, old_snaps, accel_threshold=5.0, vol_threshold=10.0)
    assert len(results) == 1
    assert results[0].is_heating is True
    assert results[0].acceleration == 8.0
    assert results[0].coin_count_change == -2


def test_compute_acceleration_not_heating():
    now_snaps = [CategorySnapshot(
        category_id="ai", name="AI", market_cap=5e10,
        market_cap_change_24h=5.0, volume_24h=2.05e9,
        coin_count=100, snapshot_at=datetime.now(timezone.utc),
    )]
    old_snaps = [CategorySnapshot(
        category_id="ai", name="AI", market_cap=4e10,
        market_cap_change_24h=4.0, volume_24h=2e9,
        coin_count=100, snapshot_at=datetime.now(timezone.utc) - timedelta(hours=6),
    )]
    results = compute_acceleration(now_snaps, old_snaps, accel_threshold=5.0, vol_threshold=10.0)
    assert len(results) == 1
    assert results[0].is_heating is False


def test_detect_market_regime():
    assert detect_market_regime(5.0) == "BULL"
    assert detect_market_regime(-5.0) == "BEAR"
    assert detect_market_regime(1.0) == "CRAB"
    assert detect_market_regime(3.0) == "CRAB"
    assert detect_market_regime(3.1) == "BULL"
    assert detect_market_regime(-3.1) == "BEAR"


async def test_fetch_categories_success():
    with aioresponses() as mocked:
        mocked.get(
            "https://api.coingecko.com/api/v3/coins/categories",
            payload=SAMPLE_CG_RESPONSE,
        )
        async with aiohttp.ClientSession() as session:
            data = await fetch_categories(session)
    assert len(data) == 2


async def test_fetch_categories_429_retries():
    with aioresponses() as mocked:
        mocked.get(
            "https://api.coingecko.com/api/v3/coins/categories",
            status=429,
        )
        mocked.get(
            "https://api.coingecko.com/api/v3/coins/categories",
            payload=SAMPLE_CG_RESPONSE,
        )
        async with aiohttp.ClientSession() as session:
            data = await fetch_categories(session)
    assert len(data) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_narrative_observer.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement observer**

```python
# scout/narrative/observer.py
"""OBSERVE phase — poll CoinGecko categories, detect acceleration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiohttp
import structlog

from scout.db import Database
from scout.narrative.models import CategoryAcceleration, CategorySnapshot

logger = structlog.get_logger()

CG_CATEGORIES_URL = "https://api.coingecko.com/api/v3/coins/categories"


async def fetch_categories(
    session: aiohttp.ClientSession,
    api_key: str = "",
    max_retries: int = 3,
) -> list[dict]:
    """Fetch /coins/categories with exponential backoff on 429."""
    headers = {}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key

    for attempt in range(max_retries):
        try:
            async with session.get(CG_CATEGORIES_URL, headers=headers) as resp:
                if resp.status == 429:
                    wait = 2 ** (attempt + 1)
                    logger.warning("cg_categories_rate_limited", wait=wait)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as e:
            logger.warning("cg_categories_fetch_error", error=str(e), attempt=attempt)
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** (attempt + 1))
    logger.error("cg_categories_all_retries_failed")
    return []


def parse_category_response(
    data: list[dict], market_regime: str
) -> list[CategorySnapshot]:
    """Parse CoinGecko categories response into snapshots. Skip malformed entries."""
    now = datetime.now(timezone.utc)
    snapshots = []
    for entry in data:
        try:
            mcap = entry.get("market_cap")
            change = entry.get("market_cap_change_24h")
            vol = entry.get("volume_24h")
            if mcap is None or change is None or vol is None:
                continue
            snapshots.append(CategorySnapshot(
                category_id=entry["id"],
                name=entry["name"],
                market_cap=float(mcap),
                market_cap_change_24h=float(change),
                volume_24h=float(vol),
                coin_count=None,  # not in /coins/categories response
                market_regime=market_regime,
                snapshot_at=now,
            ))
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("cg_category_parse_error", entry_id=entry.get("id"), error=str(e))
    return snapshots


def detect_market_regime(weighted_change_24h: float) -> str:
    """Classify market regime from total market cap change."""
    if weighted_change_24h > 3.0:
        return "BULL"
    if weighted_change_24h < -3.0:
        return "BEAR"
    return "CRAB"


def compute_acceleration(
    current: list[CategorySnapshot],
    previous: list[CategorySnapshot],
    accel_threshold: float,
    vol_threshold: float,
) -> list[CategoryAcceleration]:
    """Compare current vs 6h-ago snapshots to find accelerating categories."""
    prev_map = {s.category_id: s for s in previous}
    results = []
    for snap in current:
        old = prev_map.get(snap.category_id)
        if old is None:
            continue
        acceleration = snap.market_cap_change_24h - old.market_cap_change_24h
        vol_growth = (
            ((snap.volume_24h - old.volume_24h) / old.volume_24h) * 100
            if old.volume_24h > 0
            else 0.0
        )
        coin_change = None
        if snap.coin_count is not None and old.coin_count is not None:
            coin_change = snap.coin_count - old.coin_count

        is_heating = acceleration > accel_threshold and vol_growth > vol_threshold

        results.append(CategoryAcceleration(
            category_id=snap.category_id,
            name=snap.name,
            current_velocity=snap.market_cap_change_24h,
            previous_velocity=old.market_cap_change_24h,
            acceleration=acceleration,
            volume_growth_pct=vol_growth,
            coin_count_change=coin_change,
            is_heating=is_heating,
        ))
    return results


async def store_snapshot(
    db: Database, snapshots: list[CategorySnapshot]
) -> None:
    """Persist category snapshots to database."""
    conn = db._conn
    for s in snapshots:
        await conn.execute(
            """INSERT INTO category_snapshots
               (category_id, name, market_cap, market_cap_change_24h,
                volume_24h, coin_count, market_regime, snapshot_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (s.category_id, s.name, s.market_cap, s.market_cap_change_24h,
             s.volume_24h, s.coin_count, s.market_regime,
             s.snapshot_at.isoformat()),
        )
    await conn.commit()


async def load_snapshots_at(
    db: Database, target_time: datetime
) -> list[CategorySnapshot]:
    """Load the most recent snapshots before target_time."""
    conn = db._conn
    async with conn.execute(
        """SELECT * FROM category_snapshots
           WHERE snapshot_at <= ?
           ORDER BY snapshot_at DESC
           LIMIT 500""",
        (target_time.isoformat(),),
    ) as cur:
        rows = await cur.fetchall()

    seen = set()
    results = []
    for row in rows:
        cid = row["category_id"]
        if cid in seen:
            continue
        seen.add(cid)
        results.append(CategorySnapshot(
            category_id=cid,
            name=row["name"],
            market_cap=row["market_cap"],
            market_cap_change_24h=row["market_cap_change_24h"],
            volume_24h=row["volume_24h"],
            coin_count=row["coin_count"],
            market_regime=row["market_regime"],
            snapshot_at=datetime.fromisoformat(row["snapshot_at"]),
        ))
    return results


async def prune_old_snapshots(db: Database, retention_days: int) -> int:
    """Delete snapshots older than retention_days. Returns count deleted."""
    conn = db._conn
    cutoff = datetime.now(timezone.utc) - __import__("datetime").timedelta(days=retention_days)
    cursor = await conn.execute(
        "DELETE FROM category_snapshots WHERE snapshot_at < ?",
        (cutoff.isoformat(),),
    )
    await conn.commit()
    return cursor.rowcount
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_narrative_observer.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scout/narrative/observer.py tests/test_narrative_observer.py
git commit -m "feat(narrative): add OBSERVE phase — category polling, acceleration detection, market regime"
```

---

## Task 6: Predictor (PREDICT phase)

**Files:**
- Create: `scout/narrative/predictor.py`
- Test: `tests/test_narrative_predictor.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_narrative_predictor.py
"""Tests for narrative PREDICT phase."""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import aioresponses
import aiohttp

from scout.db import Database
from scout.narrative.models import CategoryAcceleration, LaggardToken, NarrativePrediction
from scout.narrative.predictor import (
    fetch_laggards,
    filter_laggards,
    select_control_picks,
    build_scoring_prompt,
    parse_scoring_response,
    is_cooling_down,
    record_signal,
    store_predictions,
)
from scout.narrative.strategy import Strategy


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
async def strategy(db):
    s = Strategy(db)
    await s.load_or_init()
    return s


SAMPLE_MARKETS = [
    {"id": "token-a", "symbol": "TKA", "name": "Token A",
     "market_cap": 30e6, "current_price": 0.5,
     "price_change_percentage_24h": 2.0, "total_volume": 500_000},
    {"id": "token-b", "symbol": "TKB", "name": "Token B",
     "market_cap": 80e6, "current_price": 1.2,
     "price_change_percentage_24h": -5.0, "total_volume": 200_000},
    {"id": "token-c", "symbol": "TKC", "name": "Token C",
     "market_cap": 300e6, "current_price": 10.0,
     "price_change_percentage_24h": 25.0, "total_volume": 1e6},
    {"id": "token-d", "symbol": "TKD", "name": "Token D",
     "market_cap": 20e6, "current_price": 0.1,
     "price_change_percentage_24h": 1.0, "total_volume": 50_000},
]


def test_filter_laggards_applies_thresholds():
    laggards = filter_laggards(
        SAMPLE_MARKETS,
        category_id="ai",
        category_name="AI",
        max_mcap=200e6,
        max_change=10.0,
        min_change=-20.0,
        min_volume=100_000,
    )
    symbols = [l.symbol for l in laggards]
    assert "TKA" in symbols  # 30M, +2%, 500K vol — passes
    assert "TKB" in symbols  # 80M, -5%, 200K vol — passes
    assert "TKC" not in symbols  # 300M mcap too high AND +25% change too high
    assert "TKD" not in symbols  # 50K vol too low


def test_filter_laggards_sorted_by_change_then_vol_mcap():
    laggards = filter_laggards(
        SAMPLE_MARKETS, "ai", "AI",
        max_mcap=200e6, max_change=10.0, min_change=-20.0, min_volume=100_000,
    )
    assert laggards[0].symbol == "TKB"  # -5% (most behind)
    assert laggards[1].symbol == "TKA"  # +2%


def test_partition_and_select():
    tokens = [LaggardToken(
        coin_id=f"token-{i}", symbol=f"TK{i}", name=f"Token {i}",
        market_cap=30e6, price=0.5, price_change_24h=float(i),
        volume_24h=500_000, category_id="ai", category_name="AI",
    ) for i in range(10)]
    scored, control = partition_and_select(tokens, max_picks=3)
    assert len(scored) == 3
    assert len(control) == 3
    # No overlap between scored and control
    scored_ids = {t.coin_id for t in scored}
    control_ids = {t.coin_id for t in control}
    assert scored_ids.isdisjoint(control_ids)


def test_parse_scoring_response_valid():
    text = '{"narrative_fit": 75, "staying_power": "High", "confidence": "Medium", "reasoning": "Strong fit"}'
    result = parse_scoring_response(text)
    assert result["narrative_fit"] == 75
    assert result["staying_power"] == "High"


def test_parse_scoring_response_markdown_wrapped():
    text = '```json\n{"narrative_fit": 60, "staying_power": "Low", "confidence": "Low", "reasoning": "Weak"}\n```'
    result = parse_scoring_response(text)
    assert result["narrative_fit"] == 60


async def test_is_cooling_down_no_signal(db):
    result = await is_cooling_down(db, "ai")
    assert result is False


async def test_is_cooling_down_active_signal(db):
    now = datetime.now(timezone.utc)
    future = now + timedelta(hours=2)
    await db._conn.execute(
        """INSERT INTO narrative_signals
           (category_id, category_name, acceleration, volume_growth_pct,
            trigger_count, detected_at, cooling_down_until)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("ai", "AI", 7.0, 15.0, 1, now.isoformat(), future.isoformat()),
    )
    await db._conn.commit()
    result = await is_cooling_down(db, "ai")
    assert result is True


async def test_record_signal_increments_trigger_count(db):
    now = datetime.now(timezone.utc)
    future = now + timedelta(hours=2)
    await db._conn.execute(
        """INSERT INTO narrative_signals
           (category_id, category_name, acceleration, volume_growth_pct,
            trigger_count, detected_at, cooling_down_until)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("ai", "AI", 7.0, 15.0, 1, now.isoformat(), future.isoformat()),
    )
    await db._conn.commit()
    await record_signal(db, "ai", "AI", 8.0, 20.0, None, cooldown_hours=4)
    async with db._conn.execute(
        "SELECT trigger_count FROM narrative_signals WHERE category_id='ai'"
    ) as cur:
        row = await cur.fetchone()
    assert row["trigger_count"] == 2
```

- [ ] **Step 2: Run tests, verify fail**

Run: `uv run pytest tests/test_narrative_predictor.py -v`
Expected: FAIL

- [ ] **Step 3: Implement predictor**

```python
# scout/narrative/predictor.py
"""PREDICT phase — laggard selection, Claude scoring, control picks, dedup."""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import anthropic
import structlog

from scout.db import Database
from scout.narrative.models import (
    CategoryAcceleration,
    LaggardToken,
    NarrativePrediction,
)
from scout.narrative.prompts import NARRATIVE_FIT_SYSTEM, NARRATIVE_FIT_TEMPLATE
from scout.narrative.strategy import Strategy

logger = structlog.get_logger()


async def fetch_laggards(
    session: aiohttp.ClientSession,
    category_id: str,
    api_key: str = "",
) -> list[dict]:
    """Fetch tokens in a category from CoinGecko."""
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "category": category_id,
        "order": "market_cap_desc",
        "per_page": "100",
        "sparkline": "false",
    }
    headers = {}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 429:
                logger.warning("cg_markets_rate_limited", category=category_id)
                return []
            resp.raise_for_status()
            return await resp.json()
    except aiohttp.ClientError as e:
        logger.warning("cg_markets_fetch_error", category=category_id, error=str(e))
        return []


def filter_laggards(
    tokens: list[dict],
    category_id: str,
    category_name: str,
    max_mcap: float,
    max_change: float,
    min_change: float,
    min_volume: float,
) -> list[LaggardToken]:
    """Filter and sort tokens into laggard candidates."""
    results = []
    for t in tokens:
        try:
            mcap = t.get("market_cap") or 0
            change = t.get("price_change_percentage_24h") or 0
            vol = t.get("total_volume") or 0
            price = t.get("current_price") or 0
            if mcap > max_mcap or change > max_change or change < min_change or vol < min_volume:
                continue
            results.append(LaggardToken(
                coin_id=t["id"],
                symbol=t.get("symbol", "").upper(),
                name=t.get("name", ""),
                market_cap=mcap,
                price=price,
                price_change_24h=change,
                volume_24h=vol,
                category_id=category_id,
                category_name=category_name,
            ))
        except (KeyError, TypeError):
            continue

    # Sort: price_change asc (most behind), then vol/mcap desc (tie-breaker)
    results.sort(key=lambda x: (x.price_change_24h, -(x.volume_24h / max(x.market_cap, 1))))
    return results


def partition_and_select(
    laggards: list[LaggardToken],
    max_picks: int,
) -> tuple[list[LaggardToken], list[LaggardToken]]:
    """Randomly partition laggards into scored + control groups BEFORE sorting.

    This avoids selection bias where controls are systematically less laggard
    than scored picks. Both groups are random samples from the same pool.
    """
    shuffled = laggards.copy()
    random.shuffle(shuffled)
    count = min(max_picks, len(shuffled) // 2)
    scored = shuffled[:count]
    control = shuffled[count : count * 2]
    # Sort scored by price_change for presentation order
    scored.sort(key=lambda x: (x.price_change_24h, -(x.volume_24h / max(x.market_cap, 1))))
    return scored, control


def build_scoring_prompt(
    token: LaggardToken,
    accel: CategoryAcceleration,
    market_regime: str,
    top_3_coins: str,
    lessons_appendix: str,
) -> str:
    """Build the narrative-fit prompt for a single token."""
    vol_mcap = token.volume_24h / max(token.market_cap, 1)
    return NARRATIVE_FIT_TEMPLATE.format(
        category_name=accel.name,
        mcap_change=accel.current_velocity,
        acceleration=accel.acceleration,
        volume=accel.volume_growth_pct,
        vol_growth=accel.volume_growth_pct,
        top_3_coins=top_3_coins,
        token_name=token.name,
        symbol=token.symbol,
        market_cap=token.market_cap,
        price_change_24h=token.price_change_24h,
        market_regime=market_regime,
        coin_count_change=accel.coin_count_change or 0,
        vol_mcap_ratio=vol_mcap,
        lessons_appendix=f"LESSONS:\n{lessons_appendix}\n\n" if lessons_appendix else "",
    )


def parse_scoring_response(text: str) -> dict:
    """Extract JSON from Claude response, handling markdown blocks."""
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1).strip())
    return json.loads(text.strip())


async def score_token(
    token: LaggardToken,
    accel: CategoryAcceleration,
    market_regime: str,
    top_3_coins: str,
    lessons: str,
    api_key: str,
    model: str,
    client: anthropic.AsyncAnthropic | None = None,
) -> dict | None:
    """Score a single token's narrative fit. Returns parsed dict or None on failure."""
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=api_key)

    prompt = build_scoring_prompt(token, accel, market_regime, top_3_coins, lessons)
    try:
        message = await client.messages.create(
            model=model,
            max_tokens=300,
            temperature=0,
            system=NARRATIVE_FIT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text
        return parse_scoring_response(text)
    except Exception as e:
        logger.warning("narrative_scoring_error", token=token.symbol, error=str(e))
        return None


async def is_cooling_down(db: Database, category_id: str) -> bool:
    """Check if a category has an active cooling-down signal."""
    now = datetime.now(timezone.utc).isoformat()
    async with db._conn.execute(
        """SELECT id FROM narrative_signals
           WHERE category_id = ? AND cooling_down_until > ?
           LIMIT 1""",
        (category_id, now),
    ) as cur:
        row = await cur.fetchone()
    return row is not None


async def record_signal(
    db: Database,
    category_id: str,
    category_name: str,
    acceleration: float,
    volume_growth_pct: float,
    coin_count_change: int | None,
    cooldown_hours: int,
) -> int:
    """Record or update a narrative signal. Returns current trigger_count."""
    now = datetime.now(timezone.utc)
    conn = db._conn

    # Check for existing active signal
    async with conn.execute(
        """SELECT id, trigger_count FROM narrative_signals
           WHERE category_id = ? AND cooling_down_until > ?""",
        (category_id, now.isoformat()),
    ) as cur:
        existing = await cur.fetchone()

    if existing:
        new_count = existing["trigger_count"] + 1
        await conn.execute(
            "UPDATE narrative_signals SET trigger_count = ? WHERE id = ?",
            (new_count, existing["id"]),
        )
        await conn.commit()
        return new_count

    cooldown_until = now + timedelta(hours=cooldown_hours)
    await conn.execute(
        """INSERT INTO narrative_signals
           (category_id, category_name, acceleration, volume_growth_pct,
            coin_count_change, trigger_count, detected_at, cooling_down_until)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
        (category_id, category_name, acceleration, volume_growth_pct,
         coin_count_change, now.isoformat(), cooldown_until.isoformat()),
    )
    await conn.commit()
    return 1


async def store_predictions(
    db: Database, predictions: list[NarrativePrediction]
) -> None:
    """Persist predictions to database. Skips duplicates via UNIQUE constraint."""
    conn = db._conn
    for p in predictions:
        try:
            await conn.execute(
                """INSERT OR IGNORE INTO predictions
                   (category_id, category_name, coin_id, symbol, name,
                    market_cap_at_prediction, price_at_prediction,
                    narrative_fit_score, staying_power, confidence, reasoning,
                    market_regime, trigger_count, is_control, is_holdout,
                    strategy_snapshot, strategy_snapshot_ab, predicted_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p.category_id, p.category_name, p.coin_id, p.symbol, p.name,
                 p.market_cap_at_prediction, p.price_at_prediction,
                 p.narrative_fit_score, p.staying_power, p.confidence, p.reasoning,
                 p.market_regime, p.trigger_count, int(p.is_control), int(p.is_holdout),
                 json.dumps(p.strategy_snapshot),
                 json.dumps(p.strategy_snapshot_ab) if p.strategy_snapshot_ab else None,
                 p.predicted_at.isoformat()),
            )
        except Exception as e:
            logger.warning("prediction_store_error", coin=p.symbol, error=str(e))
    await conn.commit()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_narrative_predictor.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scout/narrative/predictor.py tests/test_narrative_predictor.py
git commit -m "feat(narrative): add PREDICT phase — laggard selection, scoring, control picks, dedup"
```

---

## Task 7: Evaluator (EVALUATE phase)

**Files:**
- Create: `scout/narrative/evaluator.py`
- Test: `tests/test_narrative_evaluator.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_narrative_evaluator.py
"""Tests for narrative EVALUATE phase."""
from datetime import datetime, timezone, timedelta

import pytest

from scout.narrative.evaluator import classify_checkpoint, pick_final_class


def test_classify_hit():
    assert classify_checkpoint(20.0, hit=15.0, miss=-10.0) == "HIT"


def test_classify_miss():
    assert classify_checkpoint(-15.0, hit=15.0, miss=-10.0) == "MISS"


def test_classify_neutral():
    assert classify_checkpoint(5.0, hit=15.0, miss=-10.0) == "NEUTRAL"


def test_classify_boundary_hit():
    assert classify_checkpoint(15.0, hit=15.0, miss=-10.0) == "HIT"


def test_classify_boundary_miss():
    assert classify_checkpoint(-10.0, hit=15.0, miss=-10.0) == "MISS"


def test_final_class_uses_48h():
    assert pick_final_class("HIT", "NEUTRAL", "MISS") == "MISS"


def test_final_class_all_hit():
    assert pick_final_class("HIT", "HIT", "HIT") == "HIT"


def test_final_class_none_48h():
    assert pick_final_class("HIT", "NEUTRAL", None) is None
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/test_narrative_evaluator.py -v`
Expected: FAIL

- [ ] **Step 3: Implement evaluator**

```python
# scout/narrative/evaluator.py
"""EVALUATE phase — multi-checkpoint outcome tracking with peak detection."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import aiohttp
import structlog

from scout.db import Database
from scout.narrative.strategy import Strategy

logger = structlog.get_logger()


def classify_checkpoint(
    change_pct: float, hit: float, miss: float
) -> str:
    """Classify a single checkpoint outcome."""
    if change_pct >= hit:
        return "HIT"
    if change_pct <= miss:
        return "MISS"
    return "NEUTRAL"


def pick_final_class(
    cls_6h: str | None, cls_24h: str | None, cls_48h: str | None
) -> str | None:
    """Final verdict is the 48h checkpoint class."""
    return cls_48h


async def fetch_prices_batch(
    session: aiohttp.ClientSession,
    coin_ids: list[str],
    api_key: str = "",
) -> dict[str, float]:
    """Batch fetch current prices via /coins/markets?ids=..."""
    if not coin_ids:
        return {}
    url = "https://api.coingecko.com/api/v3/coins/markets"
    headers = {}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key

    prices = {}
    # CoinGecko supports up to 250 ids per call
    for i in range(0, len(coin_ids), 250):
        batch = coin_ids[i : i + 250]
        params = {
            "vs_currency": "usd",
            "ids": ",".join(batch),
            "per_page": "250",
            "sparkline": "false",
        }
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 429:
                    logger.warning("eval_price_fetch_rate_limited")
                    break
                resp.raise_for_status()
                data = await resp.json()
                for coin in data:
                    if coin.get("current_price") is not None:
                        prices[coin["id"]] = float(coin["current_price"])
        except Exception as e:
            logger.warning("eval_price_fetch_error", error=str(e))
    return prices


async def evaluate_pending(
    session: aiohttp.ClientSession,
    db: Database,
    strategy: Strategy,
    api_key: str = "",
) -> None:
    """Evaluate all predictions with pending checkpoints."""
    now = datetime.now(timezone.utc)
    hit_threshold = await strategy.get("hit_threshold_pct")
    miss_threshold = await strategy.get("miss_threshold_pct")
    conn = db._conn

    # Find predictions needing evaluation
    async with conn.execute(
        """SELECT id, coin_id, price_at_prediction, predicted_at,
                  outcome_6h_price, outcome_24h_price, outcome_48h_price,
                  peak_price, peak_change_pct, eval_retry_count
           FROM predictions
           WHERE outcome_class IS NULL"""
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        return

    # Collect coin IDs needing price checks
    coin_ids = list({row["coin_id"] for row in rows})
    prices = await fetch_prices_batch(session, coin_ids, api_key)

    for row in rows:
        coin_id = row["coin_id"]
        entry_price = row["price_at_prediction"]
        predicted_at = datetime.fromisoformat(row["predicted_at"])
        current_price = prices.get(coin_id)

        if current_price is None:
            retry = (row["eval_retry_count"] or 0) + 1
            if retry >= 3:
                await conn.execute(
                    """UPDATE predictions SET outcome_class = 'UNRESOLVED',
                       outcome_reason = 'price_unavailable', eval_retry_count = ?,
                       evaluated_at = ? WHERE id = ?""",
                    (retry, now.isoformat(), row["id"]),
                )
            else:
                await conn.execute(
                    "UPDATE predictions SET eval_retry_count = ? WHERE id = ?",
                    (retry, row["id"]),
                )
            continue

        change_pct = ((current_price - entry_price) / entry_price) * 100
        updates = {}

        # Peak tracking
        old_peak = row["peak_price"] or entry_price
        if current_price > old_peak:
            updates["peak_price"] = current_price
            updates["peak_change_pct"] = change_pct
            updates["peak_at"] = now.isoformat()

        # 6h checkpoint
        if row["outcome_6h_price"] is None and now >= predicted_at + timedelta(hours=6):
            updates["outcome_6h_price"] = current_price
            updates["outcome_6h_change_pct"] = change_pct
            updates["outcome_6h_class"] = classify_checkpoint(change_pct, hit_threshold, miss_threshold)

        # 24h checkpoint
        if row["outcome_24h_price"] is None and now >= predicted_at + timedelta(hours=24):
            updates["outcome_24h_price"] = current_price
            updates["outcome_24h_change_pct"] = change_pct
            updates["outcome_24h_class"] = classify_checkpoint(change_pct, hit_threshold, miss_threshold)

        # 48h checkpoint — final verdict
        if row["outcome_48h_price"] is None and now >= predicted_at + timedelta(hours=48):
            updates["outcome_48h_price"] = current_price
            updates["outcome_48h_change_pct"] = change_pct
            cls_48h = classify_checkpoint(change_pct, hit_threshold, miss_threshold)
            updates["outcome_48h_class"] = cls_48h
            updates["outcome_class"] = cls_48h
            updates["evaluated_at"] = now.isoformat()

        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [row["id"]]
            await conn.execute(
                f"UPDATE predictions SET {set_clause} WHERE id = ?", values
            )

    await conn.commit()
    logger.info("evaluate_complete", predictions_checked=len(rows), prices_found=len(prices))
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_narrative_evaluator.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scout/narrative/evaluator.py tests/test_narrative_evaluator.py
git commit -m "feat(narrative): add EVALUATE phase — multi-checkpoint outcomes, peak tracking, batch pricing"
```

---

## Task 8: Learner (LEARN phase)

**Files:**
- Create: `scout/narrative/learner.py`
- Test: `tests/test_narrative_learner.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_narrative_learner.py
"""Tests for narrative LEARN phase."""
import json
from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from scout.db import Database
from scout.narrative.learner import (
    compute_hit_rates,
    apply_adjustments,
    should_pause,
)
from scout.narrative.strategy import Strategy


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
async def strategy(db):
    s = Strategy(db)
    await s.load_or_init()
    return s


async def test_compute_hit_rates_empty(db):
    rates = await compute_hit_rates(db)
    assert rates["agent_hit_rate"] == 0.0
    assert rates["control_hit_rate"] == 0.0
    assert rates["true_alpha"] == 0.0


async def test_compute_hit_rates_with_data(db):
    now = datetime.now(timezone.utc).isoformat()
    conn = db._conn
    # Insert 2 agent predictions: 1 HIT, 1 MISS
    for i, (cls, is_ctrl) in enumerate([("HIT", 0), ("MISS", 0), ("HIT", 1), ("HIT", 1)]):
        await conn.execute(
            """INSERT INTO predictions
               (category_id, category_name, coin_id, symbol, name,
                market_cap_at_prediction, price_at_prediction,
                narrative_fit_score, staying_power, confidence, reasoning,
                market_regime, trigger_count, is_control, is_holdout,
                strategy_snapshot, predicted_at, outcome_class, evaluated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("ai", "AI", f"c{i}", f"S{i}", f"N{i}", 50e6, 1.0,
             75, "High", "Med", "r", "BULL", 1, is_ctrl, 0,
             json.dumps({}), now, cls, now),
        )
    await conn.commit()
    rates = await compute_hit_rates(db)
    assert rates["agent_hit_rate"] == 50.0  # 1/2
    assert rates["control_hit_rate"] == 100.0  # 2/2
    assert rates["true_alpha"] == -50.0


async def test_apply_adjustments_respects_min_sample(strategy, db):
    # With 0 evaluated predictions, adjustments should not apply
    adjustments = [{"key": "hit_threshold_pct", "new_value": 20.0, "reason": "test"}]
    applied = await apply_adjustments(adjustments, strategy, db, min_sample=100)
    assert applied == 0
    assert await strategy.get("hit_threshold_pct") == 15.0


def test_should_pause_below_threshold():
    daily_rates = [8.0, 9.0, 7.0, 5.0, 6.0, 8.0, 9.0]
    assert should_pause(daily_rates, threshold=10.0, consecutive_days=7) is True


def test_should_pause_above_threshold():
    daily_rates = [8.0, 9.0, 7.0, 5.0, 6.0, 8.0, 15.0]
    assert should_pause(daily_rates, threshold=10.0, consecutive_days=7) is False


def test_should_pause_not_enough_data():
    daily_rates = [5.0, 5.0]
    assert should_pause(daily_rates, threshold=10.0, consecutive_days=7) is False
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/test_narrative_learner.py -v`
Expected: FAIL

- [ ] **Step 3: Implement learner**

```python
# scout/narrative/learner.py
"""LEARN phase — daily reflection, weekly consolidation, strategy updates."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import anthropic
import structlog

from scout.db import Database
from scout.narrative.prompts import DAILY_REFLECTION_TEMPLATE, WEEKLY_CONSOLIDATION_TEMPLATE
from scout.narrative.strategy import Strategy

logger = structlog.get_logger()


async def compute_hit_rates(db: Database) -> dict:
    """Compute agent vs control hit rates from evaluated predictions."""
    conn = db._conn

    async def _rate(is_control: int) -> float:
        async with conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN outcome_class = 'HIT' THEN 1 ELSE 0 END) as hits
               FROM predictions
               WHERE is_control = ? AND outcome_class IS NOT NULL
                     AND outcome_class != 'UNRESOLVED'""",
            (is_control,),
        ) as cur:
            row = await cur.fetchone()
        total = row["total"] or 0
        hits = row["hits"] or 0
        return (hits / total * 100) if total > 0 else 0.0

    agent_rate = await _rate(0)
    control_rate = await _rate(1)
    return {
        "agent_hit_rate": agent_rate,
        "control_hit_rate": control_rate,
        "true_alpha": agent_rate - control_rate,
    }


async def get_recent_predictions(db: Database, limit: int = 100) -> list[dict]:
    """Fetch recent evaluated predictions for reflection."""
    conn = db._conn
    async with conn.execute(
        """SELECT * FROM predictions
           WHERE outcome_class IS NOT NULL
           ORDER BY evaluated_at DESC LIMIT ?""",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def apply_adjustments(
    adjustments: list[dict],
    strategy: Strategy,
    db: Database,
    cycle_number: int = 0,
    min_sample: int = 100,
) -> int:
    """Apply strategy adjustments if minimum sample met. Returns count applied."""
    conn = db._conn
    async with conn.execute(
        """SELECT COUNT(*) as cnt FROM predictions
           WHERE outcome_class IS NOT NULL AND outcome_class != 'UNRESOLVED'
                 AND is_control = 0"""
    ) as cur:
        row = await cur.fetchone()
    total = row["cnt"] or 0

    if total < min_sample:
        logger.info("learn_skip_adjustments", reason="below_min_sample", total=total, min=min_sample)
        return 0

    applied = 0
    for adj in adjustments:
        key = adj.get("key", "")
        new_val = adj.get("new_value")
        reason = adj.get("reason", "")
        try:
            await strategy.set(key, new_val, f"learn_cycle_{cycle_number}", reason)
            applied += 1
            logger.info("strategy_adjusted", key=key, new_value=new_val, reason=reason)
        except (ValueError, KeyError) as e:
            logger.warning("strategy_adjustment_rejected", key=key, error=str(e))
    return applied


def should_pause(
    daily_rates: list[float],
    threshold: float = 10.0,
    consecutive_days: int = 7,
) -> bool:
    """Check if circuit breaker should fire."""
    if len(daily_rates) < consecutive_days:
        return False
    recent = daily_rates[-consecutive_days:]
    return all(r < threshold for r in recent)


async def daily_learn(
    db: Database,
    strategy: Strategy,
    api_key: str,
    model: str,
) -> dict | None:
    """Run daily reflection. Returns parsed reflection or None."""
    rates = await compute_hit_rates(db)
    predictions = await get_recent_predictions(db, limit=100)
    if not predictions:
        logger.info("learn_skip", reason="no_predictions")
        return None

    all_strategy = await strategy.get_all()
    # Build regime breakdown
    regime_counts: dict[str, dict[str, int]] = {}
    for p in predictions:
        if p.get("is_control"):
            continue
        regime = p.get("market_regime", "UNKNOWN")
        if regime not in regime_counts:
            regime_counts[regime] = {"total": 0, "hits": 0}
        regime_counts[regime]["total"] += 1
        if p.get("outcome_class") == "HIT":
            regime_counts[regime]["hits"] += 1

    regime_str = ", ".join(
        f"{r}={d['hits']}/{d['total']} ({d['hits']/max(d['total'],1)*100:.0f}%)"
        for r, d in regime_counts.items()
    )

    prompt = DAILY_REFLECTION_TEMPLATE.format(
        sample_size=len(predictions),
        predictions_json=json.dumps(predictions, indent=2, default=str)[:8000],
        control_hit_rate=rates["control_hit_rate"],
        agent_hit_rate=rates["agent_hit_rate"],
        true_alpha=rates["true_alpha"],
        strategy_json=json.dumps(all_strategy, indent=2, default=str),
        regime_breakdown=regime_str,
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model=model,
            max_tokens=1500,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text
        # Parse JSON from response
        import re
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        raw = match.group(1).strip() if match else text.strip()
        result = json.loads(raw)

        # Apply adjustments
        min_sample = await strategy.get("min_learn_sample")
        adjustments = result.get("adjustments", [])
        applied = await apply_adjustments(adjustments, strategy, db, cycle_number=cycle, min_sample=int(min_sample))

        # Log reflection
        conn = db._conn
        async with conn.execute("SELECT MAX(cycle_number) as mx FROM learn_logs") as cur:
            row = await cur.fetchone()
        cycle = (row["mx"] or 0) + 1

        await conn.execute(
            """INSERT INTO learn_logs
               (cycle_number, cycle_type, reflection_text, changes_made,
                hit_rate_before, created_at)
               VALUES (?, 'daily', ?, ?, ?, ?)""",
            (cycle, result.get("reflection", ""),
             json.dumps(adjustments), rates["agent_hit_rate"],
             datetime.now(timezone.utc).isoformat()),
        )
        await conn.commit()

        logger.info("daily_learn_complete", cycle=cycle, adjustments=applied,
                     true_alpha=rates["true_alpha"])
        return result

    except Exception as e:
        logger.error("daily_learn_error", error=str(e))
        return None


async def weekly_consolidate(
    db: Database,
    strategy: Strategy,
    api_key: str,
    model: str,
) -> dict | None:
    """Weekly lesson consolidation — prune, merge, version lessons."""
    conn = db._conn
    current_lessons = await strategy.get("lessons_learned")
    current_version = await strategy.get("lessons_version")

    # Gather this week's daily reflections
    async with conn.execute(
        """SELECT reflection_text, changes_made, hit_rate_before, created_at
           FROM learn_logs WHERE cycle_type = 'daily'
           ORDER BY created_at DESC LIMIT 7"""
    ) as cur:
        rows = await cur.fetchall()
    weekly_reflections = "\n\n".join(
        f"[{row['created_at']}] HR={row['hit_rate_before']:.1f}%: {row['reflection_text']}"
        for row in rows
    )

    if not weekly_reflections and not current_lessons:
        logger.info("weekly_consolidate_skip", reason="no_lessons_or_reflections")
        return None

    prompt = WEEKLY_CONSOLIDATION_TEMPLATE.format(
        current_lessons=current_lessons or "(none yet)",
        weekly_reflections=weekly_reflections or "(no reflections this week)",
        hit_rate_per_lesson="(insufficient data for per-lesson breakdown)",
        next_version=int(current_version) + 1,
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model=model,
            max_tokens=2000,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text
        import re
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        raw = match.group(1).strip() if match else text.strip()
        result = json.loads(raw)

        new_lessons = result.get("consolidated_lessons", "")
        new_version = result.get("lessons_version", int(current_version) + 1)

        # Store previous version for rollback
        await strategy.set(
            f"lessons_v{current_version}", current_lessons,
            "weekly_consolidate", "archived previous version",
        )
        await strategy.set("lessons_learned", new_lessons, "weekly_consolidate", "consolidated")
        await strategy.set("lessons_version", new_version, "weekly_consolidate", "version bump")

        # Log
        async with conn.execute("SELECT MAX(cycle_number) as mx FROM learn_logs") as cur:
            row = await cur.fetchone()
        cycle = (row["mx"] or 0) + 1
        rates = await compute_hit_rates(db)
        await conn.execute(
            """INSERT INTO learn_logs
               (cycle_number, cycle_type, reflection_text, changes_made,
                hit_rate_before, created_at)
               VALUES (?, 'weekly', ?, ?, ?, ?)""",
            (cycle, f"Consolidated to v{new_version}. Removed: {json.dumps(result.get('removed', []))}",
             json.dumps(result), rates["agent_hit_rate"],
             datetime.now(timezone.utc).isoformat()),
        )
        await conn.commit()

        logger.info("weekly_consolidate_complete", version=new_version,
                     removed=len(result.get("removed", [])))
        return result

    except Exception as e:
        logger.error("weekly_consolidate_error", error=str(e))
        return None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_narrative_learner.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scout/narrative/learner.py tests/test_narrative_learner.py
git commit -m "feat(narrative): add LEARN phase — daily reflection, hit rates, circuit breaker, strategy updates"
```

---

## Task 9: Digest (daily/weekly summaries)

**Files:**
- Create: `scout/narrative/digest.py`

- [ ] **Step 1: Implement digest builder**

```python
# scout/narrative/digest.py
"""Build daily and real-time Telegram alert messages."""

from __future__ import annotations

from scout.narrative.models import CategoryAcceleration, NarrativePrediction


def format_heating_alert(
    accel: CategoryAcceleration,
    predictions: list[NarrativePrediction],
    top_3_coins: str,
) -> str:
    """Format a real-time heating narrative Telegram alert."""
    lines = [
        f"Narrative Heating: {accel.name}",
        f"Acceleration: {accel.previous_velocity:+.1f}% -> {accel.current_velocity:+.1f}% (+{accel.acceleration:.1f}%)",
        f"Volume growth: +{accel.volume_growth_pct:.0f}% in 6h",
        "",
        "Top picks (haven't pumped yet):",
    ]
    scored = [p for p in predictions if not p.is_control]
    for i, p in enumerate(scored[:5], 1):
        lines.append(
            f"{i}. {p.symbol} (${p.market_cap_at_prediction/1e6:.0f}M, "
            f"{p.price_at_prediction}$) — Fit: {p.narrative_fit_score}/100 [{p.confidence}]"
        )
        if p.reasoning:
            lines.append(f'   "{p.reasoning[:100]}"')

    lines.append(f"\nCategory leaders: {top_3_coins}")
    lines.append(f"Market regime: {predictions[0].market_regime if predictions else 'N/A'}")
    if accel.coin_count_change is not None and accel.coin_count_change < -5:
        lines.append(f"Warning: coin count dropped by {abs(accel.coin_count_change)} (survivorship risk)")
    return "\n".join(lines)


def format_daily_digest(
    heating: list[str],
    cooling: list[str],
    picks_today: int,
    categories_today: int,
    yesterday_results: list[dict],
    hit_rate: float,
    reflection: str,
    changes: list[dict],
    true_alpha: float,
) -> str:
    """Format the daily Telegram digest."""
    lines = [
        "Narrative Rotation — Daily Digest",
        "",
        f"HEATING: {', '.join(heating) if heating else 'None'}",
        f"COOLING: {', '.join(cooling) if cooling else 'None'}",
        "",
        f"Today's picks: {picks_today} across {categories_today} categories",
    ]

    if yesterday_results:
        hit_count = sum(1 for r in yesterday_results if r.get("outcome_class") == "HIT")
        total = len(yesterday_results)
        lines.append(f"Yesterday's results: {hit_count}/{total} ({hit_rate:.0f}%)")
        for r in yesterday_results[:5]:
            icon = "+" if r.get("outcome_48h_change_pct", 0) >= 0 else ""
            lines.append(
                f"  {r.get('symbol', '?')}: {icon}{r.get('outcome_48h_change_pct', 0):.1f}% "
                f"(picked at ${r.get('price_at_prediction', 0):.4f})"
            )

    lines.append(f"\nTrue alpha: {true_alpha:+.1f}pp vs random baseline")
    if reflection:
        lines.append(f'\nAgent insight: "{reflection[:200]}"')
    if changes:
        lines.append(f"Strategy changes: {len(changes)}")
        for c in changes[:3]:
            lines.append(f"  {c.get('key', '?')}: {c.get('new_value', '?')} — {c.get('reason', '')[:80]}")
    else:
        lines.append("Strategy changes: None")

    return "\n".join(lines)
```

- [ ] **Step 2: Commit**

```bash
git add scout/narrative/digest.py
git commit -m "feat(narrative): add digest builder for Telegram alerts and daily summary"
```

---

## Task 10: Main Loop Integration

**Files:**
- Modify: `scout/main.py`

- [ ] **Step 1: Read current main.py to find the right insertion point**

Look for the existing `asyncio.gather()` or main loop structure.

- [ ] **Step 2: Add narrative agent loop**

Add import at top of `scout/main.py`:

```python
from scout.narrative.observer import (
    fetch_categories, parse_category_response, compute_acceleration,
    detect_market_regime, store_snapshot, load_snapshots_at, prune_old_snapshots,
)
from scout.narrative.predictor import (
    fetch_laggards, filter_laggards, partition_and_select,
    score_token, is_cooling_down, record_signal, store_predictions,
)
from scout.narrative.evaluator import evaluate_pending
from scout.narrative.learner import daily_learn, weekly_consolidate, compute_hit_rates, should_pause
from scout.narrative.strategy import Strategy
from scout.narrative.digest import format_heating_alert
from scout.narrative.models import NarrativePrediction
from scout.db import Database
```

Add the `narrative_agent_loop` function and wire it into the existing `main()` function's `asyncio.gather()`:

```python
async def narrative_agent_loop(session: aiohttp.ClientSession, settings, db: Database) -> None:
    """Autonomous narrative rotation agent loop."""
    strategy = Strategy(db)
    await strategy.load_or_init()

    last_eval_at = await strategy.get_timestamp("last_eval_at", datetime.min)
    last_daily_learn_at = await strategy.get_timestamp("last_daily_learn_at", datetime.min)
    last_weekly_learn_at = await strategy.get_timestamp("last_weekly_learn_at", datetime.min)

    while True:
        now = datetime.now(timezone.utc)
        try:
            # OBSERVE
            raw = await fetch_categories(session, settings.COINGECKO_API_KEY)
            if raw:
                total_change = sum(
                    (c.get("market_cap_change_24h") or 0) * (c.get("market_cap") or 0)
                    for c in raw
                )
                total_mcap = sum((c.get("market_cap") or 0) for c in raw)
                weighted_change = total_change / total_mcap if total_mcap > 0 else 0
                regime = detect_market_regime(weighted_change)

                snapshots = parse_category_response(raw, regime)
                await store_snapshot(db, snapshots)

                # Load 6h-ago snapshots for acceleration
                old_snapshots = await load_snapshots_at(
                    db, now - timedelta(hours=6)
                )
                if old_snapshots:
                    accel_threshold = await strategy.get("category_accel_threshold")
                    vol_threshold = await strategy.get("category_volume_growth_min")
                    heating = compute_acceleration(
                        snapshots, old_snapshots, accel_threshold, vol_threshold
                    )
                    heating_only = [h for h in heating if h.is_heating]
                    heating_only.sort(key=lambda h: h.acceleration, reverse=True)

                    max_per_cycle = int(await strategy.get("max_heating_per_cycle"))
                    for accel in heating_only[:max_per_cycle]:
                        if await is_cooling_down(db, accel.category_id):
                            continue

                        cooldown = int(await strategy.get("signal_cooldown_hours"))
                        trigger = await record_signal(
                            db, accel.category_id, accel.name,
                            accel.acceleration, accel.volume_growth_pct,
                            accel.coin_count_change, cooldown,
                        )

                        min_trigger = int(await strategy.get("min_trigger_count"))
                        if trigger < min_trigger:
                            continue

                        # PREDICT
                        raw_tokens = await fetch_laggards(session, accel.category_id, settings.COINGECKO_API_KEY)
                        laggards = filter_laggards(
                            raw_tokens, accel.category_id, accel.name,
                            max_mcap=await strategy.get("laggard_max_mcap"),
                            max_change=await strategy.get("laggard_max_change"),
                            min_change=await strategy.get("laggard_min_change"),
                            min_volume=await strategy.get("laggard_min_volume"),
                        )

                        max_picks = int(await strategy.get("max_picks_per_category"))
                        scored_laggards, control_laggards = partition_and_select(laggards, max_picks)

                        lessons = await strategy.get("lessons_learned")
                        top_3 = ", ".join(
                            c.get("id", "") for c in (
                                next((e for e in raw if e["id"] == accel.category_id), {})
                            ).get("top_3_coins_id", [])
                        ) if raw else ""

                        predictions = []
                        for token in scored_laggards:
                            result = await score_token(
                                token, accel, regime, top_3, lessons or "",
                                settings.ANTHROPIC_API_KEY,
                                settings.NARRATIVE_SCORING_MODEL,
                            )
                            if result:
                                snap = {
                                    "category_accel_threshold": accel_threshold,
                                    "laggard_max_mcap": await strategy.get("laggard_max_mcap"),
                                    "hit_threshold_pct": await strategy.get("hit_threshold_pct"),
                                    "lessons_version": await strategy.get("lessons_version"),
                                    "min_trigger_count": min_trigger,
                                }
                                predictions.append(NarrativePrediction(
                                    category_id=accel.category_id,
                                    category_name=accel.name,
                                    coin_id=token.coin_id,
                                    symbol=token.symbol,
                                    name=token.name,
                                    market_cap_at_prediction=token.market_cap,
                                    price_at_prediction=token.price,
                                    narrative_fit_score=result["narrative_fit"],
                                    staying_power=result["staying_power"],
                                    confidence=result["confidence"],
                                    reasoning=result["reasoning"],
                                    market_regime=regime,
                                    trigger_count=trigger,
                                    strategy_snapshot=snap,
                                    predicted_at=now,
                                ))

                        # Control picks (random partition, no selection bias)
                        for token in control_laggards:
                            predictions.append(NarrativePrediction(
                                category_id=accel.category_id,
                                category_name=accel.name,
                                coin_id=token.coin_id,
                                symbol=token.symbol,
                                name=token.name,
                                market_cap_at_prediction=token.market_cap,
                                price_at_prediction=token.price,
                                narrative_fit_score=0,
                                staying_power="",
                                confidence="CONTROL",
                                reasoning="",
                                market_regime=regime,
                                trigger_count=trigger,
                                is_control=True,
                                strategy_snapshot={},
                                predicted_at=now,
                            ))

                        await store_predictions(db, predictions)

                        # ALERT
                        alert_enabled = await strategy.get("narrative_alert_enabled")
                        if alert_enabled and predictions:
                            msg = format_heating_alert(accel, predictions, top_3)
                            try:
                                from scout.alerter import send_telegram_message
                                await send_telegram_message(msg, session, settings)
                            except Exception as e:
                                logger.warning("narrative_alert_failed", error=str(e))

            # EVALUATE (gated)
            if (now - last_eval_at).total_seconds() >= settings.NARRATIVE_EVAL_INTERVAL:
                await evaluate_pending(session, db, strategy, settings.COINGECKO_API_KEY)
                last_eval_at = now
                await strategy.set_timestamp("last_eval_at", now)

            # LEARN — daily (gated)
            if (now.hour == settings.NARRATIVE_LEARN_HOUR_UTC
                    and (now - last_daily_learn_at).total_seconds() > 82800):
                await daily_learn(db, strategy, settings.ANTHROPIC_API_KEY, settings.NARRATIVE_LEARN_MODEL)
                last_daily_learn_at = now
                await strategy.set_timestamp("last_daily_learn_at", now)
                # Prune old snapshots
                await prune_old_snapshots(db, settings.NARRATIVE_SNAPSHOT_RETENTION_DAYS)

            # LEARN — weekly (gated: once per week)
            if (now.weekday() == settings.NARRATIVE_WEEKLY_LEARN_DAY
                    and now.hour == (settings.NARRATIVE_LEARN_HOUR_UTC + 1) % 24
                    and (now - last_weekly_learn_at).total_seconds() > 601200):
                await weekly_consolidate(db, strategy, settings.ANTHROPIC_API_KEY, settings.NARRATIVE_LEARN_MODEL)
                last_weekly_learn_at = now
                await strategy.set_timestamp("last_weekly_learn_at", now)

        except Exception as e:
            logger.error("narrative_agent_error", error=str(e))

        await asyncio.sleep(settings.NARRATIVE_POLL_INTERVAL)
```

In the existing `main()` function, add to the `asyncio.gather()`:

```python
if settings.NARRATIVE_ENABLED:
    tasks.append(narrative_agent_loop(session, settings, db))
```

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add scout/main.py
git commit -m "feat(narrative): wire narrative agent loop into main pipeline via asyncio.gather()"
```

---

## Task 11: Dashboard API Endpoints

**Files:**
- Modify: `dashboard/api.py`

- [ ] **Step 1: Add narrative API endpoints**

Read `dashboard/api.py` to understand the existing pattern (how routes are defined, how DB is accessed). Then add 7 endpoints following the same pattern. Each endpoint runs a SQL query against the narrative tables and returns JSON.

The SQL queries for each endpoint:

```python
# GET /api/narrative/heating — latest snapshot acceleration
# SQL: SELECT cs1.*, cs2.market_cap_change_24h as prev_change
#      FROM category_snapshots cs1
#      LEFT JOIN category_snapshots cs2 ON cs1.category_id = cs2.category_id
#        AND cs2.snapshot_at = (SELECT MAX(snapshot_at) FROM category_snapshots
#                               WHERE category_id = cs1.category_id
#                               AND snapshot_at < datetime(cs1.snapshot_at, '-6 hours'))
#      WHERE cs1.snapshot_at = (SELECT MAX(snapshot_at) FROM category_snapshots)
#      ORDER BY (cs1.market_cap_change_24h - COALESCE(cs2.market_cap_change_24h, 0)) DESC

# GET /api/narrative/predictions?limit=50&outcome=HIT
# SQL: SELECT * FROM predictions ORDER BY predicted_at DESC LIMIT ? 
#      + optional WHERE outcome_class = ? filter

# GET /api/narrative/metrics
# SQL: SELECT COUNT(*) as total,
#             SUM(CASE WHEN outcome_class='HIT' AND is_control=0 THEN 1 ELSE 0 END) as agent_hits,
#             SUM(CASE WHEN is_control=0 AND outcome_class IS NOT NULL THEN 1 ELSE 0 END) as agent_total,
#             SUM(CASE WHEN outcome_class='HIT' AND is_control=1 THEN 1 ELSE 0 END) as ctrl_hits,
#             SUM(CASE WHEN is_control=1 AND outcome_class IS NOT NULL THEN 1 ELSE 0 END) as ctrl_total
#      FROM predictions WHERE outcome_class != 'UNRESOLVED'

# GET /api/narrative/strategy
# SQL: SELECT * FROM agent_strategy ORDER BY key

# PUT /api/narrative/strategy/{key} — body: {"value": 20.0}
# SQL: UPDATE agent_strategy SET value=?, updated_at=?, updated_by='manual', locked=1 WHERE key=?

# GET /api/narrative/learn-logs?limit=20
# SQL: SELECT * FROM learn_logs ORDER BY created_at DESC LIMIT ?

# GET /api/narrative/categories/history?category_id=ai&hours=48
# SQL: SELECT * FROM category_snapshots WHERE category_id=? 
#      AND snapshot_at > datetime('now', '-? hours') ORDER BY snapshot_at
```

Follow the existing `dashboard/api.py` patterns exactly for route registration, DB connection access, and response format.

- [ ] **Step 2: Commit**

```bash
git add dashboard/api.py
git commit -m "feat(narrative): add 7 dashboard API endpoints for narrative rotation tab"
```

---

## Task 12: Dashboard Frontend — Narrative Tab

**Files:**
- Create: `dashboard/frontend/components/NarrativeTab.jsx`
- Modify: `dashboard/frontend/App.jsx`

- [ ] **Step 1: Read existing dashboard components for patterns**

Read `dashboard/frontend/App.jsx` and one existing component (e.g., `CandidatesTable.jsx`) to understand the patterns: how data is fetched (useEffect + fetch), how tables are rendered, how tabs work.

- [ ] **Step 2: Create NarrativeTab component**

Follow the existing component patterns. The component fetches from `/api/narrative/metrics`, `/api/narrative/heating`, `/api/narrative/predictions`, and renders:

1. **Metrics row** (4 cards): Hit Rate %, True Alpha pp, Active Signals count, Total Predictions
2. **Heating Categories table**: category name, acceleration %, volume growth %, regime
3. **Predictions table**: symbol, category, fit score, confidence, 6h/24h/48h outcomes, class, peak
4. **Strategy table**: key, value, updated_by, reason, locked toggle

Use the same CSS classes and table patterns as `CandidatesTable.jsx`. Fetch data on mount and poll every 60 seconds.

- [ ] **Step 3: Add tab to App.jsx**

Import `NarrativeTab` and add it as a new tab option in the existing tab switcher. Follow the pattern of existing tabs.

- [ ] **Step 3: Build and verify**

```bash
cd dashboard/frontend && npm run build && cd ../..
```

- [ ] **Step 4: Commit**

```bash
git add dashboard/frontend/
git commit -m "feat(narrative): add Narrative Rotation dashboard tab with heat map, predictions, metrics"
```

---

## Task 13: Update .env on VPS + Enable

**Files:**
- Modify: VPS `/root/gecko-alpha/.env`

- [ ] **Step 1: Deploy to Srilu VPS**

```bash
ssh srilu-vps 'cd /root/gecko-alpha && git pull origin master'
ssh srilu-vps 'cd /root/gecko-alpha && source ~/.local/bin/env && uv sync'
```

- [ ] **Step 2: Add config to .env**

```bash
ssh srilu-vps 'cat >> /root/gecko-alpha/.env << EOF

# === Narrative Rotation Agent ===
NARRATIVE_ENABLED=true
NARRATIVE_POLL_INTERVAL=1800
NARRATIVE_SCORING_MODEL=claude-haiku-4-5
NARRATIVE_LEARN_MODEL=claude-sonnet-4-6
EOF'
```

- [ ] **Step 3: Restart services**

```bash
ssh srilu-vps 'systemctl restart gecko-pipeline gecko-dashboard'
```

- [ ] **Step 4: Verify agent is running**

```bash
ssh srilu-vps 'journalctl -u gecko-pipeline -n 20 --no-pager'
```

Look for: `cg_categories` log entries every 30 minutes.

---

## Build Order Summary

| Task | What | Dependencies | Est. Time |
|------|------|-------------|-----------|
| 1 | Models + Config | None | 5 min |
| 2 | Database Tables | Task 1 | 5 min |
| 3 | Strategy Manager | Task 2 | 10 min |
| 4 | Prompts | None | 3 min |
| 5 | Observer | Tasks 1-3 | 10 min |
| 6 | Predictor | Tasks 1-5 | 15 min |
| 7 | Evaluator | Tasks 1-3 | 10 min |
| 8 | Learner | Tasks 1-4, 7 | 10 min |
| 9 | Digest | Tasks 1 | 5 min |
| 10 | Main Loop | Tasks 1-9 | 10 min |
| 11 | Dashboard API | Tasks 1-3 | 10 min |
| 12 | Dashboard Frontend | Task 11 | 15 min |
| 13 | Deploy | All | 5 min |
