"""Tests for async CryptoPanic fetcher."""

import asyncio
import re

import aiohttp
import pytest
from aioresponses import aioresponses
from structlog.testing import capture_logs

from scout.config import Settings
from scout.news.cryptopanic import fetch_cryptopanic_posts

# Regex pattern so mock matches regardless of querystring order/contents
# (aioresponses does exact-URL matching by default, which includes params).
BASE = re.compile(r"https://cryptopanic\.com/api/v1/posts/.*")


def _settings(**overrides):
    defaults = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        CRYPTOPANIC_ENABLED=True,
        CRYPTOPANIC_API_TOKEN="tok",
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def test_disabled_flag_short_circuits():
    s = _settings(CRYPTOPANIC_ENABLED=False)
    async with aiohttp.ClientSession() as session:
        result = await fetch_cryptopanic_posts(session, s)
    assert result == []


async def test_missing_token_short_circuits_without_network():
    s = _settings(CRYPTOPANIC_API_TOKEN="")
    with capture_logs() as logs:
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []
    assert any(log["event"] == "cryptopanic_auth_missing" for log in logs)


async def test_fetch_happy_path():
    s = _settings()
    body = {
        "results": [
            {
                "id": 1,
                "title": "A",
                "url": "u1",
                "published_at": "2026-04-20T10:00:00Z",
                "currencies": [{"code": "BTC"}],
                "votes": {"positive": 5, "negative": 1},
            },
            {
                "id": 2,
                "title": "B",
                "url": "u2",
                "published_at": "2026-04-20T09:00:00Z",
                "currencies": [],
                "votes": {},
            },
        ]
    }
    with aioresponses() as m:
        m.get(BASE, payload=body, status=200, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert len(result) == 2
    assert result[0].post_id == 1


async def test_fetch_empty_results():
    s = _settings()
    with aioresponses() as m:
        m.get(BASE, payload={"results": []}, status=200, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []


async def test_fetch_malformed_body_returns_empty():
    s = _settings()
    with aioresponses() as m:
        m.get(BASE, body="not json", status=200, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []


async def test_fetch_401_returns_empty():
    s = _settings()
    with aioresponses() as m:
        m.get(BASE, status=401, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []


async def test_fetch_429_retries_then_empty():
    s = _settings()
    with aioresponses() as m:
        m.get(BASE, status=429, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []


async def test_fetch_5xx_retries_then_empty():
    s = _settings()
    with aioresponses() as m:
        m.get(BASE, status=503, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []


async def test_fetch_200_after_429_succeeds():
    s = _settings()
    body = {
        "results": [
            {
                "id": 1,
                "title": "T",
                "url": "u",
                "published_at": "2026-04-20T00:00:00Z",
                "currencies": [],
                "votes": {},
            }
        ]
    }
    with aioresponses() as m:
        m.get(BASE, status=429)
        m.get(BASE, payload=body, status=200)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert len(result) == 1
    assert result[0].post_id == 1


async def test_fetch_dedups_duplicate_post_ids_in_batch():
    s = _settings()
    body = {
        "results": [
            {
                "id": 5,
                "title": "first",
                "url": "u5",
                "published_at": "2026-04-20T00:00:00Z",
                "currencies": [],
                "votes": {},
            },
            {
                "id": 5,
                "title": "dup",
                "url": "u5",
                "published_at": "2026-04-20T00:00:00Z",
                "currencies": [],
                "votes": {},
            },
            {
                "id": 6,
                "title": "second",
                "url": "u6",
                "published_at": "2026-04-20T00:00:00Z",
                "currencies": [],
                "votes": {},
            },
        ]
    }
    with aioresponses() as m:
        m.get(BASE, payload=body, status=200, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert [p.post_id for p in result] == [5, 6]


async def test_fetch_timeout_returns_empty():
    """Spec §12: asyncio.TimeoutError / aiohttp.ClientError fall through to []."""
    s = _settings()
    with aioresponses() as m:
        m.get(BASE, exception=asyncio.TimeoutError(), repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []


async def test_fetch_connection_error_returns_empty():
    """Spec §12: aiohttp.ClientConnectorError also falls through cleanly."""
    s = _settings()
    with aioresponses() as m:
        m.get(BASE, exception=aiohttp.ClientError("boom"), repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []
