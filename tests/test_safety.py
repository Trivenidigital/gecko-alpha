"""Tests for GoPlus token safety check."""

import pytest
import aiohttp
from aioresponses import aioresponses

from scout.safety import is_safe


@pytest.fixture
def mock_aiohttp():
    with aioresponses() as m:
        yield m


GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/1?contract_addresses=0xtest"


def _goplus_response(honeypot="0", is_blacklisted="0", buy_tax="0.01", sell_tax="0.01"):
    return {
        "code": 0,
        "result": {
            "0xtest": {
                "is_honeypot": honeypot,
                "is_blacklisted": is_blacklisted,
                "buy_tax": buy_tax,
                "sell_tax": sell_tax,
            }
        }
    }


async def test_safe_token_returns_true(mock_aiohttp):
    mock_aiohttp.get(
        GOPLUS_URL,
        payload=_goplus_response(),
    )

    async with aiohttp.ClientSession() as session:
        result = await is_safe("0xtest", "ethereum", session)

    assert result is True


async def test_honeypot_returns_false(mock_aiohttp):
    mock_aiohttp.get(GOPLUS_URL, payload=_goplus_response(honeypot="1"))

    async with aiohttp.ClientSession() as session:
        result = await is_safe("0xtest", "ethereum", session)

    assert result is False


async def test_blacklisted_returns_false(mock_aiohttp):
    mock_aiohttp.get(GOPLUS_URL, payload=_goplus_response(is_blacklisted="1"))

    async with aiohttp.ClientSession() as session:
        result = await is_safe("0xtest", "ethereum", session)

    assert result is False


async def test_high_sell_tax_returns_false(mock_aiohttp):
    mock_aiohttp.get(GOPLUS_URL, payload=_goplus_response(sell_tax="0.15"))

    async with aiohttp.ClientSession() as session:
        result = await is_safe("0xtest", "ethereum", session)

    assert result is False


async def test_high_buy_tax_returns_false(mock_aiohttp):
    mock_aiohttp.get(GOPLUS_URL, payload=_goplus_response(buy_tax="0.12"))

    async with aiohttp.ClientSession() as session:
        result = await is_safe("0xtest", "ethereum", session)

    assert result is False


async def test_api_failure_returns_true(mock_aiohttp):
    """Fail open: API error → return True (don't block alerts)."""
    mock_aiohttp.get(GOPLUS_URL, status=500)

    async with aiohttp.ClientSession() as session:
        result = await is_safe("0xtest", "ethereum", session)

    assert result is True


async def test_solana_chain_mapping(mock_aiohttp):
    solana_url = "https://api.gopluslabs.io/api/v1/token_security/solana?contract_addresses=0xtest"
    mock_aiohttp.get(solana_url, payload=_goplus_response())

    async with aiohttp.ClientSession() as session:
        result = await is_safe("0xtest", "solana", session)

    assert result is True
