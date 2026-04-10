# Multi-Signal Conviction Chains — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correlate signals from different modules into temporal chains that match known pump patterns. Detect when signals fire in a specific SEQUENCE (within time windows) that historically predicts sustained pumps, and boost conviction scores for tokens that complete a chain.

**Architecture:** New `scout/chains/` package runs alongside the existing pipeline via `asyncio.gather()`. Pure data-matching — zero external API calls, zero Claude calls. Event emission from existing modules (1-line calls) feeds a signal_events table. A 5-minute tracker loop matches events against configurable chain_patterns, stores completed chains in chain_matches, and exposes a `get_active_boosts()` query for the scoring pipeline.

**Tech Stack:** Python 3.12, aiosqlite, Pydantic v2, structlog, pytest (asyncio_mode=auto)

**Spec:** `docs/superpowers/specs/2026-04-10-conviction-chains-design.md`

---

## File Map

### New files (create)

| File | Responsibility |
|------|---------------|
| `scout/chains/__init__.py` | Package init |
| `scout/chains/models.py` | Pydantic models: ChainEvent, ChainStep, ChainPattern, ActiveChain, ChainMatch |
| `scout/chains/events.py` | `emit_event()` helper + signal_events CRUD + retention pruning |
| `scout/chains/patterns.py` | Built-in pattern definitions, `evaluate_condition()`, seeding helper |
| `scout/chains/tracker.py` | `run_chain_tracker()` loop + `check_chains()` matching engine + `get_active_boosts()` |
| `scout/chains/alerts.py` | High-conviction chain alert formatting |
| `tests/test_chains_models.py` | Model validation |
| `tests/test_chains_db.py` | DB schema creation & constraints |
| `tests/test_chains_events.py` | Event emission + append-only + retention |
| `tests/test_chains_patterns.py` | Condition evaluator + built-in seeding |
| `tests/test_chains_tracker.py` | Pattern matching engine (core algorithm correctness) |
| `tests/test_chains_integration.py` | Full end-to-end chain scenario |
| `tests/test_chains_learn.py` | LEARN phase: hit-rate stats + lifecycle |

### Modified files

| File | Changes |
|------|---------|
| `scout/config.py` | Add 12 `CHAIN_*` config fields (incl. 4 LEARN knobs) |
| `scout/narrative/learn.py` | Invoke `run_pattern_lifecycle` on daily LEARN tick |
| `scout/db.py` | Add 4 new tables to `_create_tables()` |
| `scout/narrative/observer.py` | Add 1 `emit_event()` call on category heating detection |
| `scout/narrative/predictor.py` | Add 2 `emit_event()` calls (laggard_picked + narrative_scored) |
| `scout/scorer.py` | Add 1 `emit_event()` call after scoring |
| `scout/counter/scorer.py` | Add 1 `emit_event()` call after counter scoring |
| `scout/gate.py` | Add 1 `emit_event()` call after gating + integrate `get_active_boosts()` |
| `scout/alerter.py` | Add 1 `emit_event()` call after sending alert |
| `scout/main.py` | Add `run_chain_tracker()` task to the main `asyncio.gather()` and seed patterns at startup |
| `.env.example` | Add `CHAIN_*` env vars |

---

## Task 1: Models + Config

**Files:**
- Create: `scout/chains/__init__.py`
- Create: `scout/chains/models.py`
- Modify: `scout/config.py`
- Modify: `.env.example`
- Test: `tests/test_chains_models.py`

- [ ] **Step 1: Write failing tests for models**

```python
# tests/test_chains_models.py
"""Tests for conviction chain Pydantic models."""
from datetime import datetime, timezone

from scout.chains.models import (
    ActiveChain,
    ChainEvent,
    ChainMatch,
    ChainPattern,
    ChainStep,
)


def test_chain_event_required_fields():
    ev = ChainEvent(
        token_id="0xabc",
        pipeline="memecoin",
        event_type="candidate_scored",
        event_data={"quant_score": 72, "signal_count": 3},
        source_module="scorer",
        created_at=datetime.now(timezone.utc),
    )
    assert ev.id is None
    assert ev.pipeline == "memecoin"
    assert ev.event_data["signal_count"] == 3


def test_chain_step_optional_condition():
    s = ChainStep(
        step_number=1,
        event_type="category_heating",
        max_hours_after_anchor=0.0,
    )
    assert s.condition is None
    assert s.max_hours_after_previous is None


def test_chain_pattern_with_steps():
    pat = ChainPattern(
        name="test_pattern",
        description="A test pattern",
        steps=[
            ChainStep(step_number=1, event_type="category_heating",
                      max_hours_after_anchor=0.0),
            ChainStep(step_number=2, event_type="laggard_picked",
                      max_hours_after_anchor=6.0),
        ],
        min_steps_to_trigger=2,
        conviction_boost=25,
        alert_priority="high",
    )
    assert pat.is_active is True
    assert pat.total_triggers == 0
    assert pat.historical_hit_rate is None


def test_active_chain_tracking():
    now = datetime.now(timezone.utc)
    ac = ActiveChain(
        token_id="0xabc",
        pipeline="memecoin",
        pattern_id=1,
        pattern_name="full_conviction",
        steps_matched=[1, 2],
        step_events={1: 10, 2: 15},
        anchor_time=now,
        last_step_time=now,
        created_at=now,
    )
    assert ac.is_complete is False
    assert ac.step_events[1] == 10


def test_chain_match_outcome_nullable():
    now = datetime.now(timezone.utc)
    cm = ChainMatch(
        token_id="0xabc",
        pipeline="memecoin",
        pattern_id=1,
        pattern_name="full_conviction",
        steps_matched=3,
        total_steps=4,
        anchor_time=now,
        completed_at=now,
        chain_duration_hours=4.5,
        conviction_boost=25,
    )
    assert cm.outcome_class is None
    assert cm.evaluated_at is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chains_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scout.chains'`

- [ ] **Step 3: Create package and models**

```python
# scout/chains/__init__.py
"""Multi-Signal Conviction Chains — temporal pattern matching over signal events."""
```

```python
# scout/chains/models.py
"""Pydantic models for the conviction chain tracker."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ChainEvent(BaseModel):
    """A single signal event emitted by any module."""
    id: int | None = None
    token_id: str
    pipeline: str                      # "narrative" | "memecoin"
    event_type: str
    event_data: dict
    source_module: str
    created_at: datetime


class ChainStep(BaseModel):
    """One step in a chain pattern definition."""
    step_number: int
    event_type: str
    condition: str | None = None
    max_hours_after_anchor: float
    max_hours_after_previous: float | None = None


class ChainPattern(BaseModel):
    """A configurable chain pattern definition."""
    id: int | None = None
    name: str
    description: str
    steps: list[ChainStep]
    min_steps_to_trigger: int
    conviction_boost: int
    alert_priority: str                # "high" | "medium" | "low"
    is_active: bool = True
    historical_hit_rate: float | None = None
    total_triggers: int = 0
    total_hits: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ActiveChain(BaseModel):
    """Tracks an in-progress chain for a specific token."""
    id: int | None = None
    token_id: str
    pipeline: str
    pattern_id: int
    pattern_name: str
    steps_matched: list[int]
    step_events: dict[int, int]        # step_number -> signal_event_id
    anchor_time: datetime
    last_step_time: datetime
    is_complete: bool = False
    completed_at: datetime | None = None
    created_at: datetime


class ChainMatch(BaseModel):
    """A completed chain — stored for LEARN phase and boost application."""
    id: int | None = None
    token_id: str
    pipeline: str
    pattern_id: int
    pattern_name: str
    steps_matched: int
    total_steps: int
    anchor_time: datetime
    completed_at: datetime
    chain_duration_hours: float
    conviction_boost: int
    outcome_class: str | None = None
    outcome_change_pct: float | None = None
    evaluated_at: datetime | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_chains_models.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Add config fields to Settings**

Add to `scout/config.py` inside the `Settings` class, after existing chain-adjacent config:

```python
    # Conviction Chains
    CHAIN_CHECK_INTERVAL_SEC: int = 300          # 5 minutes
    CHAIN_MAX_WINDOW_HOURS: float = 24.0
    CHAIN_COOLDOWN_HOURS: float = 12.0
    CHAIN_EVENT_RETENTION_DAYS: int = 14
    CHAIN_ACTIVE_RETENTION_DAYS: int = 7
    CHAIN_ALERT_ON_COMPLETE: bool = True
    CHAIN_TOTAL_BOOST_CAP: int = 30
    CHAINS_ENABLED: bool = False
    # LEARN phase lifecycle knobs (Task 9)
    CHAIN_MIN_TRIGGERS_FOR_STATS: int = 10
    CHAIN_PROMOTION_THRESHOLD: float = 0.45  # 45% hit rate for promotion from low to medium
    CHAIN_GRADUATION_MIN_TRIGGERS: int = 30
    CHAIN_GRADUATION_HIT_RATE: float = 0.55  # 55% hit rate for graduation to high alert
```

Add to `.env.example` at the bottom:

```
# === Conviction Chains ===
CHAINS_ENABLED=false
CHAIN_CHECK_INTERVAL_SEC=300
CHAIN_MAX_WINDOW_HOURS=24.0
CHAIN_COOLDOWN_HOURS=12.0
CHAIN_EVENT_RETENTION_DAYS=14
CHAIN_ACTIVE_RETENTION_DAYS=7
CHAIN_ALERT_ON_COMPLETE=true
CHAIN_TOTAL_BOOST_CAP=30
# LEARN phase lifecycle
CHAIN_MIN_TRIGGERS_FOR_STATS=10
CHAIN_PROMOTION_THRESHOLD=0.45
CHAIN_GRADUATION_MIN_TRIGGERS=30
CHAIN_GRADUATION_HIT_RATE=0.55
```

- [ ] **Step 6: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: All existing tests + new model tests PASS

- [ ] **Step 7: Commit**

```bash
git add scout/chains/__init__.py scout/chains/models.py scout/config.py .env.example tests/test_chains_models.py
git commit -m "feat(chains): add Pydantic models and config for conviction chains"
```

---

## Task 2: Database Schema

**Files:**
- Modify: `scout/db.py`
- Test: `tests/test_chains_db.py`

- [ ] **Step 1: Write failing tests for tables + constraints**

```python
# tests/test_chains_db.py
"""Tests for conviction chain database tables."""
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


async def test_chain_tables_created(db):
    tables: list[str] = []
    async with db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cur:
        async for row in cur:
            tables.append(row[0])
    for t in ["signal_events", "chain_patterns", "active_chains", "chain_matches"]:
        assert t in tables, f"Missing table: {t}"


