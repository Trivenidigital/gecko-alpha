import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scout.chains.patterns import seed_built_in_patterns
from scout.db import Database
from scripts.check_chain_anchor_health import check_chain_anchor_health


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "watchdog.db")
    await d.initialize()
    await seed_built_in_patterns(d)
    yield d
    await d.close()


def _write_env(path: Path, *, chains_enabled: bool = True) -> Path:
    path.write_text(
        f"CHAINS_ENABLED={'true' if chains_enabled else 'false'}\n",
        encoding="utf-8",
    )
    return path


async def _insert_signal_event(db, *, token_id, pipeline, event_type, data, when):
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            pipeline,
            event_type,
            json.dumps(data),
            "test",
            when.isoformat(),
        ),
    )
    await db._conn.commit()


async def _insert_active_chain(db, *, token_id, pipeline, pattern_name, when):
    async with db._conn.execute(
        "SELECT id FROM chain_patterns WHERE name=?", (pattern_name,)
    ) as cur:
        pattern_id = (await cur.fetchone())["id"]
    await db._conn.execute(
        """INSERT INTO active_chains
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            step_events, anchor_time, last_step_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            pipeline,
            pattern_id,
            pattern_name,
            "[1]",
            '{"1": 1}',
            when.isoformat(),
            when.isoformat(),
        ),
    )
    await db._conn.commit()


async def test_watchdog_disabled_when_chains_disabled(db, tmp_path):
    env = _write_env(tmp_path / ".env", chains_enabled=False)

    result = check_chain_anchor_health(db._db_path, env_path=env)

    assert result["ok"] is True
    assert result["status"] == "disabled"


async def test_watchdog_ok_with_recent_anchor_and_active_chain(db, tmp_path):
    env = _write_env(tmp_path / ".env")
    now = datetime.now(timezone.utc)
    await _insert_signal_event(
        db,
        token_id="0xabc",
        pipeline="memecoin",
        event_type="candidate_scored",
        data={"signal_count": 2},
        when=now,
    )
    await _insert_active_chain(
        db,
        token_id="0xabc",
        pipeline="memecoin",
        pattern_name="volume_breakout",
        when=now,
    )

    result = check_chain_anchor_health(db._db_path, env_path=env)

    assert result["ok"] is True
    assert result["recent_anchor_events"] == 1


async def test_watchdog_fails_when_no_active_protected_patterns(db, tmp_path):
    env = _write_env(tmp_path / ".env")
    await db._conn.execute("UPDATE chain_patterns SET is_active=0")
    await db._conn.commit()

    result = check_chain_anchor_health(db._db_path, env_path=env)

    assert result["ok"] is False
    assert "no_active_protected_patterns" in result["reasons"]


async def test_watchdog_fails_when_anchor_eligible_events_but_active_chains_stale(
    db, tmp_path
):
    env = _write_env(tmp_path / ".env")
    now = datetime.now(timezone.utc)
    stale = now - timedelta(hours=30)
    await _insert_signal_event(
        db,
        token_id="cat-ai",
        pipeline="narrative",
        event_type="category_heating",
        data={"acceleration": 8},
        when=now,
    )
    await _insert_active_chain(
        db,
        token_id="old",
        pipeline="narrative",
        pattern_name="full_conviction",
        when=stale,
    )

    result = check_chain_anchor_health(
        db._db_path,
        env_path=env,
        active_stale_hours=24,
    )

    assert result["ok"] is False
    assert "active_chains_stale" in result["reasons"]
    assert "narrative:full_conviction" in result["active_chains_stale_keys"]


async def test_watchdog_fails_when_one_pattern_stale_even_if_another_is_fresh(
    db, tmp_path
):
    env = _write_env(tmp_path / ".env")
    now = datetime.now(timezone.utc)
    stale = now - timedelta(hours=30)
    await _insert_signal_event(
        db,
        token_id="cat-ai",
        pipeline="narrative",
        event_type="category_heating",
        data={"acceleration": 8},
        when=now,
    )
    await _insert_signal_event(
        db,
        token_id="0xabc",
        pipeline="memecoin",
        event_type="candidate_scored",
        data={"signal_count": 2},
        when=now,
    )
    await _insert_active_chain(
        db,
        token_id="0xabc",
        pipeline="memecoin",
        pattern_name="volume_breakout",
        when=now,
    )
    await _insert_active_chain(
        db,
        token_id="cat-old",
        pipeline="narrative",
        pattern_name="full_conviction",
        when=stale,
    )

    result = check_chain_anchor_health(
        db._db_path,
        env_path=env,
        active_stale_hours=24,
    )

    assert result["ok"] is False
    assert "active_chains_stale" in result["reasons"]
    assert "narrative:full_conviction" in result["active_chains_stale_keys"]
    assert "memecoin:volume_breakout" not in result["active_chains_stale_keys"]


async def test_watchdog_ignores_memecoin_candidate_below_anchor_condition(db, tmp_path):
    env = _write_env(tmp_path / ".env")
    now = datetime.now(timezone.utc)
    await _insert_signal_event(
        db,
        token_id="0xabc",
        pipeline="memecoin",
        event_type="candidate_scored",
        data={"signal_count": 1},
        when=now,
    )

    result = check_chain_anchor_health(db._db_path, env_path=env)

    assert result["ok"] is True
    assert result["recent_anchor_events"] == 0


async def test_watchdog_missing_db_does_not_create_file(tmp_path):
    env = _write_env(tmp_path / ".env")
    missing = tmp_path / "missing.db"

    result = check_chain_anchor_health(missing, env_path=env)

    assert result["ok"] is False
    assert "db_missing" in result["reasons"]
    assert not missing.exists()


async def test_watchdog_schema_pending_is_ok_before_migration(tmp_path):
    import sqlite3

    env = _write_env(tmp_path / ".env")
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE chain_patterns (
                id INTEGER PRIMARY KEY,
                name TEXT,
                steps_json TEXT,
                is_active INTEGER
            )"""
        )

    result = check_chain_anchor_health(path, env_path=env)

    assert result["ok"] is True
    assert result["status"] == "schema_pending"
