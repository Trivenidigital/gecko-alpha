"""Tests for the EVALUATE phase — checkpoint classification and peak tracking."""

import json
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.db import Database
from scout.narrative.evaluator import (
    classify_checkpoint,
    evaluate_pending,
    fetch_prices_batch,
    pick_final_class,
)
from scout.narrative.strategy import Strategy

MARKETS_PATTERN = re.compile(r"https://api\.coingecko\.com/api/v3/coins/markets")


# ------------------------------------------------------------------
# classify_checkpoint
# ------------------------------------------------------------------


def test_classify_hit():
    assert classify_checkpoint(20.0, hit=15.0, miss=-10.0) == "HIT"


def test_classify_miss():
    assert classify_checkpoint(-15.0, hit=15.0, miss=-10.0) == "MISS"


def test_classify_neutral():
    assert classify_checkpoint(5.0, hit=15.0, miss=-10.0) == "NEUTRAL"


def test_classify_boundary_hit():
    assert classify_checkpoint(15.0, hit=15.0, miss=-10.0) == "HIT"


def test_classify_boundary_miss():
    assert classify_checkpoint(-10.0, hit=15.0, miss=-10.0) == "MISS"


# ------------------------------------------------------------------
# pick_final_class
# ------------------------------------------------------------------


def test_final_class_uses_48h():
    assert pick_final_class("HIT", "NEUTRAL", "MISS") == "MISS"


def test_final_class_all_hit():
    assert pick_final_class("HIT", "HIT", "HIT") == "HIT"


def test_final_class_none_48h():
    assert pick_final_class("HIT", "NEUTRAL", None) is None


# ------------------------------------------------------------------
# fetch_prices_batch
# ------------------------------------------------------------------


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


async def test_fetch_prices_batch_success():
    """Successful batch fetch returns coin prices."""
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mocked:
            mocked.get(
                MARKETS_PATTERN,
                payload=[
                    {"id": "bitcoin", "current_price": 50000.0},
                    {"id": "ethereum", "current_price": 3000.0},
                ],
            )
            result = await fetch_prices_batch(session, ["bitcoin", "ethereum"])
            assert result == {"bitcoin": 50000.0, "ethereum": 3000.0}


async def test_fetch_prices_batch_429():
    """Rate-limited response returns empty dict (partial)."""
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mocked:
            mocked.get(
                MARKETS_PATTERN,
                status=429,
            )
            result = await fetch_prices_batch(session, ["bitcoin"])
            assert result == {}


async def test_fetch_prices_batch_empty():
    """Empty coin_ids list returns empty dict without HTTP call."""
    async with aiohttp.ClientSession() as session:
        result = await fetch_prices_batch(session, [])
        assert result == {}


# ------------------------------------------------------------------
# evaluate_pending — integration tests
# ------------------------------------------------------------------