async def test_signal_events_append_only(db):
    now = datetime.now(timezone.utc).isoformat()
    # Insert the same row twice — must succeed both times.
    for _ in range(2):
        await db._conn.execute(
            """INSERT INTO signal_events
               (token_id, pipeline, event_type, event_data, source_module, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("0xabc", "memecoin", "candidate_scored", json.dumps({"x": 1}), "scorer", now),
        )
    await db._conn.commit()
    async with db._conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE token_id='0xabc'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == 2


async def test_chain_patterns_name_unique(db):
    await db._conn.execute(
        """INSERT INTO chain_patterns
           (name, description, steps_json, min_steps_to_trigger,
            conviction_boost, alert_priority)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("test", "d", "[]", 2, 10, "low"),
    )
    await db._conn.commit()
    with pytest.raises(Exception):
        await db._conn.execute(
            """INSERT INTO chain_patterns
               (name, description, steps_json, min_steps_to_trigger,
                conviction_boost, alert_priority)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("test", "d2", "[]", 2, 10, "low"),
        )
        await db._conn.commit()


async def test_active_chains_unique_constraint(db):
    # Seed a pattern
    await db._conn.execute(
        """INSERT INTO chain_patterns
           (name, description, steps_json, min_steps_to_trigger,
            conviction_boost, alert_priority)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "d", "[]", 2, 10, "low"),
    )
    await db._conn.commit()
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO active_chains
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            step_events, anchor_time, last_step_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("0xabc", "memecoin", 1, "p1", "[1]", "{}", now, now),
    )
    await db._conn.commit()
    with pytest.raises(Exception):
        await db._conn.execute(
            """INSERT INTO active_chains
               (token_id, pipeline, pattern_id, pattern_name, steps_matched,
                step_events, anchor_time, last_step_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("0xabc", "memecoin", 1, "p1", "[1]", "{}", now, now),
        )
        await db._conn.commit()


async def test_chain_matches_insert(db):
    now = datetime.now(timezone.utc).isoformat()
    # Must have a pattern first (FK)
    await db._conn.execute(
        """INSERT INTO chain_patterns
           (name, description, steps_json, min_steps_to_trigger,
            conviction_boost, alert_priority)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "d", "[]", 3, 25, "high"),
    )
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("0xabc", "memecoin", 1, "p1", 3, 4, now, now, 4.0, 25),
    )
    await db._conn.commit()
    async with db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id='0xabc'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chains_db.py -v`
Expected: FAIL — tables missing

- [ ] **Step 3: Add tables to `_create_tables()`**

Append to the `executescript` in `scout/db.py` _before_ the closing `"""`:

```sql
            CREATE TABLE IF NOT EXISTS signal_events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id       TEXT NOT NULL,
                pipeline       TEXT NOT NULL,
                event_type     TEXT NOT NULL,
                event_data     TEXT NOT NULL,
                source_module  TEXT NOT NULL,
                created_at     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sig_events_token
                ON signal_events(token_id, pipeline, created_at);
            CREATE INDEX IF NOT EXISTS idx_sig_events_type
                ON signal_events(event_type, created_at);

            CREATE TABLE IF NOT EXISTS chain_patterns (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                name                 TEXT NOT NULL UNIQUE,
                description          TEXT NOT NULL,
                steps_json           TEXT NOT NULL,
                min_steps_to_trigger INTEGER NOT NULL,
                conviction_boost     INTEGER NOT NULL DEFAULT 0,
                alert_priority       TEXT NOT NULL DEFAULT 'low',
                is_active            INTEGER NOT NULL DEFAULT 1,
                historical_hit_rate  REAL,
                total_triggers       INTEGER DEFAULT 0,
                total_hits           INTEGER DEFAULT 0,
                created_at           TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS active_chains (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id       TEXT NOT NULL,
                pipeline       TEXT NOT NULL,
                pattern_id     INTEGER NOT NULL REFERENCES chain_patterns(id),
                pattern_name   TEXT NOT NULL,
                steps_matched  TEXT NOT NULL,
                step_events    TEXT NOT NULL,
                anchor_time    TEXT NOT NULL,
                last_step_time TEXT NOT NULL,
                is_complete    INTEGER DEFAULT 0,
                completed_at   TEXT,
                created_at     TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(token_id, pipeline, pattern_id, anchor_time)
            );
            CREATE INDEX IF NOT EXISTS idx_active_chains_token
                ON active_chains(token_id, pipeline, is_complete);

            CREATE TABLE IF NOT EXISTS chain_matches (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id             TEXT NOT NULL,
                pipeline             TEXT NOT NULL,
                pattern_id           INTEGER NOT NULL REFERENCES chain_patterns(id),
                pattern_name         TEXT NOT NULL,
                steps_matched        INTEGER NOT NULL,
                total_steps          INTEGER NOT NULL,
                anchor_time          TEXT NOT NULL,
                completed_at         TEXT NOT NULL,
                chain_duration_hours REAL NOT NULL,
                conviction_boost     INTEGER NOT NULL,
                outcome_class        TEXT,
                outcome_change_pct   REAL,
                evaluated_at         TEXT,
                created_at           TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_chain_matches_pattern
                ON chain_matches(pattern_id, outcome_class);
            CREATE INDEX IF NOT EXISTS idx_chain_matches_token
                ON chain_matches(token_id, pipeline, completed_at);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_chains_db.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add scout/db.py tests/test_chains_db.py
git commit -m "feat(chains): add signal_events, chain_patterns, active_chains, chain_matches tables"
```

---

## Task 3: Event Emission Helper

**Files:**
- Create: `scout/chains/events.py`
- Test: `tests/test_chains_events.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_chains_events.py
"""Tests for chain event emission + retention."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.chains.events import emit_event, load_recent_events, prune_old_events
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def test_emit_event_inserts_row(db):
    eid = await emit_event(
        db=db,
        token_id="0xabc",
        pipeline="memecoin",
        event_type="candidate_scored",
        event_data={"quant_score": 72, "signal_count": 3},
        source_module="scorer",
    )
    assert eid > 0
    async with db._conn.execute(
        "SELECT token_id, pipeline, event_type, event_data, source_module "
        "FROM signal_events WHERE id = ?", (eid,)
    ) as cur:
        row = await cur.fetchone()
    assert row["token_id"] == "0xabc"
    assert row["pipeline"] == "memecoin"
    assert row["event_type"] == "candidate_scored"
    assert json.loads(row["event_data"])["quant_score"] == 72
    assert row["source_module"] == "scorer"


async def test_emit_event_append_only(db):
    """Two identical calls must produce two rows — no dedup."""
    e1 = await emit_event(db, "0xabc", "memecoin", "candidate_scored",
                          {"quant_score": 72}, "scorer")
    e2 = await emit_event(db, "0xabc", "memecoin", "candidate_scored",
                          {"quant_score": 72}, "scorer")
    assert e1 != e2


async def test_load_recent_events_filters_by_window(db):
    # Insert an old event (20 days ago) and a fresh one
    old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("0xold", "memecoin", "candidate_scored", "{}", "scorer", old_ts),
    )
    await db._conn.commit()
    await emit_event(db, "0xnew", "memecoin", "candidate_scored", {}, "scorer")

    events = await load_recent_events(db, max_hours=24.0)
    ids = {e.token_id for e in events}
    assert "0xnew" in ids
    assert "0xold" not in ids


async def test_prune_old_events(db):
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("0xold", "memecoin", "candidate_scored", "{}", "scorer", old_ts),
    )
    await db._conn.commit()
    await emit_event(db, "0xnew", "memecoin", "candidate_scored", {}, "scorer")

    deleted = await prune_old_events(db, retention_days=14)
    assert deleted == 1

    async with db._conn.execute("SELECT COUNT(*) FROM signal_events") as cur:
        row = await cur.fetchone()
    assert row[0] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chains_events.py -v`
Expected: FAIL — `scout.chains.events` not found

- [ ] **Step 3: Implement `events.py`**

```python
# scout/chains/events.py
"""Signal event emission + retrieval for the chain tracker.

Every module with a meaningful signal calls `emit_event()` exactly once at its
natural decision point. The event store is append-only — no deduplication.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import structlog

from scout.chains.models import ChainEvent
from scout.db import Database

logger = structlog.get_logger()


async def emit_event(
    db: Database,
    token_id: str,
    pipeline: str,
    event_type: str,
    event_data: dict,
    source_module: str,
) -> int:
    """Append a signal event. Returns the new event row id.

    Never raises for business-logic reasons — callers can fire-and-forget.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")
    if pipeline not in ("narrative", "memecoin"):
        raise ValueError(f"Invalid pipeline: {pipeline!r}")

    now = datetime.now(timezone.utc).isoformat()
    cursor = await conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (token_id, pipeline, event_type, json.dumps(event_data), source_module, now),
    )
    await conn.commit()
    eid = cursor.lastrowid
    logger.debug(
        "chain_event_emitted",
        event_id=eid,
        token_id=token_id,
        pipeline=pipeline,
        event_type=event_type,
        source_module=source_module,
    )
    return int(eid)


