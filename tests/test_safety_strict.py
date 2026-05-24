"""Direct coverage for ``is_safe_strict`` — the BL-064 fail-CLOSED variant.

Background: ``scout.safety.is_safe`` is a fail-OPEN wrapper around
``is_safe_strict`` (existing 7 tests cover the wrapper). The strict
variant returns a ``(is_safe, check_completed)`` tuple so callers can
distinguish:

  (True, True)   — GoPlus confirmed clean
  (False, True)  — GoPlus confirmed unsafe (honeypot/blacklist/high tax)
  (False, False) — GoPlus could not produce a verdict (5xx, timeout,
                   missing record) — caller must fail-closed

The wrapper-only tests can't distinguish the (False, True) vs
(False, False) cases. Round 9 adds direct coverage so a security-
critical surface refactor cannot silently turn (False, True) into
(False, False) (degrades trust) or vice versa.
"""

from __future__ import annotations

import asyncio
import socket

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.safety import is_safe_strict


@pytest.fixture
def mock_aiohttp():
    with aioresponses() as m:
        yield m


GOPLUS_URL = (
    "https://api.gopluslabs.io/api/v1/token_security/1?contract_addresses=0xtest"
)


def _goplus_payload(
    honeypot: str = "0",
    is_blacklisted: str = "0",
    buy_tax: str = "0.01",
    sell_tax: str = "0.01",
) -> dict:
    return {
        "code": 0,
        "result": {
            "0xtest": {
                "is_honeypot": honeypot,
                "is_blacklisted": is_blacklisted,
                "buy_tax": buy_tax,
                "sell_tax": sell_tax,
            }
        },
    }


# ---------------------------------------------------------------------------
# Happy / verdict-known paths — (bool, True)
# ---------------------------------------------------------------------------


async def test_strict_clean_token_returns_safe_completed():
    """Clean GoPlus response → (True, True)."""
    with aioresponses() as m:
        m.get(GOPLUS_URL, payload=_goplus_payload())
        async with aiohttp.ClientSession() as session:
            verdict, completed = await is_safe_strict(
                "0xtest", "ethereum", session
            )
    assert verdict is True
    assert completed is True


async def test_strict_honeypot_returns_unsafe_completed():
    """Confirmed honeypot → (False, True) — distinct from (False, False)."""
    with aioresponses() as m:
        m.get(GOPLUS_URL, payload=_goplus_payload(honeypot="1"))
        async with aiohttp.ClientSession() as session:
            verdict, completed = await is_safe_strict(
                "0xtest", "ethereum", session
            )
    assert verdict is False
    assert completed is True, (
        "honeypot is a CONFIRMED unsafe verdict; completed must be True "
        "so callers don't conflate with (False, False) GoPlus-outage cases"
    )


async def test_strict_blacklisted_returns_unsafe_completed():
    with aioresponses() as m:
        m.get(GOPLUS_URL, payload=_goplus_payload(is_blacklisted="1"))
        async with aiohttp.ClientSession() as session:
            verdict, completed = await is_safe_strict(
                "0xtest", "ethereum", session
            )
    assert verdict is False
    assert completed is True


async def test_strict_high_buy_tax_returns_unsafe_completed():
    """buy_tax >= 10% → (False, True)."""
    with aioresponses() as m:
        m.get(GOPLUS_URL, payload=_goplus_payload(buy_tax="0.12"))
        async with aiohttp.ClientSession() as session:
            verdict, completed = await is_safe_strict(
                "0xtest", "ethereum", session
            )
    assert verdict is False
    assert completed is True


async def test_strict_high_sell_tax_returns_unsafe_completed():
    """sell_tax >= 10% → (False, True)."""
    with aioresponses() as m:
        m.get(GOPLUS_URL, payload=_goplus_payload(sell_tax="0.15"))
        async with aiohttp.ClientSession() as session:
            verdict, completed = await is_safe_strict(
                "0xtest", "ethereum", session
            )
    assert verdict is False
    assert completed is True


# ---------------------------------------------------------------------------
# Fail-CLOSED paths — (False, False)
# ---------------------------------------------------------------------------


async def test_strict_5xx_returns_failed_closed():
    """GoPlus 5xx → (False, False) — caller MUST refuse to trade."""
    with aioresponses() as m:
        m.get(GOPLUS_URL, status=503)
        async with aiohttp.ClientSession() as session:
            verdict, completed = await is_safe_strict(
                "0xtest", "ethereum", session
            )
    assert verdict is False
    assert completed is False, (
        "5xx means GoPlus could not produce a verdict; completed=False "
        "tells the caller this is an outage, not a confirmed-unsafe verdict"
    )


async def test_strict_4xx_returns_failed_closed():
    """GoPlus 4xx (rate limit, auth) → (False, False)."""
    with aioresponses() as m:
        m.get(GOPLUS_URL, status=429)
        async with aiohttp.ClientSession() as session:
            verdict, completed = await is_safe_strict(
                "0xtest", "ethereum", session
            )
    assert verdict is False
    assert completed is False


async def test_strict_missing_record_returns_failed_closed():
    """GoPlus 200 OK but no result for our contract → (False, False)."""
    with aioresponses() as m:
        m.get(GOPLUS_URL, payload={"code": 0, "result": {}})
        async with aiohttp.ClientSession() as session:
            verdict, completed = await is_safe_strict(
                "0xtest", "ethereum", session
            )
    assert verdict is False
    assert completed is False


async def test_strict_client_error_returns_failed_closed():
    """aiohttp.ClientError → (False, False).

    Uses the base ClientError directly instead of ClientConnectorError —
    the latter has a complex __str__ that walks _conn_key.ssl and fails
    when constructed with stub args. The except clause in scout/safety.py
    catches the base class, so coverage is equivalent.
    """
    with aioresponses() as m:
        m.get(GOPLUS_URL, exception=aiohttp.ClientError("simulated network down"))
        async with aiohttp.ClientSession() as session:
            verdict, completed = await is_safe_strict(
                "0xtest", "ethereum", session
            )
    assert verdict is False
    assert completed is False


async def test_strict_timeout_returns_failed_closed():
    """asyncio.TimeoutError → (False, False)."""
    with aioresponses() as m:
        m.get(GOPLUS_URL, exception=asyncio.TimeoutError())
        async with aiohttp.ClientSession() as session:
            verdict, completed = await is_safe_strict(
                "0xtest", "ethereum", session
            )
    assert verdict is False
    assert completed is False


# ---------------------------------------------------------------------------
# Special-case: coingecko chain → fast-path (True, True)
# ---------------------------------------------------------------------------


async def test_strict_coingecko_chain_fast_path():
    """chain='coingecko' returns (True, True) without an HTTP call —
    CG-native tokens don't need GoPlus."""
    # No mock registered; if the implementation tries to fetch this will
    # fail with a connection error.
    async with aiohttp.ClientSession() as session:
        verdict, completed = await is_safe_strict(
            "0xtest", "coingecko", session
        )
    assert verdict is True
    assert completed is True


# ---------------------------------------------------------------------------
# Chain ID mapping for solana
# ---------------------------------------------------------------------------


async def test_strict_solana_uses_solana_path():
    """Solana chain maps to the 'solana' path in GoPlus URL."""
    solana_url = (
        "https://api.gopluslabs.io/api/v1/token_security/solana"
        "?contract_addresses=0xtest"
    )
    with aioresponses() as m:
        m.get(solana_url, payload=_goplus_payload())
        async with aiohttp.ClientSession() as session:
            verdict, completed = await is_safe_strict(
                "0xtest", "solana", session
            )
    assert verdict is True
    assert completed is True
