"""Tests for the narrative Strategy manager."""

from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.narrative.strategy import Strategy, STRATEGY_DEFAULTS


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
async def strategy(db: Database):
    s = Strategy(db)
    await s.load_or_init()
    return s


async def test_init_seeds_defaults(strategy: Strategy):
    """After load_or_init, default values are available."""
    assert strategy.get("hit_threshold_pct") == 15.0


async def test_get_returns_typed_value(strategy: Strategy):
    """get() returns properly typed Python values."""
    val = strategy.get("hit_threshold_pct")
    assert isinstance(val, float)


async def test_set_respects_bounds(strategy: Strategy):
    """Setting a value outside bounds raises ValueError."""
    with pytest.raises(ValueError, match="out of bounds"):
        await strategy.set("hit_threshold_pct", 999.0, updated_by="test")


async def test_set_within_bounds(strategy: Strategy):
    """Setting a value within bounds succeeds."""
    await strategy.set("hit_threshold_pct", 20.0, updated_by="test")
    assert strategy.get("hit_threshold_pct") == 20.0


async def test_locked_key_cannot_be_changed(strategy: Strategy):
    """A locked key raises ValueError on set."""
    await strategy.lock("hit_threshold_pct")
    with pytest.raises(ValueError, match="locked"):
        await strategy.set("hit_threshold_pct", 20.0, updated_by="test")


async def test_unlock_allows_change(strategy: Strategy):
    """Unlocking a key allows setting it again."""
    await strategy.lock("hit_threshold_pct")
    await strategy.unlock("hit_threshold_pct")
    await strategy.set("hit_threshold_pct", 25.0, updated_by="test")
    assert strategy.get("hit_threshold_pct") == 25.0


async def test_get_timestamp_default(strategy: Strategy):
    """get_timestamp returns datetime.min for unknown keys."""
    result = strategy.get_timestamp("nonexistent_ts")
    assert result == datetime.min


async def test_set_and_get_timestamp(strategy: Strategy):
    """Roundtrip a timestamp through set_timestamp/get_timestamp."""
    ts = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
    await strategy.set_timestamp("last_scan_at", ts)
    result = strategy.get_timestamp("last_scan_at")
    assert result == ts


async def test_get_all_returns_dict(strategy: Strategy):
    """get_all() returns a dict containing all default keys."""
    all_vals = strategy.get_all()
    assert isinstance(all_vals, dict)
    for key in STRATEGY_DEFAULTS:
        assert key in all_vals


async def test_unbounded_key_accepts_any_value(strategy: Strategy):
    """Keys without bounds accept any value."""
    await strategy.set("lessons_learned", "test", updated_by="test")
    assert strategy.get("lessons_learned") == "test"