async def load_recent_events(
    db: Database, max_hours: float
) -> list[ChainEvent]:
    """Load events from the last `max_hours`, oldest first."""
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_hours)).isoformat()
    async with conn.execute(
        """SELECT id, token_id, pipeline, event_type, event_data,
                  source_module, created_at
           FROM signal_events
           WHERE created_at >= ?
           ORDER BY created_at ASC""",
        (cutoff,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        ChainEvent(
            id=row["id"],
            token_id=row["token_id"],
            pipeline=row["pipeline"],
            event_type=row["event_type"],
            event_data=json.loads(row["event_data"]),
            source_module=row["source_module"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
        for row in rows
    ]


async def prune_old_events(db: Database, retention_days: int) -> int:
    """Delete events older than retention_days. Returns rows deleted."""
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=retention_days)
    ).isoformat()
    cursor = await conn.execute(
        "DELETE FROM signal_events WHERE created_at < ?", (cutoff,)
    )
    await conn.commit()
    return cursor.rowcount or 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_chains_events.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scout/chains/events.py tests/test_chains_events.py
git commit -m "feat(chains): add emit_event helper and signal_events CRUD"
```

---

## Task 4: Wire Event Emission into Existing Modules

**Files:**
- Modify: `scout/narrative/observer.py` (category_heating)
- Modify: `scout/narrative/predictor.py` (laggard_picked + narrative_scored)
- Modify: `scout/scorer.py` (candidate_scored)
- Modify: `scout/counter/scorer.py` (counter_scored)
- Modify: `scout/gate.py` (conviction_gated)
- Modify: `scout/alerter.py` (alert_fired)
- Test: extends `tests/test_chains_events.py` with one integration-style test

> **Strategy:** Each integration point is ONE `await emit_event(...)` call inserted at a natural decision point. The changes must be isolated and non-intrusive. Wrap each call in `try/except Exception` and log-and-continue so the chain module can never break the existing pipeline.

- [ ] **Step 1: Add integration test**

Append to `tests/test_chains_events.py`:

```python
async def test_emit_event_swallows_errors_via_safe_emit(db, monkeypatch):
    """safe_emit wraps emit_event and never raises."""
    from scout.chains.events import safe_emit
    from scout.config import Settings

    # Enable chains so safe_emit reaches emit_event (we're testing the
    # exception-swallow branch, not the kill-switch).
    monkeypatch.setattr(
        "scout.config.get_settings",
        lambda: Settings(CHAINS_ENABLED=True),
    )

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("scout.chains.events.emit_event", _boom)
    # Should NOT raise
    await safe_emit(db, "0xabc", "memecoin", "candidate_scored", {}, "scorer")


async def test_safe_emit_noop_when_disabled(db, monkeypatch):
    """CHAINS_ENABLED=False: safe_emit must insert ZERO rows."""
    from scout.chains.events import safe_emit
    from scout.config import Settings

    monkeypatch.setattr(
        "scout.config.get_settings",
        lambda: Settings(CHAINS_ENABLED=False),
    )

    # Count before
    async with db._conn.execute("SELECT COUNT(*) FROM signal_events") as cur:
        before = (await cur.fetchone())[0]

    result = await safe_emit(
        db, "0xabc", "memecoin", "candidate_scored",
        {"quant_score": 72}, "scorer",
    )
    assert result is None

    async with db._conn.execute("SELECT COUNT(*) FROM signal_events") as cur:
        after = (await cur.fetchone())[0]
    assert before == after, "safe_emit must be a total no-op when CHAINS_ENABLED=False"
```

- [ ] **Step 2: Add `safe_emit` to `scout/chains/events.py`**

Append:

```python
async def safe_emit(
    db: Database,
    token_id: str,
    pipeline: str,
    event_type: str,
    event_data: dict,
    source_module: str,
) -> int | None:
    """Call emit_event, log and swallow any exception.

    Use this from existing pipeline modules so chain tracking failures
    never break the main pipeline. When `CHAINS_ENABLED=False` this is a
    total no-op — no DB row is inserted.
    """
    # Hard kill-switch — CHAINS_ENABLED=false must produce zero writes.
    try:
        from scout.config import get_settings  # lazy import to avoid cycle
        settings = get_settings()
        if not getattr(settings, "CHAINS_ENABLED", False):
            return None
    except Exception:
        # If settings cannot be read, fail closed (no emission).
        return None
    try:
        return await emit_event(
            db, token_id, pipeline, event_type, event_data, source_module
        )
    except Exception as exc:
        logger.warning(
            "chain_event_emit_failed",
            token_id=token_id,
            pipeline=pipeline,
            event_type=event_type,
            error=str(exc),
        )
        return None
```

- [ ] **Step 3: Add event emission to existing modules**

In `scout/narrative/observer.py`, after a `CategoryAcceleration` is flagged as heating (inside the loop that marks `is_heating=True`), add:

```python
# near top of file
from scout.chains.events import safe_emit

# at the point where a heating category is confirmed, immediately after
# the existing heating log line:
await safe_emit(
    db,
    token_id=accel.category_id,
    pipeline="narrative",
    event_type="category_heating",
    event_data={
        "category_id": accel.category_id,
        "name": accel.name,
        "acceleration": accel.acceleration,
        "volume_growth_pct": accel.volume_growth_pct,
        "market_regime": market_regime,
    },
    source_module="narrative.observer",
)
```

In `scout/narrative/predictor.py`, after a laggard is selected AND after the Claude narrative-fit score is computed, add two calls:

```python
from scout.chains.events import safe_emit

# After laggard selected:
await safe_emit(
    db,
    token_id=token.coin_id,
    pipeline="narrative",
    event_type="laggard_picked",
    event_data={
        "category_id": accel.category_id,
        "category_name": accel.name,
        "narrative_fit_score": int(score_result.get("narrative_fit", 0)),
        "confidence": score_result.get("confidence", ""),
        "trigger_count": trigger_count,
    },
    source_module="narrative.predictor",
)

# After narrative_fit scoring completes:
await safe_emit(
    db,
    token_id=token.coin_id,
    pipeline="narrative",
    event_type="narrative_scored",
    event_data={
        "narrative_fit_score": int(score_result.get("narrative_fit", 0)),
        "staying_power": score_result.get("staying_power", ""),
        "confidence": score_result.get("confidence", ""),
    },
    source_module="narrative.predictor",
)
```

In `scout/scorer.py`, immediately after `score_token()` computes the final score (at the existing return-site), add:

```python
from scout.chains.events import safe_emit

await safe_emit(
    db,
    token_id=token.contract_address,
    pipeline="memecoin",
    event_type="candidate_scored",
    event_data={
        "quant_score": int(score),
        "signals_fired": list(signals_fired),
        "signal_count": len(signals_fired),
    },
    source_module="scorer",
)
```

> **Note:** `scorer.score_token` must accept a `db` parameter if it does not already. If threading `db` through is intrusive, move the `safe_emit` call to the caller site in `main.py.run_cycle` / `gate.py` where the score is recorded. Prefer the caller-site emission when `db` is not already in scope.

In `scout/counter/scorer.py`, at the end of both `score_counter_memecoin` and `score_counter_narrative`, just before returning `result`:

```python
from scout.chains.events import safe_emit

pipeline = "memecoin"  # or "narrative" in score_counter_narrative
await safe_emit(
    db,
    token_id=token_id,
    pipeline=pipeline,
    event_type="counter_scored",
    event_data={
        "risk_score": result.risk_score,
        "flag_count": len(result.flags),
        "high_severity_count": sum(1 for f in result.flags if f.severity == "high"),
        "data_completeness": result.data_completeness,
    },
    source_module="counter.scorer",
)
```

In `scout/gate.py`, at the end of `evaluate()`, just before `return (should_alert, conviction, updated)`:

```python
from scout.chains.events import safe_emit

# Emit UNCONDITIONALLY on every evaluate() call — NOT gated by should_alert.
# The volume_breakout pattern needs conviction_gated events to fire regardless
# of alert outcome so the chain tracker can match sequences that include
# borderline-score tokens that didn't quite trip the alert threshold.
await safe_emit(
    db,
    token_id=token.contract_address,
    pipeline="memecoin",
    event_type="conviction_gated",
    event_data={
        "conviction_score": float(conviction),
        "quant_score": int(quant_score),
        "narrative_score": int(narrative_score) if narrative_score is not None else None,
        "should_alert": bool(should_alert),
    },
    source_module="gate",
)
```

In `scout/alerter.py`, at the end of a successful Telegram send, add:

```python
from scout.chains.events import safe_emit

await safe_emit(
    db,
    token_id=token.contract_address,
    pipeline="memecoin",
    event_type="alert_fired",
    event_data={
        "conviction_score": float(token.conviction_score or 0),
        "alert_type": "telegram",
    },
    source_module="alerter",
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_chains_events.py -v`
Run: `uv run pytest --tb=short -q`
Expected: All tests pass (no regression from the integration points).

- [ ] **Step 5: Commit**

```bash
git add scout/chains/events.py scout/narrative/observer.py scout/narrative/predictor.py scout/scorer.py scout/counter/scorer.py scout/gate.py scout/alerter.py tests/test_chains_events.py
git commit -m "feat(chains): emit signal events from observer, predictor, scorer, counter, gate, alerter"
```

---

## Task 5: Built-in Patterns + Condition Evaluator + Seeding

**Files:**
- Create: `scout/chains/patterns.py`
- Test: `tests/test_chains_patterns.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_chains_patterns.py
"""Tests for chain pattern definitions, condition evaluator, and seeding."""
import json

import pytest

from scout.chains.models import ChainPattern, ChainStep
from scout.chains.patterns import (
    BUILT_IN_PATTERNS,
    evaluate_condition,
    load_active_patterns,
    seed_built_in_patterns,
)
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def test_condition_none_returns_true():
    assert evaluate_condition(None, {"x": 1}) is True


def test_condition_lt_matches():
    assert evaluate_condition("risk_score < 30", {"risk_score": 20}) is True
    assert evaluate_condition("risk_score < 30", {"risk_score": 30}) is False


def test_condition_gte_matches():
    assert evaluate_condition("signal_count >= 3", {"signal_count": 3}) is True
    assert evaluate_condition("signal_count >= 3", {"signal_count": 2}) is False


def test_condition_gt_matches():
    assert evaluate_condition("narrative_fit_score > 70", {"narrative_fit_score": 71}) is True
    assert evaluate_condition("narrative_fit_score > 70", {"narrative_fit_score": 70}) is False


def test_condition_missing_field_returns_false():
    assert evaluate_condition("risk_score < 30", {"other": 1}) is False


def test_condition_invalid_raises():
    with pytest.raises(ValueError):
        evaluate_condition("risk_score !! 30", {"risk_score": 20})


def test_builtin_patterns_count_and_fields():
    names = [p.name for p in BUILT_IN_PATTERNS]
    assert "full_conviction" in names
    assert "narrative_momentum" in names
    assert "volume_breakout" in names
    for p in BUILT_IN_PATTERNS:
        assert p.min_steps_to_trigger <= len(p.steps)
        assert p.conviction_boost >= 0
        assert p.alert_priority in ("high", "medium", "low")
        # Step 1 must always have max_hours_after_anchor == 0
        assert p.steps[0].max_hours_after_anchor == 0.0


async def test_seed_built_in_patterns_idempotent(db):
    await seed_built_in_patterns(db)
    await seed_built_in_patterns(db)  # second call must not duplicate
    async with db._conn.execute("SELECT COUNT(*) FROM chain_patterns") as cur:
        row = await cur.fetchone()
    assert row[0] == len(BUILT_IN_PATTERNS)


async def test_load_active_patterns_skips_inactive(db):
    await seed_built_in_patterns(db)
    # Mark one pattern inactive
    await db._conn.execute(
        "UPDATE chain_patterns SET is_active = 0 WHERE name = 'narrative_momentum'"
    )
    await db._conn.commit()
    patterns = await load_active_patterns(db)
    names = [p.name for p in patterns]
    assert "narrative_momentum" not in names
    assert "full_conviction" in names
    # Step roundtrip
    full = next(p for p in patterns if p.name == "full_conviction")
    assert len(full.steps) == 4
    assert full.steps[0].event_type == "category_heating"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chains_patterns.py -v`
Expected: FAIL — `scout.chains.patterns` does not exist

- [ ] **Step 3: Implement `patterns.py`**

```python
# scout/chains/patterns.py
"""Built-in chain pattern definitions, condition evaluator, and DB seeding."""

from __future__ import annotations

import json
import operator
import re
from datetime import datetime, timezone

import structlog

from scout.chains.models import ChainPattern, ChainStep
from scout.db import Database

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Condition evaluator
# ---------------------------------------------------------------------------

_OPERATORS = {
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    ">":  operator.gt,
    "<":  operator.lt,
}

# Order matters: longer tokens first so `>=` matches before `>`.
# NOTE: This regex is intentionally TIGHTER than the spec grammar.
# The spec allows free-form `field OP NUMBER`, but we anchor the entire
# string (^...$), require an identifier field, forbid whitespace inside
# the number, and restrict to a fixed operator set. This rejects injection
# attempts and pathological patterns like `1 < 2` or multi-clause expressions
# while still covering every condition used by the built-in patterns.
_CONDITION_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)\s*$"
)


def evaluate_condition(condition: str | None, event_data: dict) -> bool:
    """Evaluate a simple condition against event_data.

    Supported grammar: `field OP NUMBER` where OP in {>, <, >=, <=, ==}.
    - Returns True if condition is None (unconditional step).
    - Returns False if the field is missing from event_data.
    - Raises ValueError for malformed conditions.
    """
    if condition is None:
        return True
    m = _CONDITION_RE.match(condition)
    if not m:
        raise ValueError(f"Invalid condition: {condition!r}")
    field, op_str, value_str = m.groups()
    if field not in event_data or event_data[field] is None:
        return False
    try:
        return _OPERATORS[op_str](float(event_data[field]), float(value_str))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Built-in patterns
# ---------------------------------------------------------------------------

BUILT_IN_PATTERNS: list[ChainPattern] = [
    ChainPattern(
        name="full_conviction",
        description=(
            "Narrative heats → laggard picked → counter clean → quant signals "
            "converge. The strongest pattern."
        ),
        steps=[
            ChainStep(step_number=1, event_type="category_heating",
                      max_hours_after_anchor=0.0),
            ChainStep(step_number=2, event_type="laggard_picked",
                      max_hours_after_anchor=6.0),
            ChainStep(step_number=3, event_type="counter_scored",
                      condition="risk_score < 30",
                      max_hours_after_anchor=8.0),
            ChainStep(step_number=4, event_type="candidate_scored",
                      condition="signal_count >= 3",
                      max_hours_after_anchor=12.0),
        ],
        min_steps_to_trigger=3,
        conviction_boost=25,
        alert_priority="low",  # incubation by default; LEARN promotes later
    ),
    ChainPattern(
        name="narrative_momentum",
        description=(
            "Heating category + clean counter + high narrative fit. Early "
            "alert before volume confirms."
        ),
        steps=[
            ChainStep(step_number=1, event_type="category_heating",
                      max_hours_after_anchor=0.0),
            ChainStep(step_number=2, event_type="laggard_picked",
                      max_hours_after_anchor=4.0),
            ChainStep(step_number=3, event_type="narrative_scored",
                      condition="narrative_fit_score > 70",
                      max_hours_after_anchor=4.0),
            ChainStep(step_number=4, event_type="counter_scored",
                      condition="risk_score < 40",
                      max_hours_after_anchor=6.0),
        ],
        min_steps_to_trigger=3,
        conviction_boost=15,
        alert_priority="low",
    ),
    ChainPattern(
        name="volume_breakout",
        description=(
            "Pure quant: successive candidate scores improve, counter is "
            "clean, and gate fires. Score velocity signal."
        ),
        steps=[
            ChainStep(step_number=1, event_type="candidate_scored",
                      condition="signal_count >= 2",
                      max_hours_after_anchor=0.0),
            ChainStep(step_number=2, event_type="candidate_scored",
                      condition="signal_count >= 3",
                      max_hours_after_anchor=4.0),
            ChainStep(step_number=3, event_type="counter_scored",
                      condition="risk_score < 50",
                      max_hours_after_anchor=6.0),
            ChainStep(step_number=4, event_type="conviction_gated",
                      max_hours_after_anchor=8.0),
        ],
        min_steps_to_trigger=3,
        conviction_boost=20,
        alert_priority="low",
    ),
]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _pattern_to_row(p: ChainPattern) -> tuple:
    steps_json = json.dumps([s.model_dump() for s in p.steps])
    return (
        p.name, p.description, steps_json, p.min_steps_to_trigger,
        p.conviction_boost, p.alert_priority,
        1 if p.is_active else 0,
    )


