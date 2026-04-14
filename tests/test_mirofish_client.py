"""Tests for MiroFish async client."""

import asyncio

import pytest
import aiohttp
from aioresponses import aioresponses

from scout.exceptions import MiroFishConnectionError, MiroFishTimeoutError
from scout.mirofish.client import simulate
from scout.models import MiroFishResult


SAMPLE_SEED = {
    "token_name": "TestCoin",
    "ticker": "TST",
    "chain": "solana",
    "market_cap": 50000,
    "age_hours": 60,
    "concept_description": "A meme token",
    "social_snippets": "None detected",
    "prompt": "Token: TestCoin (TST) on solana...",
}


@pytest.fixture
def mock_aiohttp():
    with aioresponses() as m:
        yield m


async def test_simulate_success(mock_aiohttp, settings_factory):
    mock_aiohttp.post(
        "http://localhost:5001/simulate",
        payload={
            "narrative_score": 75,
            "virality_class": "High",
            "summary": "Strong viral potential with self-referential narrative.",
        },
    )

    settings = settings_factory(MIROFISH_URL="http://localhost:5001", MIROFISH_TIMEOUT_SEC=5)
    async with aiohttp.ClientSession() as session:
        result = await simulate(SAMPLE_SEED, session, settings)

    assert isinstance(result, MiroFishResult)
    assert result.narrative_score == 75
    assert result.virality_class == "High"
    assert result.summary == "Strong viral potential with self-referential narrative."


async def test_simulate_timeout_raises(mock_aiohttp, settings_factory):
    mock_aiohttp.post(
        "http://localhost:5001/simulate",
        exception=asyncio.TimeoutError(),
    )

    settings = settings_factory(MIROFISH_URL="http://localhost:5001", MIROFISH_TIMEOUT_SEC=5)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MiroFishTimeoutError):
            await simulate(SAMPLE_SEED, session, settings)


async def test_simulate_connection_error_raises(mock_aiohttp, settings_factory):
    mock_aiohttp.post(
        "http://localhost:5001/simulate",
        exception=aiohttp.ClientError("Connection refused"),
    )

    settings = settings_factory(MIROFISH_URL="http://localhost:5001", MIROFISH_TIMEOUT_SEC=5)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MiroFishConnectionError):
            await simulate(SAMPLE_SEED, session, settings)


async def test_simulate_malformed_response_raises(mock_aiohttp, settings_factory):
    mock_aiohttp.post(
        "http://localhost:5001/simulate",
        payload={"invalid": "response"},
    )

    settings = settings_factory(MIROFISH_URL="http://localhost:5001", MIROFISH_TIMEOUT_SEC=5)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MiroFishConnectionError):
            await simulate(SAMPLE_SEED, session, settings)


async def test_simulate_http_error_raises(mock_aiohttp, settings_factory):
    mock_aiohttp.post(
        "http://localhost:5001/simulate",
        status=500,
    )

    settings = settings_factory(MIROFISH_URL="http://localhost:5001", MIROFISH_TIMEOUT_SEC=5)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MiroFishConnectionError):
            await simulate(SAMPLE_SEED, session, settings)
