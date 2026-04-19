"""Tests for the PREDICT phase — laggard selection, scoring, control picks, dedup."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.narrative.models import CategoryAcceleration, LaggardToken
from scout.narrative.predictor import (
    filter_laggards,
    is_cooling_down,
    parse_scoring_response,
    partition_and_select,
    record_signal,
    store_predictions,
)

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.initialize()
    yield database
    await database.close()


def _make_raw_token(
    coin_id: str = "coin-a",
    symbol: str = "A",
    name: str = "Coin A",
    market_cap: float = 50_000_000,
    price: float = 1.0,
    change_24h: float = -5.0,
    volume: float = 200_000,
) -> dict:
    return {
        "id": coin_id,
        "symbol": symbol,
        "name": name,
        "market_cap": market_cap,
        "current_price": price,
        "price_change_percentage_24h": change_24h,
        "total_volume": volume,
    }


def _make_laggard(
    coin_id: str = "coin-a",
    price_change_24h: float = -5.0,
    volume_24h: float = 200_000,
    market_cap: float = 50_000_000,
) -> LaggardToken:
    return LaggardToken(
        coin_id=coin_id,
        symbol=coin_id.upper(),
        name=f"Token {coin_id}",
        market_cap=market_cap,
        price=1.0,
        price_change_24h=price_change_24h,
        volume_24h=volume_24h,
        category_id="cat-defi",
        category_name="DeFi",
    )


# ------------------------------------------------------------------
# filter_laggards
# ------------------------------------------------------------------


def test_filter_laggards_applies_thresholds():
    """4 tokens, only 2 pass the threshold filters."""
    tokens = [
        _make_raw_token(
            coin_id="ok1", market_cap=50_000_000, change_24h=-3.0, volume=200_000
        ),
        _make_raw_token(
            coin_id="ok2", market_cap=100_000_000, change_24h=5.0, volume=150_000
        ),
        # Fails: mcap too high
        _make_raw_token(
            coin_id="big", market_cap=500_000_000, change_24h=-2.0, volume=300_000
        ),
        # Fails: volume too low
        _make_raw_token(
            coin_id="low", market_cap=10_000_000, change_24h=-1.0, volume=5_000
        ),
    ]
    result = filter_laggards(
        tokens,
        category_id="cat1",
        category_name="DeFi",
        max_mcap=200_000_000,
        max_change=10.0,
        min_change=-20.0,
        min_volume=100_000,
    )
    ids = [t.coin_id for t in result]
    assert len(result) == 2
    assert "ok1" in ids
    assert "ok2" in ids
    assert "big" not in ids
    assert "low" not in ids


def test_filter_laggards_sorted_by_change():
    """Tokens are sorted by price_change_24h ascending (most negative first)."""
    tokens = [
        _make_raw_token(coin_id="mid", change_24h=-3.0),
        _make_raw_token(coin_id="worst", change_24h=-10.0),
        _make_raw_token(coin_id="best", change_24h=2.0),
    ]
    result = filter_laggards(
        tokens,
        category_id="cat1",
        category_name="DeFi",
        max_mcap=200_000_000,
        max_change=10.0,
        min_change=-20.0,
        min_volume=100_000,
    )
    assert len(result) == 3
    assert result[0].coin_id == "worst"
    assert result[1].coin_id == "mid"
    assert result[2].coin_id == "best"


# ------------------------------------------------------------------
# partition_and_select
# ------------------------------------------------------------------


def test_partition_and_select():
    """10 tokens, max_picks=3 -> scored=3, control=3, no overlap."""
    laggards = [_make_laggard(coin_id=f"coin-{i}") for i in range(10)]
    scored, control = partition_and_select(laggards, max_picks=3)
    assert len(scored) == 3
    assert len(control) == 3
    scored_ids = {t.coin_id for t in scored}
    control_ids = {t.coin_id for t in control}
    assert scored_ids.isdisjoint(control_ids), "scored and control must not overlap"


# ------------------------------------------------------------------
# parse_scoring_response
# ------------------------------------------------------------------


def test_parse_scoring_response_valid():
    """Plain JSON string is parsed correctly."""
    text = '{"narrative_fit": 75, "staying_power": "Medium", "confidence": "High", "reasoning": "test"}'
    result = parse_scoring_response(text)
    assert result["narrative_fit"] == 75
    assert result["staying_power"] == "Medium"


def test_parse_scoring_response_markdown():
    """JSON wrapped in ```json block is extracted and parsed."""
    text = '```json\n{"narrative_fit": 60, "staying_power": "Low", "confidence": "Low", "reasoning": "ok"}\n```'
    result = parse_scoring_response(text)
    assert result["narrative_fit"] == 60
    assert result["confidence"] == "Low"


# ------------------------------------------------------------------
# is_cooling_down
# ------------------------------------------------------------------


async def test_is_cooling_down_no_signal(db: Database):
    """Returns False when no signal exists for the category."""
    result = await is_cooling_down(db, "nonexistent-cat")
    assert result is False


async def test_is_cooling_down_active_signal(db: Database):
    """Returns True when signal has future cooling_down_until."""
    conn = db._conn
    assert conn is not None
    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=4)).isoformat()
    await conn.execute(
        """INSERT INTO narrative_signals
           (category_id, category_name, acceleration, volume_growth_pct,
            coin_count_change, trigger_count, detected_at, cooling_down_until)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("cat-active", "Active Cat", 8.0, 20.0, 5, 1, now.isoformat(), future),
    )
    await conn.commit()
    result = await is_cooling_down(db, "cat-active")
    assert result is True


# ------------------------------------------------------------------
# record_signal
# ------------------------------------------------------------------


async def test_record_signal_increments_trigger_count(db: Database):
    """Insert a signal, call again -> trigger_count becomes 2."""
    count1 = await record_signal(
        db,
        category_id="cat-inc",
        category_name="Incrementing",
        acceleration=6.0,
        volume_growth_pct=15.0,
        coin_count_change=3,
        cooldown_hours=4,
    )
    assert count1 == 1

    count2 = await record_signal(
        db,
        category_id="cat-inc",
        category_name="Incrementing",
        acceleration=7.0,
        volume_growth_pct=18.0,
        coin_count_change=4,
        cooldown_hours=4,
    )
    assert count2 == 2


# ------------------------------------------------------------------
# store_predictions
# ------------------------------------------------------------------


async def test_store_predictions(db: Database):
    """Insert a prediction and verify the row exists."""
    now = datetime.now(timezone.utc).isoformat()
    predictions = [
        {
            "category_id": "cat-store",
            "category_name": "Store Test",
            "coin_id": "bitcoin",
            "symbol": "BTC",
            "name": "Bitcoin",
            "market_cap_at_prediction": 1_000_000,
            "price_at_prediction": 50_000.0,
            "narrative_fit_score": 80,
            "staying_power": "High",
            "confidence": "High",
            "reasoning": "Strong narrative fit",
            "market_regime": "BULL",
            "trigger_count": 1,
            "is_control": False,
            "is_holdout": False,
            "strategy_snapshot": {"key": "value"},
            "strategy_snapshot_ab": {"ab_key": "ab_value"},
            "predicted_at": now,
        }
    ]
    await store_predictions(db, predictions)

    conn = db._conn
    assert conn is not None
    cursor = await conn.execute(
        "SELECT coin_id, narrative_fit_score, strategy_snapshot, strategy_snapshot_ab "
        "FROM predictions WHERE category_id = 'cat-store'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "bitcoin"
    assert row[1] == 80
    # Verify JSON serialization
    assert json.loads(row[2]) == {"key": "value"}
    assert json.loads(row[3]) == {"ab_key": "ab_value"}


# ------------------------------------------------------------------
# score_token
# ------------------------------------------------------------------


async def test_score_token_success():
    """Test score_token with mocked Anthropic client."""
    from unittest.mock import MagicMock
    from scout.narrative.predictor import score_token
    from scout.narrative.models import LaggardToken, CategoryAcceleration

    token = LaggardToken(
        coin_id="test",
        symbol="TST",
        name="Test Token",
        market_cap=50e6,
        price=1.0,
        price_change_24h=2.0,
        volume_24h=500_000,
        category_id="ai",
        category_name="AI",
    )
    accel = CategoryAcceleration(
        category_id="ai",
        name="AI",
        current_velocity=12.0,
        previous_velocity=5.0,
        acceleration=7.0,
        volume_24h=2e9,
        volume_growth_pct=15.0,
        coin_count_change=-2,
        is_heating=True,
    )

    from unittest.mock import AsyncMock

    mock_client = MagicMock()
    mock_message = MagicMock()
    mock_message.content = [
        MagicMock(
            text='{"narrative_fit": 75, "staying_power": "High", "confidence": "Medium", "reasoning": "Strong fit"}'
        )
    ]
    mock_client.messages.create = AsyncMock(return_value=mock_message)

    result = await score_token(
        token,
        accel,
        "BULL",
        "fetch-ai, render",
        "",
        "fake-key",
        "claude-haiku-4-5",
        client=mock_client,
    )
    assert result is not None
    assert result["narrative_fit"] == 75
    assert result["confidence"] == "Medium"


async def test_score_token_api_failure():
    """Test score_token returns None on API error."""
    from unittest.mock import MagicMock
    from scout.narrative.predictor import score_token
    from scout.narrative.models import LaggardToken, CategoryAcceleration

    token = LaggardToken(
        coin_id="test",
        symbol="TST",
        name="Test",
        market_cap=50e6,
        price=1.0,
        price_change_24h=2.0,
        volume_24h=500_000,
        category_id="ai",
        category_name="AI",
    )
    accel = CategoryAcceleration(
        category_id="ai",
        name="AI",
        current_velocity=12.0,
        previous_velocity=5.0,
        acceleration=7.0,
        volume_24h=2e9,
        volume_growth_pct=15.0,
        coin_count_change=0,
        is_heating=True,
    )

    mock_client = MagicMock()
    mock_client.messages.create = MagicMock(side_effect=Exception("API error"))

    result = await score_token(
        token,
        accel,
        "BULL",
        "fetch-ai",
        "",
        "fake-key",
        "claude-haiku-4-5",
        client=mock_client,
    )
    assert result is None


async def test_build_scoring_prompt_includes_watchlist():
    """build_scoring_prompt should embed watchlist_users in the prompt text."""
    from scout.narrative.predictor import build_scoring_prompt
    from scout.narrative.models import LaggardToken, CategoryAcceleration

    token = LaggardToken(
        coin_id="test",
        symbol="TST",
        name="Test Token",
        market_cap=50e6,
        price=1.0,
        price_change_24h=2.0,
        volume_24h=500_000,
        category_id="ai",
        category_name="AI",
    )
    accel = CategoryAcceleration(
        category_id="ai",
        name="AI",
        current_velocity=12.0,
        previous_velocity=5.0,
        acceleration=7.0,
        volume_24h=2e9,
        volume_growth_pct=15.0,
        coin_count_change=-2,
        is_heating=True,
    )

    prompt = build_scoring_prompt(
        token,
        accel,
        "BULL",
        "fetch-ai, render",
        "",
        watchlist_users=42_500,
    )
    assert "CoinGecko watchlist" in prompt
    assert "42,500 users tracking this coin" in prompt


async def test_score_token_passes_watchlist_to_prompt():
    """score_token should pass watchlist_users through to the built prompt."""
    from unittest.mock import MagicMock
    from scout.narrative.predictor import score_token
    from scout.narrative.models import LaggardToken, CategoryAcceleration

    token = LaggardToken(
        coin_id="test",
        symbol="TST",
        name="Test Token",
        market_cap=50e6,
        price=1.0,
        price_change_24h=2.0,
        volume_24h=500_000,
        category_id="ai",
        category_name="AI",
    )
    accel = CategoryAcceleration(
        category_id="ai",
        name="AI",
        current_velocity=12.0,
        previous_velocity=5.0,
        acceleration=7.0,
        volume_24h=2e9,
        volume_growth_pct=15.0,
        coin_count_change=-2,
        is_heating=True,
    )

    mock_client = MagicMock()
    mock_message = MagicMock()
    mock_message.content = [
        MagicMock(
            text='{"narrative_fit": 60, "staying_power": "Medium", "confidence": "Low", "reasoning": "x"}'
        )
    ]
    mock_client.messages.create = MagicMock(return_value=mock_message)

    await score_token(
        token,
        accel,
        "BULL",
        "fetch-ai",
        "",
        "fake-key",
        "claude-haiku-4-5",
        client=mock_client,
        watchlist_users=7_777,
    )

    called_kwargs = mock_client.messages.create.call_args.kwargs
    user_prompt = called_kwargs["messages"][0]["content"]
    assert "CoinGecko watchlist" in user_prompt
    assert "7,777 users tracking this coin" in user_prompt