def _row_to_pattern(row) -> ChainPattern:
    steps_raw = json.loads(row["steps_json"])
    steps = [ChainStep(**s) for s in steps_raw]
    return ChainPattern(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        steps=steps,
        min_steps_to_trigger=row["min_steps_to_trigger"],
        conviction_boost=row["conviction_boost"],
        alert_priority=row["alert_priority"],
        is_active=bool(row["is_active"]),
        historical_hit_rate=row["historical_hit_rate"],
        total_triggers=row["total_triggers"] or 0,
        total_hits=row["total_hits"] or 0,
        created_at=datetime.fromisoformat(row["created_at"])
            if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"])
            if row["updated_at"] else None,
    )


async def seed_built_in_patterns(db: Database) -> int:
    """Insert BUILT_IN_PATTERNS if they are not already present.

    Returns number of new patterns inserted. Idempotent.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")
    async with conn.execute("SELECT name FROM chain_patterns") as cur:
        existing = {row["name"] for row in await cur.fetchall()}

    inserted = 0
    for pattern in BUILT_IN_PATTERNS:
        if pattern.name in existing:
            continue
        await conn.execute(
            """INSERT INTO chain_patterns
               (name, description, steps_json, min_steps_to_trigger,
                conviction_boost, alert_priority, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            _pattern_to_row(pattern),
        )
        inserted += 1
    await conn.commit()
    if inserted:
        logger.info("chains_seeded_built_in_patterns", count=inserted)
    return inserted