async def _insert_prediction(
    db: Database,
    coin_id: str = "bitcoin",
    price_at_prediction: float = 100.0,
    predicted_at: datetime | None = None,
    eval_retry_count: int = 0,
) -> int:
    """Helper to insert a prediction row and return its id."""
    conn = db._conn
    assert conn is not None
    if predicted_at is None:
        predicted_at = datetime.now(timezone.utc) - timedelta(hours=49)
    cursor = await conn.execute(
        """INSERT INTO predictions
           (category_id, category_name, coin_id, symbol, name,
            market_cap_at_prediction, price_at_prediction,
            narrative_fit_score, staying_power, confidence, reasoning,
            strategy_snapshot, predicted_at, eval_retry_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "cat1",
            "Test Category",
            coin_id,
            "BTC",
            "Bitcoin",
            1_000_000,
            price_at_prediction,
            80,
            "strong",
            "high",
            "test reasoning",
            json.dumps({}),
            predicted_at.isoformat(),
            eval_retry_count,
        ),
    )
    await conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def test_evaluate_pending_48h_hit(db: Database, strategy: Strategy):
    """Prediction older than 48h with price increase is classified HIT."""
    pred_id = await _insert_prediction(db, price_at_prediction=100.0)

    async with aiohttp.ClientSession() as session:
        with aioresponses() as mocked:
            mocked.get(
                MARKETS_PATTERN,
                payload=[{"id": "bitcoin", "current_price": 120.0}],
            )
            await evaluate_pending(session, db, strategy)

    conn = db._conn
    assert conn is not None
    cursor = await conn.execute(
        "SELECT outcome_class, outcome_6h_class, outcome_24h_class, outcome_48h_class "
        "FROM predictions WHERE id = ?",
        (pred_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "HIT"  # outcome_class
    assert row[1] == "HIT"  # 6h
    assert row[2] == "HIT"  # 24h
    assert row[3] == "HIT"  # 48h


async def test_evaluate_pending_miss(db: Database, strategy: Strategy):
    """Prediction with large price drop is classified MISS."""
    pred_id = await _insert_prediction(db, price_at_prediction=100.0)

    async with aiohttp.ClientSession() as session:
        with aioresponses() as mocked:
            mocked.get(
                MARKETS_PATTERN,
                payload=[{"id": "bitcoin", "current_price": 80.0}],
            )
            await evaluate_pending(session, db, strategy)

    conn = db._conn
    assert conn is not None
    cursor = await conn.execute(
        "SELECT outcome_class FROM predictions WHERE id = ?", (pred_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "MISS"


async def test_evaluate_pending_price_unavailable_retries(
    db: Database, strategy: Strategy
):
    """Missing price increments retry count; third retry marks UNRESOLVED."""
    pred_id = await _insert_prediction(db, coin_id="unknowncoin", eval_retry_count=2)

    async with aiohttp.ClientSession() as session:
        with aioresponses() as mocked:
            # Return empty list — unknowncoin not found
            mocked.get(
                MARKETS_PATTERN,
                payload=[],
            )
            await evaluate_pending(session, db, strategy)

    conn = db._conn
    assert conn is not None
    cursor = await conn.execute(
        "SELECT outcome_class, outcome_reason, eval_retry_count "
        "FROM predictions WHERE id = ?",
        (pred_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "UNRESOLVED"
    assert row[1] == "price_unavailable"
    assert row[2] == 3


async def test_evaluate_pending_peak_tracking(db: Database, strategy: Strategy):
    """Peak price and peak_change_pct are updated when current > previous peak."""
    pred_id = await _insert_prediction(db, price_at_prediction=100.0)

    async with aiohttp.ClientSession() as session:
        with aioresponses() as mocked:
            mocked.get(
                MARKETS_PATTERN,
                payload=[{"id": "bitcoin", "current_price": 150.0}],
            )
            await evaluate_pending(session, db, strategy)

    conn = db._conn
    assert conn is not None
    cursor = await conn.execute(
        "SELECT peak_price, peak_change_pct FROM predictions WHERE id = ?",
        (pred_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 150.0
    assert row[1] == pytest.approx(50.0, rel=1e-2)


async def test_evaluate_pending_only_6h_elapsed(db: Database, strategy: Strategy):
    """Prediction only 7h old gets 6h checkpoint but not 24h/48h."""
    predicted_at = datetime.now(timezone.utc) - timedelta(hours=7)
    pred_id = await _insert_prediction(
        db, price_at_prediction=100.0, predicted_at=predicted_at
    )

    async with aiohttp.ClientSession() as session:
        with aioresponses() as mocked:
            mocked.get(
                MARKETS_PATTERN,
                payload=[{"id": "bitcoin", "current_price": 120.0}],
            )
            await evaluate_pending(session, db, strategy)

    conn = db._conn
    assert conn is not None
    cursor = await conn.execute(
        "SELECT outcome_class, outcome_6h_class, outcome_24h_class, outcome_48h_class "
        "FROM predictions WHERE id = ?",
        (pred_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] is None  # outcome_class not set (48h not reached)
    assert row[1] == "HIT"  # 6h evaluated
    assert row[2] is None  # 24h not reached
    assert row[3] is None  # 48h not reached
