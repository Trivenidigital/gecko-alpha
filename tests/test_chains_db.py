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
