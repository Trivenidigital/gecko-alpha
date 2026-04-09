"""Tests for the LEARN phase — hit rates, adjustments, circuit breaker."""

import json
from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.narrative.learner import (
    apply_adjustments,
    compute_hit_rates,
    get_recent_predictions,
    should_pause,
)
from scout.narrative.strategy import Strategy


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "learn_test.db")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
async def strategy(db: Database):
    s = Strategy(db)
    await s.load_or_init()
    return s


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_NOW = datetime.now(timezone.utc).isoformat()


def _prediction_row(
    *,
    coin_id: str,
    is_control: int = 0,
    outcome_class: str | None = None,
    evaluated_at: str | None = None,
) -> tuple:
    """Build a full prediction row tuple for INSERT."""
    return (
        "cat1",          # category_id
        "DeFi",          # category_name
        coin_id,         # coin_id
        coin_id.upper(), # symbol
        f"Token {coin_id}",  # name
        1_000_000.0,     # market_cap_at_prediction
        1.0,             # price_at_prediction
        80,              # narrative_fit_score
        "Medium",        # staying_power
        "High",          # confidence
        "test reasoning",  # reasoning
        json.dumps({}),  # strategy_snapshot
        _NOW,            # predicted_at
        is_control,      # is_control
        outcome_class,   # outcome_class
        evaluated_at,    # evaluated_at
    )


_INSERT_SQL = """
    INSERT INTO predictions
        (category_id, category_name, coin_id, symbol, name,
         market_cap_at_prediction, price_at_prediction,
         narrative_fit_score, staying_power, confidence, reasoning,
         strategy_snapshot, predicted_at, is_control,
         outcome_class, evaluated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


# ------------------------------------------------------------------
# compute_hit_rates
# ------------------------------------------------------------------


async def test_compute_hit_rates_empty(db: Database):
    """With no predictions, all rates are 0.0."""
    result = await compute_hit_rates(db)
    assert result == {
        "agent_hit_rate": 0.0,
        "control_hit_rate": 0.0,
        "true_alpha": 0.0,
    }


async def test_compute_hit_rates_with_data(db: Database):
    """Insert 2 agent (1 HIT, 1 MISS) + 2 control (2 HIT) and verify rates."""
    conn = db._conn
    assert conn is not None

    # Agent predictions
    await conn.execute(_INSERT_SQL, _prediction_row(
        coin_id="a1", is_control=0, outcome_class="HIT", evaluated_at=_NOW,
    ))
    await conn.execute(_INSERT_SQL, _prediction_row(
        coin_id="a2", is_control=0, outcome_class="MISS", evaluated_at=_NOW,
    ))
    # Control predictions
    await conn.execute(_INSERT_SQL, _prediction_row(
        coin_id="c1", is_control=1, outcome_class="HIT", evaluated_at=_NOW,
    ))
    await conn.execute(_INSERT_SQL, _prediction_row(
        coin_id="c2", is_control=1, outcome_class="HIT", evaluated_at=_NOW,
    ))
    await conn.commit()

    result = await compute_hit_rates(db)
    assert result["agent_hit_rate"] == 50.0
    assert result["control_hit_rate"] == 100.0
    assert result["true_alpha"] == -50.0


# ------------------------------------------------------------------
# apply_adjustments
# ------------------------------------------------------------------


async def test_apply_adjustments_respects_min_sample(
    db: Database, strategy: Strategy
):
    """With 0 predictions, adjustments should not be applied."""
    adjustments = [{"key": "hit_threshold_pct", "new_value": 20.0, "reason": "test"}]
    applied = await apply_adjustments(adjustments, strategy, db, min_sample=100)
    assert applied == 0
    # hit_threshold_pct should remain at default
    assert strategy.get("hit_threshold_pct") == 15.0


# ------------------------------------------------------------------
# should_pause (circuit breaker)
# ------------------------------------------------------------------


async def test_should_pause_below_threshold():
    """7 consecutive days below threshold → True."""
    rates = [5.0, 6.0, 7.0, 8.0, 3.0, 4.0, 9.0]
    assert should_pause(rates, threshold=10.0, consecutive_days=7) is True


async def test_should_pause_above_threshold():
    """Last rate is above threshold → False."""
    rates = [5.0, 6.0, 7.0, 8.0, 3.0, 4.0, 15.0]
    assert should_pause(rates, threshold=10.0, consecutive_days=7) is False


async def test_should_pause_not_enough_data():
    """Only 2 rates — not enough data → False."""
    rates = [5.0, 6.0]
    assert should_pause(rates, threshold=10.0, consecutive_days=7) is False
