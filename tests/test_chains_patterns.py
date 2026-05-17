"""Tests for chain pattern definitions, condition evaluator, and seeding."""

import pytest

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
    assert (
        evaluate_condition("narrative_fit_score > 70", {"narrative_fit_score": 71})
        is True
    )
    assert (
        evaluate_condition("narrative_fit_score > 70", {"narrative_fit_score": 70})
        is False
    )


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
        assert p.steps[0].max_hours_after_anchor == 0.0


async def test_seed_built_in_patterns_idempotent(db):
    await seed_built_in_patterns(db)
    await seed_built_in_patterns(db)
    async with db._conn.execute("SELECT COUNT(*) FROM chain_patterns") as cur:
        row = await cur.fetchone()
    assert row[0] == len(BUILT_IN_PATTERNS)


async def test_load_active_patterns_skips_inactive(db):
    await seed_built_in_patterns(db)
    await db._conn.execute(
        "UPDATE chain_patterns SET is_active = 0 WHERE name = 'narrative_momentum'"
    )
    await db._conn.commit()
    patterns = await load_active_patterns(db)
    names = [p.name for p in patterns]
    assert "narrative_momentum" not in names
    assert "full_conviction" in names
    full = next(p for p in patterns if p.name == "full_conviction")
    assert len(full.steps) == 4
    assert full.steps[0].event_type == "category_heating"


async def test_seed_reactivates_legacy_lifecycle_retired_builtin(db):
    await seed_built_in_patterns(db)
    await db._conn.execute(
        """UPDATE chain_patterns
           SET is_active = 0,
               disabled_reason = 'legacy_lifecycle_retired',
               disabled_at = '2026-05-17T01:24:59+00:00'
           WHERE name = 'full_conviction'"""
    )
    await db._conn.commit()

    await seed_built_in_patterns(db)

    async with db._conn.execute(
        """SELECT is_active, disabled_reason, disabled_at, is_protected_builtin
           FROM chain_patterns WHERE name = 'full_conviction'"""
    ) as cur:
        row = await cur.fetchone()
    assert row["is_active"] == 1
    assert row["disabled_reason"] is None
    assert row["disabled_at"] is None
    assert row["is_protected_builtin"] == 1


async def test_seed_preserves_operator_disabled_builtin(db):
    await seed_built_in_patterns(db)
    await db._conn.execute(
        """UPDATE chain_patterns
           SET is_active = 0,
               disabled_reason = 'operator_disabled',
               disabled_at = '2026-05-17T01:24:59+00:00'
           WHERE name = 'full_conviction'"""
    )
    await db._conn.commit()

    await seed_built_in_patterns(db)

    async with db._conn.execute(
        "SELECT is_active, disabled_reason FROM chain_patterns WHERE name = 'full_conviction'"
    ) as cur:
        row = await cur.fetchone()
    assert row["is_active"] == 0
    assert row["disabled_reason"] == "operator_disabled"


async def test_seed_preserves_learned_alert_priority(db):
    await seed_built_in_patterns(db)
    await db._conn.execute(
        "UPDATE chain_patterns SET alert_priority = 'high' WHERE name = 'full_conviction'"
    )
    await db._conn.commit()

    await seed_built_in_patterns(db)

    async with db._conn.execute(
        "SELECT alert_priority FROM chain_patterns WHERE name = 'full_conviction'"
    ) as cur:
        row = await cur.fetchone()
    assert row["alert_priority"] == "high"


async def test_seed_syncs_builtin_steps_json(db):
    await seed_built_in_patterns(db)
    await db._conn.execute(
        """UPDATE chain_patterns
           SET steps_json = '[{"step_number":1,"event_type":"broken","condition":null,"max_hours_after_anchor":0.0,"max_hours_after_previous":null}]'
           WHERE name = 'full_conviction'"""
    )
    await db._conn.commit()

    await seed_built_in_patterns(db)

    patterns = await load_active_patterns(db)
    full = next(p for p in patterns if p.name == "full_conviction")
    assert [step.event_type for step in full.steps] == [
        "category_heating",
        "laggard_picked",
        "counter_scored",
        "candidate_scored",
    ]
