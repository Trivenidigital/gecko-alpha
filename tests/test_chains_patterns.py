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
