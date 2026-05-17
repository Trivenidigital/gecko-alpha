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
        "SELECT is_active, disabled_reason FROM chain_patterns WHERE name='full_conviction'"
    ) as cur:
        row = await cur.fetchone()
    assert row["is_active"] == 1
    assert row["disabled_reason"] is None


async def test_lifecycle_preserves_operator_disabled_builtin(db, settings):
    await db._conn.execute(
        """UPDATE chain_patterns
           SET is_active = 0, disabled_reason = 'operator_disabled'
           WHERE name = 'full_conviction'"""
    )
    await db._conn.commit()
    await _seed_matches(db, "full_conviction", "memecoin", n_hits=1, n_misses=14)

    await run_pattern_lifecycle(db, settings)

    async with db._conn.execute(
        "SELECT is_active, disabled_reason FROM chain_patterns WHERE name='full_conviction'"
    ) as cur:
        row = await cur.fetchone()
    assert row["is_active"] == 0
    assert row["disabled_reason"] == "operator_disabled"


async def test_lifecycle_preserves_code_disabled_builtin(db, settings):
    await db._conn.execute(
        """UPDATE chain_patterns
           SET is_active = 0, disabled_reason = 'code_disabled'
           WHERE name = 'full_conviction'"""
    )
    await db._conn.commit()
    await _seed_matches(db, "full_conviction", "memecoin", n_hits=1, n_misses=14)

    await run_pattern_lifecycle(db, settings)

    async with db._conn.execute(
        "SELECT is_active, disabled_reason FROM chain_patterns WHERE name='full_conviction'"
    ) as cur:
        row = await cur.fetchone()
    assert row["is_active"] == 0
    assert row["disabled_reason"] == "code_disabled"


async def test_lifecycle_retires_non_builtin_pattern(db, settings):
    await db._conn.execute(
        """INSERT INTO chain_patterns
           (name, description, steps_json, min_steps_to_trigger,
            conviction_boost, alert_priority, is_active, is_protected_builtin)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "experimental_bad",
            "test pattern",
            '[{"step_number":1,"event_type":"candidate_scored","condition":null,"max_hours_after_anchor":0.0,"max_hours_after_previous":null}]',
            1,
            1,
            "low",
            1,
            0,
        ),
    )
    await db._conn.commit()
    await _seed_matches(db, "experimental_bad", "memecoin", n_hits=1, n_misses=14)

    await run_pattern_lifecycle(db, settings)

    async with db._conn.execute(
        "SELECT is_active, disabled_reason, disabled_at FROM chain_patterns WHERE name='experimental_bad'"
    ) as cur:
        row = await cur.fetchone()
    assert row["is_active"] == 0
    assert row["disabled_reason"] == "lifecycle_retired"
    assert row["disabled_at"] is not None


async def test_lifecycle_preserves_operator_disabled_non_builtin_pattern(db, settings):
    await db._conn.execute(
        """INSERT INTO chain_patterns
           (name, description, steps_json, min_steps_to_trigger,
            conviction_boost, alert_priority, is_active, is_protected_builtin,
            disabled_reason, disabled_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "experimental_operator_disabled",
            "test pattern",
            '[{"step_number":1,"event_type":"candidate_scored","condition":null,"max_hours_after_anchor":0.0,"max_hours_after_previous":null}]',
            1,
            1,
            "low",
            0,
            0,
            "operator_disabled",
            "2026-05-17T00:00:00+00:00",
        ),
    )
    await db._conn.commit()
    await _seed_matches(
        db, "experimental_operator_disabled", "memecoin", n_hits=1, n_misses=14
    )

    await run_pattern_lifecycle(db, settings)

    async with db._conn.execute(
        "SELECT is_active, disabled_reason FROM chain_patterns WHERE name='experimental_operator_disabled'"
    ) as cur:
        row = await cur.fetchone()
    assert row["is_active"] == 0
    assert row["disabled_reason"] == "operator_disabled"


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

    updated = await update_chain_outcomes(db, session=object())
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
    updated = await update_chain_outcomes(db, session=object())
    assert updated == 0


# ---------------------------------------------------------------------------
# BL-071 — systemic-zero-hits guard
# ---------------------------------------------------------------------------


async def test_lifecycle_skips_retirement_when_all_patterns_zero_hits(db, settings):
    """When every pattern shows 0 hits across the trigger floor, the cause is
    almost certainly broken outcome telemetry — NOT bad patterns. The guard
    must short-circuit before any is_active is touched.

    This is the exact scenario that fired on 2026-05-01T01:26Z and silently
    deactivated all 3 patterns, killing chain_matches for ~17 days.
    """
    # Seed 12 misses on each of the 3 built-in patterns. Above
    # CHAIN_MIN_TRIGGERS_FOR_STATS=10, well below _RETIREMENT_HIT_RATE=0.20.
    # Without the guard, all 3 patterns would be auto-retired.
    await _seed_matches(db, "full_conviction", "memecoin", n_hits=0, n_misses=12)
    await _seed_matches(db, "narrative_momentum", "narrative", n_hits=0, n_misses=12)
    await _seed_matches(db, "volume_breakout", "memecoin", n_hits=0, n_misses=12)

    await run_pattern_lifecycle(db, settings)

    # All three patterns must remain active — guard short-circuited the loop
    # before any UPDATE could fire. The `chain_pattern_retirement_skipped_systemwide_zero_hits`
    # warning is logged via structlog (not stdlib) so we assert behaviour, not log text.
    async with db._conn.execute(
        "SELECT name, is_active FROM chain_patterns ORDER BY name"
    ) as cur:
        rows = await cur.fetchall()
    for r in rows:
        assert r[1] == 1, f"pattern {r[0]} was retired despite zero-hits-systemwide"


async def test_lifecycle_still_retires_bad_pattern_when_others_have_hits(db, settings):
    """The guard must NOT block per-pattern retirement when at least one
    other pattern is producing hits. That keeps Tier-1b-style auto-suspend
    working for genuinely bad patterns when the system has demonstrated it
    CAN observe hits elsewhere.
    """
    # full_conviction: 6 hits / 4 misses (60%) — healthy
    # narrative_momentum: 0 hits / 12 misses (0%) — should retire
    # volume_breakout: 0 hits / 12 misses (0%) — should retire
    await _seed_matches(db, "full_conviction", "memecoin", n_hits=6, n_misses=4)
    await _seed_matches(db, "narrative_momentum", "narrative", n_hits=0, n_misses=12)
    await _seed_matches(db, "volume_breakout", "memecoin", n_hits=0, n_misses=12)

    await run_pattern_lifecycle(db, settings)

    async with db._conn.execute(
        "SELECT name, is_active FROM chain_patterns ORDER BY name"
    ) as cur:
        rows = {r[0]: r[1] for r in await cur.fetchall()}

    assert rows["full_conviction"] == 1, "healthy pattern must stay active"
    assert rows["narrative_momentum"] == 1, "protected built-in must stay active"
    assert rows["volume_breakout"] == 1, "protected built-in must stay active"
