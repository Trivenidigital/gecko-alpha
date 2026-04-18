"""Tests for scout.social.lunarcrush.client.

Cover auth header, 429 backoff with 60s cap, 401 disable flag, malformed
JSON, missing fields, field name consistency, and ClientSession isolation.
"""

from __future__ import annotations

import re

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.config import Settings
from scout.social.lunarcrush.client import LunarCrushClient


def _settings(**overrides) -> Settings:
    defaults = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        LUNARCRUSH_API_KEY="lc_key",
        LUNARCRUSH_ENABLED=True,
    )
    defaults.update(overrides)
    return Settings(**defaults)


LC_URL = re.compile(r"https://lunarcrush\.com/api4/public/coins/list/v2.*")


@pytest.mark.asyncio
async def test_auth_header_bearer():
    """Each request carries the Bearer token auth header."""
    s = _settings()
    with aioresponses() as m:
        m.get(
            LC_URL,
            payload={"data": [{"id": 1, "symbol": "FOO", "name": "Foo"}]},
        )
        client = LunarCrushClient(s)
        try:
            coins, cost = await client.fetch_coins_list()
        finally:
            await client.close()
    assert coins[0]["symbol"] == "FOO"
    # aioresponses doesn't easily expose request headers; rely on the
    # client-side wiring by inspecting the header builder.
    headers = client._auth_headers()
    assert headers["Authorization"] == "Bearer lc_key"


@pytest.mark.asyncio
async def test_401_sets_disabled_flag():
    """A 401 response exits the client cleanly and flips disabled=True.

    Zero credit is charged on auth failure (the server didn't process a
    billable request).
    """
    s = _settings()
    with aioresponses() as m:
        m.get(LC_URL, status=401, body="")
        client = LunarCrushClient(s)
        try:
            coins, cost = await client.fetch_coins_list()
        finally:
            await client.close()
    assert client.disabled is True
    assert coins == []
    assert cost == 0


@pytest.mark.asyncio
async def test_malformed_json_returns_empty():
    """Malformed JSON never raises; returns empty list."""
    s = _settings()
    with aioresponses() as m:
        m.get(LC_URL, status=200, body="not-json-at-all")
        client = LunarCrushClient(s)
        try:
            coins, cost = await client.fetch_coins_list()
        finally:
            await client.close()
    assert coins == []


@pytest.mark.asyncio
async def test_missing_data_key_returns_empty():
    """Response without 'data' key returns empty coin list."""
    s = _settings()
    with aioresponses() as m:
        m.get(LC_URL, status=200, payload={"something_else": 42})
        client = LunarCrushClient(s)
        try:
            coins, cost = await client.fetch_coins_list()
        finally:
            await client.close()
    assert coins == []


@pytest.mark.asyncio
async def test_field_names_match_v4():
    """Returned coin dicts preserve v4 field names for downstream."""
    s = _settings()
    payload = {
        "data": [
            {
                "id": 1,
                "symbol": "AST",
                "name": "Asteroid",
                "social_volume_24h": 5000,
                "interactions_24h": 2000,
                "social_dominance": 0.04,
                "galaxy_score": 72,
            }
        ]
    }
    with aioresponses() as m:
        m.get(LC_URL, status=200, payload=payload)
        client = LunarCrushClient(s)
        try:
            coins, cost = await client.fetch_coins_list()
        finally:
            await client.close()
    c = coins[0]
    assert c["social_volume_24h"] == 5000
    assert c["interactions_24h"] == 2000
    assert c["social_dominance"] == 0.04


@pytest.mark.asyncio
async def test_429_backoff_sequence(monkeypatch):
    """429 responses backoff 5 -> 10 -> 20 (capped at 60s) and ultimately give up."""
    s = _settings()
    client = LunarCrushClient(s)

    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("scout.social.lunarcrush.client.asyncio.sleep", _fake_sleep)

    with aioresponses() as m:
        # Always 429: we should see the backoff ladder attempted then give up.
        for _ in range(5):
            m.get(LC_URL, status=429, body="rate limited")
        try:
            coins, cost = await client.fetch_coins_list()
        finally:
            await client.close()
    # First delay should be 5s, second 10, third 20, fourth 40, capped at 60.
    assert delays[:3] == [5.0, 10.0, 20.0]
    # Nothing stored; no crash.
    assert coins == []


