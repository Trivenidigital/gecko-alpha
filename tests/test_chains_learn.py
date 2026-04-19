"""Tests for chain pattern LEARN phase: hit rate, promotion, retirement."""

from datetime import datetime, timedelta, timezone

import pytest

from scout.chains.patterns import (
    compute_pattern_stats,
    run_pattern_lifecycle,
    seed_built_in_patterns,
)
from scout.chains.tracker import update_chain_outcomes
from scout.db import Database

_LEARN_DEFAULTS = dict(
    CHAINS_ENABLED=True,
    CHAIN_MIN_TRIGGERS_FOR_STATS=10,
    CHAIN_PROMOTION_THRESHOLD=0.45,
    CHAIN_GRADUATION_MIN_TRIGGERS=30,
    CHAIN_GRADUATION_HIT_RATE=0.55,
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
    return settings_factory(**_LEARN_DEFAULTS)


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
            (
                f"tok-h{i}",
                pipeline,
                pid,
                pattern_name,
                3,
                4,
                now,
                now,
                2.0,
                25,
                "hit",
                now,
            ),
        )
    for i in range(n_misses):
        await db._conn.execute(
            """INSERT INTO chain_matches
               (token_id, pipeline, pattern_id, pattern_name, steps_matched,
                total_steps, anchor_time, completed_at, chain_duration_hours,
                conviction_boost, outcome_class, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"tok-m{i}",
                pipeline,
                pid,
                pattern_name,
                3,
                4,
                now,
                now,
                2.0,
                25,
                "miss",
                now,
            ),
        )
    await db._conn.commit()


async def test_pattern_hit_rate(db, settings):
    await _seed_matches(db, "full_conviction", "memecoin", n_hits=6, n_misses=4)
    stats = await compute_pattern_stats(db, settings)
    fc = [
        s
        for s in stats
        if s["pattern_name"] == "full_conviction" and s["pipeline"] == "memecoin"
    ][0]
    assert fc["total_evaluated"] == 10
    assert fc["hit_rate"] == pytest.approx(0.6, abs=1e-6)


async def test_pattern_hit_rate_per_pipeline_baseline(db, settings):
    await _seed_matches(db, "full_conviction", "memecoin", n_hits=6, n_misses=4)
    await _seed_matches(db, "full_conviction", "narrative", n_hits=2, n_misses=8)
    stats = await compute_pattern_stats(db, settings)
    memes = [
        s
        for s in stats
        if s["pattern_name"] == "full_conviction" and s["pipeline"] == "memecoin"
    ][0]
    narr = [
        s
        for s in stats
        if s["pattern_name"] == "full_conviction" and s["pipeline"] == "narrative"
    ][0]
    assert memes["hit_rate"] == pytest.approx(0.6, abs=1e-6)
    assert narr["hit_rate"] == pytest.approx(0.2, abs=1e-6)


async def test_pattern_promotion(db, settings):
    await _seed_matches(db, "full_conviction", "memecoin", n_hits=5, n_misses=5)
    await run_pattern_lifecycle(db, settings)
    async with db._conn.execute(
        "SELECT alert_priority FROM chain_patterns WHERE name='full_conviction'"
    ) as cur:
        prio = (await cur.fetchone())[0]
    assert prio == "medium"


async def test_pattern_graduation(db, settings):
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
    await _seed_matches(db, "full_conviction", "memecoin", n_hits=1, n_misses=14)
    await run_pattern_lifecycle(db, settings)
    async with db._conn.execute(
        "SELECT is_active FROM chain_patterns WHERE name='full_conviction'"
    ) as cur:
        active = (await cur.fetchone())[0]
    assert active == 0


async def test_update_chain_outcomes_from_predictions(db, settings):
    """Chain outcomes (narrative pipeline) are hydrated from predictions."""
    async with db._conn.execute(
        "SELECT id FROM chain_patterns WHERE name='full_conviction'"
    ) as cur:
        pid = (await cur.fetchone())[0]

    coin_id = "hydrated-coin-1"
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Insert a prediction with a realized HIT outcome for this coin.
    await db._conn.execute(
        """INSERT INTO predictions
           (category_id, category_name, coin_id, symbol, name,
            market_cap_at_prediction, price_at_prediction,
            narrative_fit_score, staying_power, confidence, reasoning,
            strategy_snapshot, predicted_at, outcome_class, evaluated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "cat-1",
            "Cat One",
            coin_id,
            "HYD",
            "Hydrated",
            1_000_000.0,
            0.01,
            80,
            "STRONG",
            "HIGH",
            "reason",
            "{}",
            old_ts,
            "HIT",
            now_iso,
        ),
    )
    # Insert a stale (>48h) chain_match for the same coin with NULL outcome.
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            coin_id,
            "narrative",
            pid,
            "full_conviction",
            3,
            4,
            old_ts,
            old_ts,
            2.0,
            25,
        ),
    )
    await db._conn.commit()

    updated = await update_chain_outcomes(db)
    assert updated == 1

    async with db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id = ?",
        (coin_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "hit"


async def test_update_chain_outcomes_skips_recent(db, settings):
    """Chain matches younger than 48h are not hydrated yet."""
    async with db._conn.execute(
        "SELECT id FROM chain_patterns WHERE name='full_conviction'"
    ) as cur:
        pid = (await cur.fetchone())[0]

    recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "recent-coin",
            "narrative",
            pid,
            "full_conviction",
            3,
            4,
            recent_ts,
            recent_ts,
            1.0,
            25,
        ),
    )
    await db._conn.commit()
    updated = await update_chain_outcomes(db)
    assert updated == 0
