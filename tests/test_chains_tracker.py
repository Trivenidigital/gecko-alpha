"""Tests for the chain matching engine."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.chains.patterns import seed_built_in_patterns
from scout.chains.tracker import check_chains, get_active_boosts
from scout.db import Database


_TRACKER_DEFAULTS = dict(
    CHAIN_CHECK_INTERVAL_SEC=300,
    CHAIN_MAX_WINDOW_HOURS=24.0,
    CHAIN_COOLDOWN_HOURS=12.0,
    CHAIN_EVENT_RETENTION_DAYS=14,
    CHAIN_ACTIVE_RETENTION_DAYS=7,
    CHAIN_ALERT_ON_COMPLETE=False,
    CHAIN_TOTAL_BOOST_CAP=30,
    CHAINS_ENABLED=True,
)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    await seed_built_in_patterns(d)
    yield d
    await d.close()


@pytest.fixture
def settings(settings_factory):
    return settings_factory(**_TRACKER_DEFAULTS)


@pytest.fixture(autouse=True)
def _patch_get_settings(monkeypatch, settings_factory):
    """Ensure safe_emit sees CHAINS_ENABLED=True so chain_complete events fire."""
    s = settings_factory(**_TRACKER_DEFAULTS)
    monkeypatch.setattr("scout.config.get_settings", lambda: s)


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
    await _insert_event_at(db, "cat-ai", "narrative", "category_heating",
                           {"acceleration": 8.0}, now, "narrative.observer")
    await _insert_event_at(db, "cat-ai", "narrative", "laggard_picked",
                           {"narrative_fit_score": 80, "confidence": "High"},
                           now + timedelta(hours=1), "narrative.predictor")
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
    async with db._conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE event_type='chain_complete'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] >= 1


async def test_pipeline_isolation(db, settings):
    now = datetime.now(timezone.utc)
    await _insert_event_at(db, "token-x", "narrative", "category_heating",
                           {"acceleration": 8.0}, now, "narrative.observer")
    await _insert_event_at(db, "token-x", "memecoin", "laggard_picked",
                           {"narrative_fit_score": 80},
                           now + timedelta(hours=1), "narrative.predictor")
    await check_chains(db, settings)
    async with db._conn.execute(
        "SELECT steps_matched FROM active_chains WHERE pattern_name='full_conviction'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and json.loads(row[0]) == [1]


async def test_event_consumption_rule(db, settings):
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)
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
    first_count = await _count_matches(db, "cat-ai")
    assert first_count >= 1

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
    assert await _count_matches(db, "cat-ai") == first_count


async def test_chain_expiry(db, settings):
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    await db._conn.execute(
        """INSERT INTO active_chains
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            step_events, anchor_time, last_step_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("cat-stale", "narrative", 1, "full_conviction", "[1]", "{\"1\": 1}",
         old.isoformat(), old.isoformat()),
    )
    await db._conn.commit()
    # Need a fresh event for cat-stale to re-visit the chain in check_chains
    await _insert_event_at(
        db, "cat-stale", "narrative", "category_heating",
        {"acceleration": 8.0}, datetime.now(timezone.utc), "narrative.observer",
    )
    await check_chains(db, settings)
    async with db._conn.execute(
        "SELECT COUNT(*) FROM active_chains WHERE token_id='cat-stale' AND pattern_name='full_conviction' AND anchor_time=?",
        (old.isoformat(),),
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == 0


async def test_get_active_boosts_caps_total(db, settings):
    now = datetime.now(timezone.utc).isoformat()
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
    assert boost == 30


async def test_get_active_boosts_expired_chains_ignored(db, settings):
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