async def load_active_patterns(db: Database) -> list[ChainPattern]:
    """Load all active chain patterns from the database."""
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")
    async with conn.execute(
        """SELECT id, name, description, steps_json, min_steps_to_trigger,
                  conviction_boost, alert_priority, is_active,
                  historical_hit_rate, total_triggers, total_hits,
                  created_at, updated_at
           FROM chain_patterns
           WHERE is_active = 1"""
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_pattern(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_chains_patterns.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add scout/chains/patterns.py tests/test_chains_patterns.py
git commit -m "feat(chains): built-in patterns, condition evaluator, and DB seeding"
```

---

## Task 6: Pattern Matching Engine (`tracker.py`)

> **THIS IS THE CORE ALGORITHM.** Pay special attention to:
> 1. **Pipeline isolation** — events only match chains in the same pipeline.
> 2. **Event consumption rule** — an event consumed by one step of a chain cannot satisfy a later step of the same chain (critical for `volume_breakout`).
> 3. **Time-window matching by event timestamp, not arrival order** — out-of-order step arrivals must still match.
> 4. **Cooldown** — no new chain for (token, pattern) within `CHAIN_COOLDOWN_HOURS` of a prior completion.
> 5. **Expiry** — chains past `CHAIN_MAX_WINDOW_HOURS` are pruned.
> 6. **Deterministic ordering** — iterate events by `created_at ASC`, then by `id ASC` as tie-breaker.

**Files:**
- Create: `scout/chains/tracker.py`
- Test: `tests/test_chains_tracker.py`

- [ ] **Step 1: Write failing tests for the matching engine**

```python
# tests/test_chains_tracker.py
"""Tests for the chain matching engine."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.chains.events import emit_event
from scout.chains.patterns import BUILT_IN_PATTERNS, seed_built_in_patterns
from scout.chains.tracker import check_chains, get_active_boosts
from scout.config import Settings
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    await seed_built_in_patterns(d)
    yield d
    await d.close()


@pytest.fixture
def settings():
    return Settings(
        CHAIN_CHECK_INTERVAL_SEC=300,
        CHAIN_MAX_WINDOW_HOURS=24.0,
        CHAIN_COOLDOWN_HOURS=12.0,
        CHAIN_EVENT_RETENTION_DAYS=14,
        CHAIN_ACTIVE_RETENTION_DAYS=7,
        CHAIN_ALERT_ON_COMPLETE=False,
        CHAIN_TOTAL_BOOST_CAP=30,
        CHAINS_ENABLED=True,
    )


async def _insert_event_at(db, token_id, pipeline, event_type, data, when, source):
    """Helper that inserts a signal_event with a specific created_at."""
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (token_id, pipeline, event_type, json.dumps(data), source, when.isoformat()),
    )
    await db._conn.commit()


async def _count_matches(db, token_id=None):
    q = "SELECT COUNT(*) FROM chain_matches"
    params: tuple = ()
    if token_id:
        q += " WHERE token_id = ?"
        params = (token_id,)
    async with db._conn.execute(q, params) as cur:
        row = await cur.fetchone()
    return row[0]


async def test_no_events_no_chains(db, settings):
    await check_chains(db, settings)
    assert await _count_matches(db) == 0


async def test_chain_starts_on_anchor(db, settings):
    now = datetime.now(timezone.utc)
    await _insert_event_at(db, "cat-ai", "narrative", "category_heating",
                           {"acceleration": 8.0}, now, "narrative.observer")
    await check_chains(db, settings)
    async with db._conn.execute(
        "SELECT COUNT(*) FROM active_chains "
        "WHERE token_id='cat-ai' AND is_complete=0"
    ) as cur:
        row = await cur.fetchone()
    # Only full_conviction/narrative_momentum have category_heating as step 1;
    # we expect both to start.
    assert row[0] == 2


async def test_chain_advances_within_window(db, settings):
    now = datetime.now(timezone.utc)
    await _insert_event_at(db, "cat-ai", "narrative", "category_heating",
                           {"acceleration": 8.0}, now, "narrative.observer")
    await _insert_event_at(db, "cat-ai", "narrative", "laggard_picked",
                           {"category_id": "ai", "narrative_fit_score": 80,
                            "confidence": "High"},
                           now + timedelta(hours=1), "narrative.predictor")
    await check_chains(db, settings)
    async with db._conn.execute(
        "SELECT steps_matched FROM active_chains WHERE pattern_name='full_conviction'"
    ) as cur:
        row = await cur.fetchone()
    assert sorted(json.loads(row[0])) == [1, 2]


async def test_chain_rejects_late_step(db, settings):
    now = datetime.now(timezone.utc)
    await _insert_event_at(db, "cat-ai", "narrative", "category_heating",
                           {"acceleration": 8.0}, now, "narrative.observer")
    # laggard_picked arrives 10h later — step 2 window is 6h for full_conviction,
    # 4h for narrative_momentum. Both should reject.
    await _insert_event_at(db, "cat-ai", "narrative", "laggard_picked",
                           {"narrative_fit_score": 80, "confidence": "High"},
                           now + timedelta(hours=10), "narrative.predictor")
    await check_chains(db, settings)
    async with db._conn.execute(
        "SELECT steps_matched FROM active_chains WHERE pattern_name='full_conviction'"
    ) as cur:
        row = await cur.fetchone()
    assert json.loads(row[0]) == [1]


async def test_chain_rejects_failed_condition(db, settings):
    now = datetime.now(timezone.utc)
    # Step 1: category_heating (anchor)
    await _insert_event_at(db, "cat-ai", "narrative", "category_heating",
                           {"acceleration": 8.0}, now, "narrative.observer")
    # Step 2: a valid laggard_picked so the chain can reach step 3
    await _insert_event_at(db, "cat-ai", "narrative", "laggard_picked",
                           {"narrative_fit_score": 80, "confidence": "High"},
                           now + timedelta(hours=1), "narrative.predictor")
    # Step 3: narrative_scored with fit=50 must FAIL the ">70" condition
    # on narrative_momentum — so the chain stalls at steps_matched == [1, 2].
    await _insert_event_at(db, "cat-ai", "narrative", "narrative_scored",
                           {"narrative_fit_score": 50},
                           now + timedelta(hours=2), "narrative.predictor")
    await check_chains(db, settings)
    async with db._conn.execute(
        "SELECT steps_matched FROM active_chains WHERE pattern_name='narrative_momentum'"
    ) as cur:
        row = await cur.fetchone()
    assert json.loads(row[0]) == [1, 2]


async def test_chain_completes_and_emits(db, settings):
    """3 of 4 steps on full_conviction → completion + chain_complete event."""
    now = datetime.now(timezone.utc)
    await _insert_event_at(db, "cat-ai", "narrative", "category_heating",
                           {"acceleration": 8.0}, now, "narrative.observer")
    await _insert_event_at(db, "cat-ai", "narrative", "laggard_picked",
                           {"narrative_fit_score": 80, "confidence": "High"},
                           now + timedelta(hours=1), "narrative.predictor")
    await _insert_event_at(db, "cat-ai", "narrative", "counter_scored",
                           {"risk_score": 20, "flag_count": 0,
                            "high_severity_count": 0, "data_completeness": "full"},
                           now + timedelta(hours=2), "counter.scorer")
    await check_chains(db, settings)
    assert await _count_matches(db, "cat-ai") >= 1
    # chain_complete event was emitted
    async with db._conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE event_type='chain_complete'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] >= 1


async def test_pipeline_isolation(db, settings):
    """Same token_id string in different pipelines must not cross-match."""
    now = datetime.now(timezone.utc)
    # narrative anchor
    await _insert_event_at(db, "token-x", "narrative", "category_heating",
                           {"acceleration": 8.0}, now, "narrative.observer")
    # memecoin event with same id, would match a step type but different pipeline
    await _insert_event_at(db, "token-x", "memecoin", "laggard_picked",
                           {"narrative_fit_score": 80},
                           now + timedelta(hours=1), "narrative.predictor")
    await check_chains(db, settings)
    async with db._conn.execute(
        "SELECT steps_matched FROM active_chains WHERE pattern_name='full_conviction'"
    ) as cur:
        row = await cur.fetchone()
    # Only the anchor matched — cross-pipeline event was rejected
    assert row is not None and json.loads(row[0]) == [1]


async def test_event_consumption_rule(db, settings):
    """volume_breakout: one candidate_scored cannot satisfy both anchor AND step 2."""
    now = datetime.now(timezone.utc)
    # Single candidate_scored with signal_count=3 satisfies both step 1 (>=2)
    # and step 2 (>=3) grammatically, BUT must count as only one step.
    await _insert_event_at(db, "0xabc", "memecoin", "candidate_scored",
                           {"quant_score": 60, "signal_count": 3}, now, "scorer")
    await check_chains(db, settings)
    async with db._conn.execute(
        "SELECT steps_matched FROM active_chains WHERE pattern_name='volume_breakout'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert json.loads(row[0]) == [1]


async def test_volume_breakout_completes_with_two_candidate_events(db, settings):
    now = datetime.now(timezone.utc)
    # Two distinct candidate_scored events: the second must advance step 2.
    await _insert_event_at(db, "0xabc", "memecoin", "candidate_scored",
                           {"quant_score": 60, "signal_count": 2}, now, "scorer")
    await _insert_event_at(db, "0xabc", "memecoin", "candidate_scored",
                           {"quant_score": 72, "signal_count": 3},
                           now + timedelta(hours=1), "scorer")
    await _insert_event_at(db, "0xabc", "memecoin", "counter_scored",
                           {"risk_score": 25, "flag_count": 0,
                            "high_severity_count": 0, "data_completeness": "full"},
                           now + timedelta(hours=2), "counter.scorer")
    await check_chains(db, settings)
    assert await _count_matches(db, "0xabc") >= 1


async def test_out_of_order_step_arrival(db, settings):
    """Events inserted in non-chronological order must still match by timestamp.

    Narrative-pipeline scenario: in production the narrative agent can emit
    counter_scored before category_heating if the observer tick hasn't run
    yet, but the tracker sorts by `created_at` so the sequence still matches.
    """
    now = datetime.now(timezone.utc)
    # Insert step 3 first (3h after anchor), then step 1 (anchor at t=0),
    # then step 2 (t+1h). All within windows.
    await _insert_event_at(db, "cat-ai", "narrative", "counter_scored",
                           {"risk_score": 20, "flag_count": 0,
                            "high_severity_count": 0, "data_completeness": "full"},
                           now + timedelta(hours=3), "counter.scorer")
    await _insert_event_at(db, "cat-ai", "narrative", "category_heating",
                           {"acceleration": 8.0}, now, "narrative.observer")
    await _insert_event_at(db, "cat-ai", "narrative", "laggard_picked",
                           {"narrative_fit_score": 80, "confidence": "High"},
                           now + timedelta(hours=1), "narrative.predictor")
    await check_chains(db, settings)
    assert await _count_matches(db, "cat-ai") >= 1


async def test_chain_cooldown_blocks_retrigger(db, settings):
    """A completed chain cannot re-fire for same (token, pattern) within cooldown."""
    now = datetime.now(timezone.utc)
    # Complete once
    await _insert_event_at(db, "cat-ai", "narrative", "category_heating",
                           {"acceleration": 8.0}, now, "narrative.observer")
    await _insert_event_at(db, "cat-ai", "narrative", "laggard_picked",
                           {"narrative_fit_score": 80, "confidence": "High"},
                           now + timedelta(hours=1), "narrative.predictor")
    await _insert_event_at(db, "cat-ai", "narrative", "counter_scored",
                           {"risk_score": 20, "flag_count": 0,
                            "high_severity_count": 0, "data_completeness": "full"},
                           now + timedelta(hours=2), "counter.scorer")
    await check_chains(db, settings)
    first_count = await _count_matches(db, "cat-ai")
    assert first_count >= 1

    # Emit a fresh set of events 1h later — cooldown is 12h so no new match
    later = now + timedelta(hours=3)
    await _insert_event_at(db, "cat-ai", "narrative", "category_heating",
                           {"acceleration": 8.0}, later, "narrative.observer")
    await _insert_event_at(db, "cat-ai", "narrative", "laggard_picked",
                           {"narrative_fit_score": 80, "confidence": "High"},
                           later + timedelta(hours=1), "narrative.predictor")
    await _insert_event_at(db, "cat-ai", "narrative", "counter_scored",
                           {"risk_score": 20, "flag_count": 0,
                            "high_severity_count": 0, "data_completeness": "full"},
                           later + timedelta(hours=2), "counter.scorer")
    await check_chains(db, settings)
    assert await _count_matches(
        db, "cat-ai"
    ) == first_count, "Cooldown must prevent re-trigger"


async def test_chain_expiry(db, settings):
    """Active chain past max_window is deleted."""
    old = datetime.now(timezone.utc) - timedelta(hours=30)  # > 24h window
    # Insert an anchor (stale enough to be outside load_recent_events window too)
    await db._conn.execute(
        """INSERT INTO active_chains
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            step_events, anchor_time, last_step_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("cat-stale", "narrative", 1, "full_conviction", "[1]", "{\"1\": 1}",
         old.isoformat(), old.isoformat()),
    )
    await db._conn.commit()
    await check_chains(db, settings)
    async with db._conn.execute(
        "SELECT COUNT(*) FROM active_chains WHERE token_id='cat-stale'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == 0


async def test_get_active_boosts_caps_total(db, settings):
    """Two completed chains at +25 and +15 should cap at 30."""
    now = datetime.now(timezone.utc).isoformat()
    # Seed two matches manually
    for pname, boost in [("full_conviction", 25), ("narrative_momentum", 15)]:
        async with db._conn.execute(
            "SELECT id FROM chain_patterns WHERE name = ?", (pname,)
        ) as cur:
            pid = (await cur.fetchone())[0]
        await db._conn.execute(
            """INSERT INTO chain_matches
               (token_id, pipeline, pattern_id, pattern_name, steps_matched,
                total_steps, anchor_time, completed_at, chain_duration_hours,
                conviction_boost)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("0xabc", "memecoin", pid, pname, 3, 4, now, now, 4.0, boost),
        )
    await db._conn.commit()
    boost = await get_active_boosts(db, "0xabc", "memecoin", settings)
    assert boost == 30  # capped


async def test_get_active_boosts_expired_chains_ignored(db, settings):
    """A chain completed outside the cooldown window must not contribute."""
    old = (datetime.now(timezone.utc) - timedelta(hours=settings.CHAIN_COOLDOWN_HOURS + 1)).isoformat()
    async with db._conn.execute(
        "SELECT id FROM chain_patterns WHERE name = 'full_conviction'"
    ) as cur:
        pid = (await cur.fetchone())[0]
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("0xabc", "memecoin", pid, "full_conviction", 3, 4, old, old, 4.0, 25),
    )
    await db._conn.commit()
    boost = await get_active_boosts(db, "0xabc", "memecoin", settings)
    assert boost == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chains_tracker.py -v`
Expected: FAIL — `scout.chains.tracker` not found

- [ ] **Step 3: Implement `tracker.py`**

```python
# scout/chains/tracker.py
"""Chain tracker — pattern matching engine + main async loop + boost query.

Algorithm (per tick, inside check_chains):
  1. Load active patterns.
  2. Load recent events (last CHAIN_MAX_WINDOW_HOURS), ordered by (created_at, id).
  3. Load existing active_chains into memory.
  4. Group events by (token_id, pipeline).
  5. For each (token_id, pipeline) group:
       For each pattern:
         - Load or create the in-progress chain.
         - Walk events chronologically:
             * Skip events already consumed by this chain (by event id).
             * Try to advance the next unmatched step whose event_type matches
               AND whose condition passes AND whose time windows are satisfied.
             * An event may advance at most ONE step per chain.
         - After walking events, check completion (len(steps_matched) >= min).
         - Check expiry (now - anchor_time > CHAIN_MAX_WINDOW_HOURS).
         - Check cooldown BEFORE starting a new chain: if a ChainMatch for
           (token_id, pipeline, pattern_id) completed within CHAIN_COOLDOWN_HOURS,
           do not start a new chain at all.
  6. Persist updated / newly-created / completed / expired chains.
  7. For newly completed chains:
       - INSERT chain_matches row.
       - Emit `chain_complete` event.
       - If alert_priority in {"high","medium"} and CHAIN_ALERT_ON_COMPLETE:
           format_chain_alert + send via alerts module (best-effort).
  8. Prune old signal_events (retention) + stale active_chains.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Iterable

import structlog

from scout.chains.events import (
    emit_event,
    load_recent_events,
    prune_old_events,
    safe_emit,
)
from scout.chains.models import ActiveChain, ChainEvent, ChainPattern, ChainStep
from scout.chains.patterns import (
    evaluate_condition,
    load_active_patterns,
    seed_built_in_patterns,
)
from scout.config import Settings
from scout.db import Database

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_chain_tracker(db: Database, settings: Settings) -> None:
    """Main chain tracking loop — runs forever.

    Called from main.py as an asyncio task inside asyncio.gather().
    Never raises — all exceptions are logged and the loop continues.
    """
    await seed_built_in_patterns(db)
    logger.info(
        "chain_tracker_started",
        interval_sec=settings.CHAIN_CHECK_INTERVAL_SEC,
    )
    while True:
        try:
            await check_chains(db, settings)
        except Exception:
            logger.exception("chain_tracker_cycle_error")
        try:
            await asyncio.sleep(settings.CHAIN_CHECK_INTERVAL_SEC)
        except asyncio.CancelledError:
            logger.info("chain_tracker_cancelled")
            raise


# ---------------------------------------------------------------------------
# Core matching engine
# ---------------------------------------------------------------------------

async def check_chains(db: Database, settings: Settings) -> None:
    """One pass of the pattern matching engine."""
    patterns = await load_active_patterns(db)
    if not patterns:
        return

    events = await load_recent_events(db, max_hours=settings.CHAIN_MAX_WINDOW_HOURS)
    if not events:
        # Still do housekeeping even with no events
        await _prune_stale(db, settings)
        return

    # Deterministic order: timestamp then id
    events.sort(key=lambda e: (e.created_at, e.id or 0))

    # Group by (token_id, pipeline)
    groups: dict[tuple[str, str], list[ChainEvent]] = {}
    for ev in events:
        groups.setdefault((ev.token_id, ev.pipeline), []).append(ev)

    # Load all active chains once
    active_by_key = await _load_active_chains(db)

    now = datetime.now(timezone.utc)
    completed_chains: list[tuple[ActiveChain, ChainPattern]] = []

    for (token_id, pipeline), token_events in groups.items():
        for pattern in patterns:
            key = (token_id, pipeline, pattern.id)
            chain = active_by_key.get(key)

            # Expiry check for pre-existing chain
            if chain is not None and not chain.is_complete:
                age_h = (now - chain.anchor_time).total_seconds() / 3600.0
                if age_h > settings.CHAIN_MAX_WINDOW_HOURS:
                    await _delete_active_chain(db, chain)
                    active_by_key.pop(key, None)
                    chain = None
                    logger.info(
                        "chain_expired",
                        token_id=token_id,
                        pattern=pattern.name,
                    )

            # Skip entirely if a recent completion exists (cooldown)
            if chain is None and await _in_cooldown(db, token_id, pipeline, pattern, settings):
                continue

            # Advance or create
            chain, newly_complete = _advance_chain(
                chain, pattern, token_id, pipeline, token_events, now
            )
            if chain is None:
                continue

            active_by_key[key] = chain
            await _persist_active_chain(db, chain)

            if newly_complete:
                completed_chains.append((chain, pattern))

    # Record completions
    for chain, pattern in completed_chains:
        await _record_completion(db, chain, pattern, settings)

    await _prune_stale(db, settings)


def _advance_chain(
    chain: ActiveChain | None,
    pattern: ChainPattern,
    token_id: str,
    pipeline: str,
    events: list[ChainEvent],
    now: datetime,
) -> tuple[ActiveChain | None, bool]:
    """Try to advance (or start) a chain of the given pattern for this token.

    Returns (updated_or_new_chain, was_newly_completed).
    Returns (None, False) if no chain exists AND no anchor matched.
    """
    steps_by_number = {s.step_number: s for s in pattern.steps}
    total_steps = len(pattern.steps)

    # Existing chain that is already complete? Do nothing.
    if chain is not None and chain.is_complete:
        return chain, False

    # If no chain yet, try to start one from the earliest matching anchor.
    if chain is None:
        anchor_step = steps_by_number[1]
        for ev in events:
            if ev.event_type != anchor_step.event_type:
                continue
            try:
                if not evaluate_condition(anchor_step.condition, ev.event_data):
                    continue
            except ValueError:
                logger.warning(
                    "chain_invalid_condition",
                    pattern=pattern.name,
                    step=1,
                    condition=anchor_step.condition,
                )
                continue
            chain = ActiveChain(
                token_id=token_id,
                pipeline=pipeline,
                pattern_id=pattern.id or 0,
                pattern_name=pattern.name,
                steps_matched=[1],
                step_events={1: ev.id or 0},
                anchor_time=ev.created_at,
                last_step_time=ev.created_at,
                created_at=now,
            )
            break
        if chain is None:
            return None, False

    # Walk events chronologically and try to advance successive steps.
    # Event consumption: any event already stored in chain.step_events is skipped.
    consumed_ids = set(chain.step_events.values())
    advanced = True
    while advanced:
        advanced = False
        for ev in events:
            if ev.id in consumed_ids:
                continue
            # Try to advance any unmatched step whose event_type matches.
            # We iterate step_number ascending so earlier steps are filled first.
            for step_num in sorted(steps_by_number.keys()):
                if step_num in chain.steps_matched:
                    continue
                step = steps_by_number[step_num]
                if step.event_type != ev.event_type:
                    continue

                # Anchor-window check
                hours_from_anchor = (
                    ev.created_at - chain.anchor_time
                ).total_seconds() / 3600.0
                if hours_from_anchor < 0:
                    # Event pre-dates anchor — cannot satisfy a later step.
                    continue
                if hours_from_anchor > step.max_hours_after_anchor:
                    continue

                # Previous-step window check. Must be measured against the
                # PRIOR STEP's event timestamp (step_num - 1), NOT
                # chain.last_step_time. Out-of-order arrivals can push
                # last_step_time ahead of the immediately-prior step, which
                # would incorrectly reject valid events.
                if step.max_hours_after_previous is not None:
                    prior_event_id = chain.step_events.get(step_num - 1)
                    prior_ts: datetime | None = None
                    if prior_event_id is not None:
                        for prior_ev in events:
                            if prior_ev.id == prior_event_id:
                                prior_ts = prior_ev.created_at
                                break
                    if prior_ts is None:
                        # Prior step not yet matched — skip; we'll retry after
                        # it is advanced on a subsequent scan pass.
                        continue
                    hours_from_prev = (
                        ev.created_at - prior_ts
                    ).total_seconds() / 3600.0
                    if hours_from_prev < 0 or hours_from_prev > step.max_hours_after_previous:
                        continue

                # Condition check
                try:
                    if not evaluate_condition(step.condition, ev.event_data):
                        continue
                except ValueError:
                    logger.warning(
                        "chain_invalid_condition",
                        pattern=pattern.name,
                        step=step_num,
                    )
                    continue

                # Advance
                chain.steps_matched = sorted(chain.steps_matched + [step_num])
                chain.step_events[step_num] = ev.id or 0
                if ev.created_at > chain.last_step_time:
                    chain.last_step_time = ev.created_at
                consumed_ids.add(ev.id or 0)
                advanced = True
                logger.info(
                    "chain_step_matched",
                    token_id=token_id,
                    pattern=pattern.name,
                    step=step_num,
                )
                break  # this event is now consumed; restart inner loop
            if advanced:
                break  # re-scan events in case ordering matters

    newly_complete = False
    if (
        not chain.is_complete
        and len(chain.steps_matched) >= pattern.min_steps_to_trigger
    ):
        chain.is_complete = True
        chain.completed_at = now
        newly_complete = True
        logger.info(
            "chain_complete",
            token_id=chain.token_id,
            pattern=pattern.name,
            steps=len(chain.steps_matched),
            total=total_steps,
            duration_hours=round(
                (chain.last_step_time - chain.anchor_time).total_seconds() / 3600.0,
                2,
            ),
        )

    return chain, newly_complete


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

async def _load_active_chains(
    db: Database,
) -> dict[tuple[str, str, int], ActiveChain]:
    conn = db._conn
    async with conn.execute(
        """SELECT id, token_id, pipeline, pattern_id, pattern_name,
                  steps_matched, step_events, anchor_time, last_step_time,
                  is_complete, completed_at, created_at
           FROM active_chains
           WHERE is_complete = 0"""
    ) as cur:
        rows = await cur.fetchall()
    out: dict[tuple[str, str, int], ActiveChain] = {}
    for row in rows:
        chain = ActiveChain(
            id=row["id"],
            token_id=row["token_id"],
            pipeline=row["pipeline"],
            pattern_id=row["pattern_id"],
            pattern_name=row["pattern_name"],
            steps_matched=json.loads(row["steps_matched"]),
            step_events={int(k): v for k, v in json.loads(row["step_events"]).items()},
            anchor_time=datetime.fromisoformat(row["anchor_time"]),
            last_step_time=datetime.fromisoformat(row["last_step_time"]),
            is_complete=bool(row["is_complete"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"] else None
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
        out[(chain.token_id, chain.pipeline, chain.pattern_id)] = chain
    return out


async def _persist_active_chain(db: Database, chain: ActiveChain) -> None:
    conn = db._conn
    steps_json = json.dumps(chain.steps_matched)
    events_json = json.dumps({str(k): v for k, v in chain.step_events.items()})
    if chain.id is None:
        cursor = await conn.execute(
            """INSERT OR IGNORE INTO active_chains
               (token_id, pipeline, pattern_id, pattern_name,
                steps_matched, step_events, anchor_time, last_step_time,
                is_complete, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                chain.token_id, chain.pipeline, chain.pattern_id,
                chain.pattern_name, steps_json, events_json,
                chain.anchor_time.isoformat(), chain.last_step_time.isoformat(),
                1 if chain.is_complete else 0,
                chain.completed_at.isoformat() if chain.completed_at else None,
            ),
        )
        chain.id = cursor.lastrowid
    else:
        await conn.execute(
            """UPDATE active_chains
               SET steps_matched = ?, step_events = ?, last_step_time = ?,
                   is_complete = ?, completed_at = ?
               WHERE id = ?""",
            (
                steps_json, events_json, chain.last_step_time.isoformat(),
                1 if chain.is_complete else 0,
                chain.completed_at.isoformat() if chain.completed_at else None,
                chain.id,
            ),
        )
    await conn.commit()


async def _delete_active_chain(db: Database, chain: ActiveChain) -> None:
    if chain.id is None:
        return
    await db._conn.execute("DELETE FROM active_chains WHERE id = ?", (chain.id,))
    await db._conn.commit()


async def _in_cooldown(
    db: Database,
    token_id: str,
    pipeline: str,
    pattern: ChainPattern,
    settings: Settings,
) -> bool:
    """True if a completed chain for (token, pipeline, pattern) exists within cooldown."""
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(hours=settings.CHAIN_COOLDOWN_HOURS)
    ).isoformat()
    async with db._conn.execute(
        """SELECT 1 FROM chain_matches
           WHERE token_id = ? AND pipeline = ? AND pattern_id = ?
             AND completed_at >= ?
           LIMIT 1""",
        (token_id, pipeline, pattern.id, cutoff),
    ) as cur:
        row = await cur.fetchone()
    return row is not None


async def _record_completion(
    db: Database,
    chain: ActiveChain,
    pattern: ChainPattern,
    settings: Settings,
) -> None:
    """Write chain_matches row + emit chain_complete event + optional alert."""
    duration_h = (
        (chain.last_step_time - chain.anchor_time).total_seconds() / 3600.0
    )
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            chain.token_id, chain.pipeline, pattern.id, pattern.name,
            len(chain.steps_matched), len(pattern.steps),
            chain.anchor_time.isoformat(),
            (chain.completed_at or datetime.now(timezone.utc)).isoformat(),
            round(duration_h, 3),
            pattern.conviction_boost,
        ),
    )
    # Increment pattern trigger count
    await db._conn.execute(
        "UPDATE chain_patterns SET total_triggers = total_triggers + 1 "
        "WHERE id = ?",
        (pattern.id,),
    )
    await db._conn.commit()

    # Emit a chain_complete signal event (feeds back into the event store)
    await safe_emit(
        db,
        token_id=chain.token_id,
        pipeline=chain.pipeline,
        event_type="chain_complete",
        event_data={
            "pattern_name": pattern.name,
            "steps_matched": len(chain.steps_matched),
            "total_steps": len(pattern.steps),
            "conviction_boost": pattern.conviction_boost,
            "chain_duration_hours": round(duration_h, 3),
        },
        source_module="chains.tracker",
    )

    if settings.CHAIN_ALERT_ON_COMPLETE and pattern.alert_priority in ("high", "medium"):
        try:
            from scout.chains.alerts import send_chain_alert  # lazy import
            await send_chain_alert(db, chain, pattern, settings)
        except Exception:
            logger.exception("chain_alert_failed", pattern=pattern.name)


async def _prune_stale(db: Database, settings: Settings) -> None:
    """Prune old signal_events and stale/completed active_chains."""
    deleted_events = await prune_old_events(
        db, retention_days=settings.CHAIN_EVENT_RETENTION_DAYS
    )
    if deleted_events:
        logger.debug("chain_events_pruned", count=deleted_events)

    # Delete expired/completed active_chains older than CHAIN_ACTIVE_RETENTION_DAYS
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=settings.CHAIN_ACTIVE_RETENTION_DAYS)
    ).isoformat()
    cursor = await db._conn.execute(
        """DELETE FROM active_chains
           WHERE (is_complete = 1 AND completed_at < ?)
              OR (is_complete = 0 AND anchor_time < ?)""",
        (cutoff, cutoff),
    )
    await db._conn.commit()
    if cursor.rowcount:
        logger.debug("chain_active_pruned", count=cursor.rowcount)


# ---------------------------------------------------------------------------
# Boost query — consumed by the scoring pipeline
# ---------------------------------------------------------------------------

async def get_active_boosts(
    db: Database,
    token_id: str,
    pipeline: str,
    settings: Settings,
) -> int:
    """Return total conviction boost for a token, capped at CHAIN_TOTAL_BOOST_CAP.

    Sums conviction_boost from chain_matches completed within CHAIN_COOLDOWN_HOURS.
    """
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(hours=settings.CHAIN_COOLDOWN_HOURS)
    ).isoformat()
    async with db._conn.execute(
        """SELECT COALESCE(SUM(conviction_boost), 0) AS total
           FROM chain_matches
           WHERE token_id = ? AND pipeline = ? AND completed_at >= ?""",
        (token_id, pipeline, cutoff),
    ) as cur:
        row = await cur.fetchone()
    total = int(row[0] or 0)
    return min(total, settings.CHAIN_TOTAL_BOOST_CAP)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_chains_tracker.py -v`
Expected: All tests PASS. If any fail, STOP and re-read the matching algorithm — correctness here is load-bearing for every downstream consumer.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: No regressions.

- [ ] **Step 6: Commit**

```bash
git add scout/chains/tracker.py tests/test_chains_tracker.py
git commit -m "feat(chains): pattern matching engine with pipeline isolation, consumption, and boost query"
```

---

## Task 7: Alert Formatting

**Files:**
- Create: `scout/chains/alerts.py`
- Test: extend `tests/test_chains_tracker.py` with an alert formatting test (no external sends)

- [ ] **Step 1: Write failing test**

Create `tests/test_chains_alerts.py`:

```python
# tests/test_chains_alerts.py
"""Tests for chain alert formatting."""
from datetime import datetime, timedelta, timezone

import pytest

from scout.chains.alerts import format_chain_alert
from scout.chains.models import ActiveChain, ChainPattern, ChainStep


def _make_pattern() -> ChainPattern:
    return ChainPattern(
        id=1,
        name="full_conviction",
        description="test",
        steps=[
            ChainStep(step_number=1, event_type="category_heating",
                      max_hours_after_anchor=0.0),
            ChainStep(step_number=2, event_type="laggard_picked",
                      max_hours_after_anchor=6.0),
            ChainStep(step_number=3, event_type="counter_scored",
                      max_hours_after_anchor=8.0),
            ChainStep(step_number=4, event_type="candidate_scored",
                      max_hours_after_anchor=12.0),
        ],
        min_steps_to_trigger=3,
        conviction_boost=25,
        alert_priority="high",
        historical_hit_rate=0.42,
        total_triggers=17,
        total_hits=7,
    )


def test_format_chain_alert_contains_required_fields():
    now = datetime.now(timezone.utc)
    chain = ActiveChain(
        token_id="0xabc",
        pipeline="memecoin",
        pattern_id=1,
        pattern_name="full_conviction",
        steps_matched=[1, 2, 3],
        step_events={1: 10, 2: 11, 3: 12},
        anchor_time=now,
        last_step_time=now + timedelta(hours=3),
        is_complete=True,
        completed_at=now + timedelta(hours=3),
        created_at=now,
    )
    msg = format_chain_alert(chain, _make_pattern())
    assert "CONVICTION CHAIN COMPLETE" in msg
    assert "full_conviction" in msg
    assert "3/4" in msg
    assert "+25" in msg
    # Hit rate is displayed (42.0%)
    assert "42" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chains_alerts.py -v`
Expected: FAIL — `scout.chains.alerts` missing

- [ ] **Step 3: Implement `alerts.py`**

```python
# scout/chains/alerts.py
"""Format and send high-conviction chain alerts.

The actual Telegram delivery reuses scout.alerter's existing infrastructure.
This module only builds the message and invokes the send path best-effort.
"""

from __future__ import annotations

import structlog

from scout.chains.models import ActiveChain, ChainPattern
from scout.config import Settings
from scout.db import Database

logger = structlog.get_logger()


def format_chain_alert(chain: ActiveChain, pattern: ChainPattern) -> str:
    """Build a Telegram-ready chain completion message."""
    duration_h = (
        (chain.last_step_time - chain.anchor_time).total_seconds() / 3600.0
    )
    hit_rate_str = (
        f"{pattern.historical_hit_rate * 100:.1f}%"
        if pattern.historical_hit_rate is not None
        else "n/a"
    )
    lines = [
        "=== CONVICTION CHAIN COMPLETE ===",
        f"Pattern: {pattern.name} ({len(chain.steps_matched)}/{len(pattern.steps)} steps)",
        f"Token: {chain.token_id} ({chain.pipeline})",
        "",
        "Timeline:",
    ]
    for step_num in sorted(chain.steps_matched):
        step = next((s for s in pattern.steps if s.step_number == step_num), None)
        if step is None:
            continue
        lines.append(f"  step {step_num}: {step.event_type}")
    lines.extend([
        "",
        f"Chain duration: {duration_h:.2f}h",
        f"Historical hit rate: {hit_rate_str} ({pattern.total_triggers} prior triggers)",
        f"Conviction boost: +{pattern.conviction_boost} points",
    ])
    return "\n".join(lines)


async def send_chain_alert(
    db: Database,
    chain: ActiveChain,
    pattern: ChainPattern,
    settings: Settings,
) -> None:
    """Best-effort Telegram delivery. Never raises.

    Delegates to scout.alerter's existing Telegram helpers.
    """
    message = format_chain_alert(chain, pattern)
    try:
        # Reuse the existing raw-message send helper if available.
        from scout.alerter import send_telegram_message  # type: ignore
        await send_telegram_message(message, settings)
    except Exception:
        logger.exception(
            "chain_alert_send_failed",
            pattern=pattern.name,
            token_id=chain.token_id,
        )
```

> **Note:** If `scout.alerter` does not expose a raw `send_telegram_message(message, settings)` helper, add a thin wrapper there around the existing Telegram call. Do NOT change alert formatting of existing pipelines.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_chains_alerts.py tests/test_chains_tracker.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add scout/chains/alerts.py tests/test_chains_alerts.py
git commit -m "feat(chains): high-conviction chain alert formatting + send helper"
```

---

## Task 8: Wire Tracker into main.py + Boost Integration in gate.py

**Files:**
- Modify: `scout/main.py` (seed + gather)
- Modify: `scout/gate.py` (apply `get_active_boosts`)
- Test: `tests/test_chains_integration.py` (end-to-end)

- [ ] **Step 1: Write end-to-end integration test**

```python
# tests/test_chains_integration.py
"""End-to-end conviction chain integration test.

Emit a full sequence of events via the public emit_event API, run check_chains
once, and verify the chain completes, the match row exists, and
get_active_boosts returns the expected boosted value.
"""
import pytest

from scout.chains.events import emit_event
from scout.chains.patterns import seed_built_in_patterns
from scout.chains.tracker import check_chains, get_active_boosts
from scout.config import Settings
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    await seed_built_in_patterns(d)
    yield d
    await d.close()


@pytest.fixture
def settings():
    return Settings(
        CHAIN_CHECK_INTERVAL_SEC=300,
        CHAIN_MAX_WINDOW_HOURS=24.0,
        CHAIN_COOLDOWN_HOURS=12.0,
        CHAIN_EVENT_RETENTION_DAYS=14,
        CHAIN_ACTIVE_RETENTION_DAYS=7,
        CHAIN_ALERT_ON_COMPLETE=False,
        CHAIN_TOTAL_BOOST_CAP=30,
        CHAINS_ENABLED=True,
    )


async def test_full_conviction_e2e(db, settings):
    await emit_event(
        db, "cat-ai", "narrative", "category_heating",
        {"acceleration": 8.0, "volume_growth_pct": 40.0, "market_regime": "BULL"},
        "narrative.observer",
    )
    await emit_event(
        db, "cat-ai", "narrative", "laggard_picked",
        {"narrative_fit_score": 80, "confidence": "High", "trigger_count": 2},
        "narrative.predictor",
    )
    await emit_event(
        db, "cat-ai", "narrative", "counter_scored",
        {"risk_score": 20, "flag_count": 0, "high_severity_count": 0,
         "data_completeness": "full"},
        "counter.scorer",
    )
    await check_chains(db, settings)

    async with db._conn.execute(
        "SELECT COUNT(*) FROM chain_matches WHERE token_id='cat-ai'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] >= 1

    boost = await get_active_boosts(db, "cat-ai", "narrative", settings)
    assert boost >= 15  # at least narrative_momentum (+15), possibly more capped at 30
```

- [ ] **Step 2: Run it — expect PASS**

Run: `uv run pytest tests/test_chains_integration.py -v`
Expected: PASS (tracker implementation already written in Task 6).

- [ ] **Step 3: Wire the tracker into `scout/main.py`**

At the top of `scout/main.py`:

```python
from scout.chains.patterns import seed_built_in_patterns
from scout.chains.tracker import run_chain_tracker
```

In `run_cycle` / main startup (the `async with aiohttp.ClientSession()` block, just before the `tasks` list is built — around line 800):

```python
# Seed chain patterns once at startup (idempotent)
if settings.CHAINS_ENABLED:
    await seed_built_in_patterns(db)
```

Then append the tracker task to the `tasks` list beside the narrative agent:

```python
tasks: list[asyncio.Task] = [
    asyncio.create_task(_pipeline_loop()),
]
if settings.NARRATIVE_ENABLED:
    tasks.append(
        asyncio.create_task(narrative_agent_loop(session, settings, db))
    )
if settings.CHAINS_ENABLED:
    tasks.append(
        asyncio.create_task(run_chain_tracker(db, settings))
    )

await asyncio.gather(*tasks, return_exceptions=True)
```

- [ ] **Step 4: Integrate `get_active_boosts` into `scout/gate.py`**

Modify `scout/gate.py:evaluate()` so that AFTER the base conviction is computed but BEFORE the `should_alert` decision, the active chain boost is applied:

```python
# near top
from scout.chains.tracker import get_active_boosts

# inside evaluate(), after: conviction = (quant_score * QUANT_WEIGHT) + (narrative_score * NARRATIVE_WEIGHT) / fallback
chain_boost = 0
if getattr(settings, "CHAINS_ENABLED", False):
    try:
        chain_boost = await get_active_boosts(
            db, token.contract_address, "memecoin", settings
        )
    except Exception:
        logger.exception(
            "chain_boost_lookup_failed",
            contract_address=token.contract_address,
        )
        chain_boost = 0

boosted_conviction = min(100.0, float(conviction) + float(chain_boost))
should_alert = boosted_conviction >= settings.CONVICTION_THRESHOLD

updated = token.model_copy(update={
    "narrative_score": narrative_score,
    "conviction_score": boosted_conviction,
})

return (should_alert, boosted_conviction, updated)
```

> Keep the existing `conviction_gated` safe_emit call (added in Task 4) — but emit the **boosted** conviction so downstream chains see the final score.

- [ ] **Step 5: Add a gate-integration test**

Append to `tests/test_chains_integration.py`:

```python
async def test_gate_applies_chain_boost(db, settings, monkeypatch):
    """gate.evaluate adds get_active_boosts to the conviction score."""
    from datetime import datetime, timezone

    import scout.gate as gate_mod
    from scout.models import CandidateToken

    # Seed a completed chain manually for the token
    async with db._conn.execute(
        "SELECT id FROM chain_patterns WHERE name='full_conviction'"
    ) as cur:
        pid = (await cur.fetchone())[0]
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("0xabc", "memecoin", pid, "full_conviction", 3, 4, now_iso, now_iso, 2.0, 25),
    )
    await db._conn.commit()

    token = CandidateToken(
        contract_address="0xabc",
        chain="ethereum",
        token_name="Test",
        ticker="TST",
        quant_score=50,
        first_seen_at=datetime.now(timezone.utc),
    )

    # Stub _get_narrative_score to keep the test hermetic
    async def _no_narrative(*args, **kwargs):
        return None
    monkeypatch.setattr(gate_mod, "_get_narrative_score", _no_narrative)

    # Settings with CHAINS_ENABLED so gate applies the boost; low threshold so it alerts.
    settings.CHAINS_ENABLED = True
    settings.CONVICTION_THRESHOLD = 70
    settings.MIN_SCORE = 999  # skip narrative path entirely

    import aiohttp
    async with aiohttp.ClientSession() as session:
        should_alert, conviction, updated = await gate_mod.evaluate(
            token, db, session, settings, signals_fired=[]
        )

    assert conviction == 75.0  # 50 base + 25 boost
    assert should_alert is True
    assert updated.conviction_score == 75.0
```

- [ ] **Step 6: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests pass including the new integration + gate boost test.

- [ ] **Step 7: Dry-run the pipeline**

Run: `CHAINS_ENABLED=true uv run python -m scout.main --dry-run --cycles 1`
Expected: Clean exit, structured log lines `chain_tracker_started` visible, no tracebacks.

- [ ] **Step 8: Commit**

```bash
git add scout/main.py scout/gate.py tests/test_chains_integration.py
git commit -m "feat(chains): wire tracker into main.py and apply get_active_boosts in gate"
```

---

## Task 9: LEARN Phase Integration

> **Purpose:** Close the feedback loop. Compute per-pattern hit rates from
> `chain_matches.outcome_class`, promote patterns that are proving themselves
> (low → medium → high), and retire patterns that underperform the
> per-pipeline baseline. Runs daily alongside the narrative agent's existing
> LEARN phase on the same schedule — no new scheduler.

**Files:**
- Modify: `scout/chains/patterns.py` (add `compute_pattern_stats` + lifecycle logic)
- Modify: `scout/narrative/learn.py` (invoke chain learn step on its daily tick)
- Test: `tests/test_chains_learn.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_chains_learn.py
"""Tests for chain pattern LEARN phase: hit rate, promotion, retirement."""
from datetime import datetime, timezone

import pytest

from scout.chains.patterns import (
    compute_pattern_stats,
    run_pattern_lifecycle,
    seed_built_in_patterns,
)
from scout.config import Settings
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    await seed_built_in_patterns(d)
    yield d
    await d.close()


@pytest.fixture
def settings():
    return Settings(
        CHAINS_ENABLED=True,
        CHAIN_MIN_TRIGGERS_FOR_STATS=10,
        CHAIN_PROMOTION_THRESHOLD=0.45,
        CHAIN_GRADUATION_MIN_TRIGGERS=30,
        CHAIN_GRADUATION_HIT_RATE=0.55,
    )


async def _seed_matches(db, pattern_name, pipeline, n_hits, n_misses):
    async with db._conn.execute(
        "SELECT id FROM chain_patterns WHERE name = ?", (pattern_name,)
    ) as cur:
        pid = (await cur.fetchone())[0]
    now = datetime.now(timezone.utc).isoformat()
    for i in range(n_hits):
        await db._conn.execute(
            """INSERT INTO chain_matches
               (token_id, pipeline, pattern_id, pattern_name, steps_matched,
                total_steps, anchor_time, completed_at, chain_duration_hours,
                conviction_boost, outcome_class, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"tok-h{i}", pipeline, pid, pattern_name, 3, 4, now, now, 2.0, 25,
             "hit", now),
        )
    for i in range(n_misses):
        await db._conn.execute(
            """INSERT INTO chain_matches
               (token_id, pipeline, pattern_id, pattern_name, steps_matched,
                total_steps, anchor_time, completed_at, chain_duration_hours,
                conviction_boost, outcome_class, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"tok-m{i}", pipeline, pid, pattern_name, 3, 4, now, now, 2.0, 25,
             "miss", now),
        )
    await db._conn.commit()


async def test_pattern_hit_rate(db, settings):
    """compute_pattern_stats returns hit_rate = hits / (hits + misses)."""
    await _seed_matches(db, "full_conviction", "memecoin", n_hits=6, n_misses=4)
    stats = await compute_pattern_stats(db, settings)
    # Find the memecoin-pipeline entry for full_conviction
    fc = [s for s in stats if s["pattern_name"] == "full_conviction"
          and s["pipeline"] == "memecoin"][0]
    assert fc["total_evaluated"] == 10
    assert fc["hit_rate"] == pytest.approx(0.6, abs=1e-6)


async def test_pattern_hit_rate_per_pipeline_baseline(db, settings):
    """Hit rates must be computed independently per pipeline."""
    await _seed_matches(db, "full_conviction", "memecoin", n_hits=6, n_misses=4)
    await _seed_matches(db, "full_conviction", "narrative", n_hits=2, n_misses=8)
    stats = await compute_pattern_stats(db, settings)
    memes = [s for s in stats if s["pattern_name"] == "full_conviction"
             and s["pipeline"] == "memecoin"][0]
    narr = [s for s in stats if s["pattern_name"] == "full_conviction"
            and s["pipeline"] == "narrative"][0]
    assert memes["hit_rate"] == pytest.approx(0.6, abs=1e-6)
    assert narr["hit_rate"] == pytest.approx(0.2, abs=1e-6)


async def test_pattern_promotion(db, settings):
    """>=10 triggers AND >=45% hit rate promotes low → medium."""
    await _seed_matches(db, "full_conviction", "memecoin", n_hits=5, n_misses=5)
    await run_pattern_lifecycle(db, settings)
    async with db._conn.execute(
        "SELECT alert_priority FROM chain_patterns WHERE name='full_conviction'"
    ) as cur:
        prio = (await cur.fetchone())[0]
    assert prio == "medium"


async def test_pattern_graduation(db, settings):
    """>=30 triggers AND >=55% hit rate graduates medium → high."""
    # Start the pattern at medium
    await db._conn.execute(
        "UPDATE chain_patterns SET alert_priority='medium' WHERE name='full_conviction'"
    )
    await db._conn.commit()
    await _seed_matches(db, "full_conviction", "memecoin", n_hits=20, n_misses=15)
    await run_pattern_lifecycle(db, settings)
    async with db._conn.execute(
        "SELECT alert_priority FROM chain_patterns WHERE name='full_conviction'"
    ) as cur:
        prio = (await cur.fetchone())[0]
    assert prio == "high"


async def test_pattern_retirement(db, settings):
    """A pattern with >=MIN_TRIGGERS and hit rate <20% is marked inactive."""
    await _seed_matches(db, "full_conviction", "memecoin", n_hits=1, n_misses=14)
    await run_pattern_lifecycle(db, settings)
    async with db._conn.execute(
        "SELECT is_active FROM chain_patterns WHERE name='full_conviction'"
    ) as cur:
        active = (await cur.fetchone())[0]
    assert active == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chains_learn.py -v`
Expected: FAIL — `compute_pattern_stats` / `run_pattern_lifecycle` do not exist.

- [ ] **Step 3: Implement LEARN helpers in `scout/chains/patterns.py`**

Append:

```python
# ---------------------------------------------------------------------------
# LEARN phase: hit-rate computation + pattern lifecycle
# ---------------------------------------------------------------------------

# Baseline thresholds (below these a pattern is retired once it has enough data)
_RETIREMENT_HIT_RATE = 0.20


async def compute_pattern_stats(db: Database, settings: Settings) -> list[dict]:
    """Compute hit rate per (pattern, pipeline) over evaluated chain_matches.

    A match is considered evaluated iff `outcome_class` is non-null.
    Hit rate = count(outcome_class='hit') / count(evaluated).
    Patterns with fewer than CHAIN_MIN_TRIGGERS_FOR_STATS evaluated rows
    are still returned but flagged with `sufficient=False`.
    """
    conn = db._conn
    rows: list[dict] = []
    async with conn.execute(
        """SELECT pattern_id, pattern_name, pipeline,
                  COUNT(*) AS total_evaluated,
                  SUM(CASE WHEN outcome_class='hit' THEN 1 ELSE 0 END) AS hits
           FROM chain_matches
           WHERE outcome_class IS NOT NULL
           GROUP BY pattern_id, pattern_name, pipeline"""
    ) as cur:
        for row in await cur.fetchall():
            total = row["total_evaluated"] or 0
            hits = row["hits"] or 0
            rate = (hits / total) if total > 0 else 0.0
            rows.append({
                "pattern_id": row["pattern_id"],
                "pattern_name": row["pattern_name"],
                "pipeline": row["pipeline"],
                "total_evaluated": total,
                "hits": hits,
                "hit_rate": rate,
                "sufficient": total >= settings.CHAIN_MIN_TRIGGERS_FOR_STATS,
            })
    return rows


async def run_pattern_lifecycle(db: Database, settings: Settings) -> None:
    """Promote / graduate / retire chain patterns based on rolling stats.

    Lifecycle transitions (per pattern, aggregated across pipelines by
    taking the BEST performing pipeline as the promotion candidate):

      incubation (low)  →  medium   when triggers >= CHAIN_MIN_TRIGGERS_FOR_STATS
                                     AND hit_rate  >= CHAIN_PROMOTION_THRESHOLD
      medium            →  high     when triggers >= CHAIN_GRADUATION_MIN_TRIGGERS
                                     AND hit_rate  >= CHAIN_GRADUATION_HIT_RATE
      any               →  inactive when triggers >= CHAIN_MIN_TRIGGERS_FOR_STATS
                                     AND hit_rate  <  _RETIREMENT_HIT_RATE

    Also refreshes `historical_hit_rate`, `total_triggers`, `total_hits`
    on each pattern row from the aggregate stats.
    """
    stats = await compute_pattern_stats(db, settings)
    if not stats:
        return

    # Aggregate per pattern (best pipeline wins for promotion purposes)
    by_pattern: dict[int, dict] = {}
    for s in stats:
        pid = s["pattern_id"]
        cur_best = by_pattern.get(pid)
        if cur_best is None or s["hit_rate"] > cur_best["hit_rate"]:
            by_pattern[pid] = s

    conn = db._conn
    for pid, s in by_pattern.items():
        async with conn.execute(
            "SELECT alert_priority, is_active FROM chain_patterns WHERE id = ?",
            (pid,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            continue
        prio = row["alert_priority"]
        is_active = bool(row["is_active"])
        new_prio = prio
        new_active = is_active

        if s["total_evaluated"] >= settings.CHAIN_MIN_TRIGGERS_FOR_STATS:
            if s["hit_rate"] < _RETIREMENT_HIT_RATE:
                new_active = False
                logger.info(
                    "chain_pattern_retired",
                    pattern=s["pattern_name"],
                    hit_rate=s["hit_rate"],
                )
            elif prio == "low" and s["hit_rate"] >= settings.CHAIN_PROMOTION_THRESHOLD:
                new_prio = "medium"
                logger.info(
                    "chain_pattern_promoted",
                    pattern=s["pattern_name"],
                    from_priority="low",
                    to_priority="medium",
                    hit_rate=s["hit_rate"],
                )

        if (
            prio == "medium"
            and s["total_evaluated"] >= settings.CHAIN_GRADUATION_MIN_TRIGGERS
            and s["hit_rate"] >= settings.CHAIN_GRADUATION_HIT_RATE
        ):
            new_prio = "high"
            logger.info(
                "chain_pattern_graduated",
                pattern=s["pattern_name"],
                hit_rate=s["hit_rate"],
            )

        await conn.execute(
            """UPDATE chain_patterns
               SET alert_priority = ?,
                   is_active      = ?,
                   historical_hit_rate = ?,
                   total_triggers = ?,
                   total_hits     = ?,
                   updated_at     = datetime('now')
               WHERE id = ?""",
            (
                new_prio,
                1 if new_active else 0,
                s["hit_rate"],
                s["total_evaluated"],
                s["hits"],
                pid,
            ),
        )
    await conn.commit()
```

- [ ] **Step 4: Invoke `run_pattern_lifecycle` from the narrative LEARN tick**

In `scout/narrative/learn.py`, at the end of the daily LEARN phase function
(the same scheduled task that already recomputes narrative weights), add:

```python
# Chain patterns share the narrative agent's daily cadence — one scheduler
# already runs at this interval, so just piggyback here.
if getattr(settings, "CHAINS_ENABLED", False):
    try:
        from scout.chains.patterns import run_pattern_lifecycle
        await run_pattern_lifecycle(db, settings)
    except Exception:
        logger.exception("chain_learn_cycle_failed")
```

> If `scout/narrative/learn.py` does not exist yet (narrative agent is still
> rolling out), instead call `run_pattern_lifecycle` from `scout/main.py` on
> the same daily cadence as the narrative agent's LEARN tick — the goal is
> ONE scheduler, not two.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_chains_learn.py --tb=short -q`
Expected: All 5 tests PASS.

- [ ] **Step 6: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: No regressions.

- [ ] **Step 7: Commit**

```bash
git add scout/chains/patterns.py scout/narrative/learn.py tests/test_chains_learn.py
git commit -m "feat(chains): LEARN phase — hit-rate stats and pattern lifecycle (promote/graduate/retire)"
```

---

## Review checklist

- [ ] All tests pass: `uv run pytest --tb=short -q`
- [ ] `black scout/ tests/` clean
- [ ] Dry-run `--cycles 1` produces no tracebacks
- [ ] `CHAINS_ENABLED=false` path is a total no-op (no new rows in `signal_events` — verify via `sqlite3 scout.db "SELECT COUNT(*) FROM signal_events"` before/after)
- [ ] Pipeline isolation confirmed in tracker tests (`test_pipeline_isolation`)
- [ ] Event consumption rule confirmed (`test_event_consumption_rule`, `test_volume_breakout_completes_with_two_candidate_events`)
- [ ] Out-of-order arrival matches (`test_out_of_order_step_arrival`)
- [ ] Cooldown enforced (`test_chain_cooldown_blocks_retrigger`)
- [ ] Boost cap enforced (`test_get_active_boosts_caps_total`)
- [ ] No existing test regressed
