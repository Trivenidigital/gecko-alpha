"""Tests for scout.counter.scorer — counter-narrative scoring orchestrator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from scout.counter.models import CounterScore, RedFlag
from scout.counter.scorer import (
    _parse_counter_response,
    score_counter_memecoin,
    score_counter_narrative,
)

# --- _parse_counter_response tests ---


def test_parse_counter_response_valid():
    raw = '{"risk_score": 55, "counter_argument": "High risk due to low liquidity."}'
    result = _parse_counter_response(raw)
    assert result == {
        "risk_score": 55,
        "counter_argument": "High risk due to low liquidity.",
    }


def test_parse_counter_response_markdown():
    raw = '```json\n{"risk_score": 30, "counter_argument": "Minor concerns."}\n```'
    result = _parse_counter_response(raw)
    assert result == {"risk_score": 30, "counter_argument": "Minor concerns."}


def test_parse_counter_response_invalid():
    assert _parse_counter_response("not json at all") is None
    assert _parse_counter_response("") is None
    assert _parse_counter_response("```json\nbroken{```") is None


# --- score_counter_narrative tests ---


def _make_mock_client(response_text: str) -> AsyncMock:
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=response_text)]
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)
    return mock_client


@pytest.fixture()
def sample_flags() -> list[RedFlag]:
    return [
        RedFlag(flag="already_peaked", severity="high", detail="30d price change 120%"),
    ]


async def test_score_counter_narrative_success(sample_flags: list[RedFlag]):
    response_json = '{"risk_score": 65, "counter_argument": "Price already peaked."}'
    mock_client = _make_mock_client(response_json)

    result = await score_counter_narrative(
        token_name="FooToken",
        symbol="FOO",
        market_cap=5_000_000,
        price_change_24h=12.5,
        category_name="DeFi",
        acceleration=3.2,
        narrative_fit_score=70.0,
        flags=sample_flags,
        data_completeness="full",
        api_key="test-key",
        client=mock_client,
    )

    assert isinstance(result, CounterScore)
    assert result.risk_score == 65
    assert result.counter_argument == "Price already peaked."
    assert result.red_flags == sample_flags
    mock_client.messages.create.assert_awaited_once()


async def test_score_counter_narrative_api_failure(sample_flags: list[RedFlag]):
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API timeout"))

    result = await score_counter_narrative(
        token_name="FooToken",
        symbol="FOO",
        market_cap=5_000_000,
        price_change_24h=12.5,
        category_name="DeFi",
        acceleration=3.2,
        narrative_fit_score=70.0,
        flags=sample_flags,
        data_completeness="partial",
        api_key="test-key",
        client=mock_client,
    )

    assert isinstance(result, CounterScore)
    assert result.risk_score is None
    assert result.counter_argument == ""


# --- score_counter_memecoin tests ---


async def test_score_counter_memecoin_success():
    flags = [
        RedFlag(flag="liquidity_trap", severity="high", detail="Liquidity below $15k"),
    ]
    response_json = '{"risk_score": 72, "counter_argument": "Very low liquidity."}'
    mock_client = _make_mock_client(response_json)

    result = await score_counter_memecoin(
        token_name="MemeDog",
        symbol="MDOG",
        chain="solana",
        token_age_days=0.5,
        liquidity_usd=12_000,
        vol_liq_ratio=35.0,
        buy_pressure=0.65,
        holder_count=120,
        flags=flags,
        data_completeness="full",
        api_key="test-key",
        client=mock_client,
    )

    assert isinstance(result, CounterScore)
    assert result.risk_score == 72
    assert result.counter_argument == "Very low liquidity."
    assert result.red_flags == flags

    # Verify template used token_age_hours (0.5 * 24 = 12)
    call_kwargs = mock_client.messages.create.call_args.kwargs
    user_msg = call_kwargs["messages"][0]["content"]
    assert "12 hours" in user_msg


async def test_score_counter_no_flags():
    response_json = '{"risk_score": 10, "counter_argument": "No red flags detected."}'
    mock_client = _make_mock_client(response_json)

    result = await score_counter_narrative(
        token_name="SafeToken",
        symbol="SAFE",
        market_cap=50_000_000,
        price_change_24h=5.0,
        category_name="L1",
        acceleration=1.0,
        narrative_fit_score=85.0,
        flags=[],
        data_completeness="full",
        api_key="test-key",
        client=mock_client,
    )

    assert isinstance(result, CounterScore)
    assert result.risk_score == 10
    assert result.counter_argument == "No red flags detected."
    assert result.red_flags == []