@pytest.mark.asyncio
async def test_owns_its_own_session():
    """Closing the client closes the underlying ClientSession."""
    s = _settings()
    client = LunarCrushClient(s)
    assert not client._session.closed
    await client.close()
    assert client._session.closed


@pytest.mark.asyncio
async def test_request_timeout_on_hanging_endpoint():
    """A hanging endpoint surfaces as an asyncio.TimeoutError -> transport
    error branch -> empty coins, 0 credit."""
    s = _settings()

    with aioresponses() as m:
        # aioresponses' ``timeout=True`` flag raises asyncio.TimeoutError
        # on the call, exactly like a network timeout.
        m.get(LC_URL, timeout=True)
        client = LunarCrushClient(s)
        try:
            coins, cost = await client.fetch_coins_list()
        finally:
            await client.close()
    assert coins == []
    assert cost == 0


@pytest.mark.asyncio
async def test_429_capped_at_60s(monkeypatch):
    """Five consecutive 429s: delays[3]==40, delays[4]==60, max <= 60."""
    s = _settings()
    client = LunarCrushClient(s)

    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("scout.social.lunarcrush.client.asyncio.sleep", _fake_sleep)
    with aioresponses() as m:
        for _ in range(6):
            m.get(LC_URL, status=429, body="rate limited")
        try:
            coins, cost = await client.fetch_coins_list()
        finally:
            await client.close()
    # 5 slots in the sequence + cap: [5,10,20,40,60].
    assert delays[:5] == [5.0, 10.0, 20.0, 40.0, 60.0]
    assert max(delays) <= 60.0


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds(monkeypatch):
    """503 twice then 200 succeeds on retry; backoff applied."""
    s = _settings()
    client = LunarCrushClient(s)
    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("scout.social.lunarcrush.client.asyncio.sleep", _fake_sleep)
    with aioresponses() as m:
        m.get(LC_URL, status=503, body="bad")
        m.get(LC_URL, status=503, body="bad")
        m.get(LC_URL, status=200, payload={"data": [{"id": 1, "symbol": "FOO"}]})
        try:
            coins, cost = await client.fetch_coins_list()
        finally:
            await client.close()
    assert len(coins) == 1
    assert cost == 1
    assert delays  # at least one 5xx backoff happened


@pytest.mark.asyncio
async def test_5xx_gives_up_after_max_retries_zero_credit(monkeypatch):
    """After 3 503s the client gives up cleanly with 0 credits charged."""
    s = _settings()
    client = LunarCrushClient(s)

    async def _fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("scout.social.lunarcrush.client.asyncio.sleep", _fake_sleep)
    with aioresponses() as m:
        for _ in range(5):
            m.get(LC_URL, status=503, body="bad")
        try:
            coins, cost = await client.fetch_coins_list()
        finally:
            await client.close()
    assert coins == []
    assert cost == 0


@pytest.mark.asyncio
async def test_auth_header_bearer_sent_on_wire():
    """Inspect the actual wire headers: Authorization: Bearer <key>."""
    s = _settings()
    captured: dict[str, str] = {}

    def _callback(url, **kwargs):
        for k, v in (kwargs.get("headers") or {}).items():
            captured[k] = v
        from aioresponses import CallbackResult

        return CallbackResult(status=200, payload={"data": []})

    with aioresponses() as m:
        m.get(LC_URL, callback=_callback)
        client = LunarCrushClient(s)
        try:
            await client.fetch_coins_list()
        finally:
            await client.close()
    assert captured.get("Authorization") == "Bearer lc_key"
