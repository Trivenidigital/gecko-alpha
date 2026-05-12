"""Tests for held-position price-refresh lane (§12c-narrow remediation).

See scout/ingestion/held_position_prices.py + tasks/plan_held_position_price_freshness.md.
"""

from __future__ import annotations

import re

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.db import Database
from scout.ingestion.held_position_prices import (
    _is_cg_coin_id,
    _reset_cycle_counter_for_tests,
    _shape_for_cache_prices,
    fetch_held_position_prices,
)
from scout.ratelimit import coingecko_limiter

SIMPLE_PRICE_PATTERN = re.compile(r"https://api\.coingecko\.com/api/v3/simple/price")


@pytest.fixture(autouse=True)
async def _clear_rate_limit():
    await coingecko_limiter.reset()
    yield
    await coingecko_limiter.reset()


@pytest.fixture(autouse=True)
def _reset_counter():
    _reset_cycle_counter_for_tests()
    yield
    _reset_cycle_counter_for_tests()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


async def _insert_open_trade(db: Database, token_id: str, symbol: str = "TEST"):
    """Insert a minimal open paper_trades row for testing."""
    await db._conn.execute(
        """INSERT INTO paper_trades (
            token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at, signal_combo
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            symbol,
            symbol,
            "coingecko",
            "test",
            "{}",
            1.0,
            100.0,
            100.0,
            20.0,
            10.0,
            1.2,
            0.9,
            "open",
            "2026-05-12T00:00:00+00:00",
            "test",
        ),
    )
    await db._conn.commit()


# --- _is_cg_coin_id heuristic ---


@pytest.mark.parametrize(
    "token_id,expected",
    [
        ("payai-network-2", True),
        ("bitcoin", True),
        ("anon-alien", True),
        ("ribbita-by-virtuals", True),
        ("simple_underscore", True),
        ("0xabc123def456", False),  # EVM contract addr
        ("0x" + "a" * 40, False),  # full EVM addr
        (
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            False,
        ),  # Solana base58, > 60 chars
        ("", False),
        (None, False),
    ],
)
def test_is_cg_coin_id_heuristic(token_id, expected):
    assert _is_cg_coin_id(token_id) is expected


# --- _shape_for_cache_prices ---


def test_shape_for_cache_prices_converts_simple_price_response():
    response = {
        "bitcoin": {"usd": 100_000.0, "usd_market_cap": 2e12, "usd_24h_change": 1.5},
        "payai-network-2": {"usd": 0.01, "usd_market_cap": 1e7, "usd_24h_change": -5.0},
    }
    raw = _shape_for_cache_prices(response)
    assert len(raw) == 2
    by_id = {r["id"]: r for r in raw}
    assert by_id["bitcoin"]["current_price"] == 100_000.0
    assert by_id["bitcoin"]["market_cap"] == 2e12
    assert by_id["bitcoin"]["price_change_percentage_24h"] == 1.5
    # 7d is intentionally omitted; cache_prices() COALESCE preserves existing
    assert "price_change_percentage_7d_in_currency" not in by_id["bitcoin"]


def test_shape_for_cache_prices_skips_non_dict_entries():
    response = {"bitcoin": {"usd": 100.0}, "broken": None, "also-broken": "string"}
    raw = _shape_for_cache_prices(response)
    assert len(raw) == 1
    assert raw[0]["id"] == "bitcoin"


# --- fetch_held_position_prices integration ---


async def test_no_open_trades_returns_empty(db, settings_factory):
    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        with aioresponses():
            result = await fetch_held_position_prices(session, settings, db)
    assert result == []


async def test_all_contract_addr_held_tokens_skipped(db, settings_factory):
    await _insert_open_trade(db, "0xabc123", "TKN1")
    await _insert_open_trade(
        db, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" + "x", "TKN2"
    )
    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            result = await fetch_held_position_prices(session, settings, db)
            # No HTTP call should have fired
            assert len(m.requests) == 0
    assert result == []


async def test_mixed_cohort_only_cg_ids_in_batch(db, settings_factory):
    await _insert_open_trade(db, "bitcoin", "BTC")
    await _insert_open_trade(db, "0xabc123", "EVMTKN")
    await _insert_open_trade(db, "payai-network-2", "PAYAI")
    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                SIMPLE_PRICE_PATTERN,
                payload={
                    "bitcoin": {
                        "usd": 100_000.0,
                        "usd_market_cap": 2e12,
                        "usd_24h_change": 1.5,
                    },
                    "payai-network-2": {
                        "usd": 0.01,
                        "usd_market_cap": 1e7,
                        "usd_24h_change": -12.0,
                    },
                },
            )
            result = await fetch_held_position_prices(session, settings, db)
    assert len(result) == 2
    ids = {r["id"] for r in result}
    assert ids == {"bitcoin", "payai-network-2"}


async def test_cache_prices_preserves_existing_7d(db, settings_factory):
    """The cache_prices() enhancement: COALESCE on price_change_7d so the
    held-position lane (which lacks 7d in /simple/price) doesn't null out
    existing 7d values written by earlier markets/trending fetches."""
    # Seed an existing row with 7d data (from a previous markets fetch)
    seeded = [
        {
            "id": "bitcoin",
            "current_price": 90_000.0,
            "price_change_percentage_24h": 1.0,
            "price_change_percentage_7d_in_currency": 12.5,
            "market_cap": 1.8e12,
        }
    ]
    await db.cache_prices(seeded)

    # Held-position lane writes without 7d
    held_raw = [
        {
            "id": "bitcoin",
            "current_price": 100_000.0,
            "price_change_percentage_24h": 2.0,
            "market_cap": 2.0e12,
        }
    ]
    await db.cache_prices(held_raw)

    cached = await db.get_cached_prices(["bitcoin"])
    assert cached["bitcoin"]["usd"] == 100_000.0  # current_price refreshed
    assert cached["bitcoin"]["change_24h"] == 2.0  # 24h refreshed
    assert cached["bitcoin"]["change_7d"] == 12.5  # 7d preserved via COALESCE
    assert cached["bitcoin"]["market_cap"] == 2.0e12


async def test_disabled_flag_short_circuits(db, settings_factory):
    await _insert_open_trade(db, "bitcoin", "BTC")
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=False)
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            result = await fetch_held_position_prices(session, settings, db)
            assert len(m.requests) == 0
    assert result == []


async def test_cadence_throttling(db, settings_factory):
    """interval=3 means refresh fires on counter 3, 6, 9, ... (1-indexed)."""
    await _insert_open_trade(db, "bitcoin", "BTC")
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES=3)
    payload = {"bitcoin": {"usd": 100.0, "usd_market_cap": 1e9, "usd_24h_change": 0.0}}

    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(SIMPLE_PRICE_PATTERN, payload=payload, repeat=True)
            r1 = await fetch_held_position_prices(session, settings, db)  # counter=1
            r2 = await fetch_held_position_prices(session, settings, db)  # counter=2
            r3 = await fetch_held_position_prices(
                session, settings, db
            )  # counter=3 fires
            r4 = await fetch_held_position_prices(session, settings, db)  # counter=4

    assert r1 == []
    assert r2 == []
    assert len(r3) == 1
    assert r4 == []


async def test_aalien_never_cached_case_gets_row_created(db, settings_factory):
    """AALIEN-style: open trade exists but no price_cache row.
    First refresh should create the row via INSERT ON CONFLICT DO UPDATE."""
    await _insert_open_trade(db, "anon-alien", "AALIEN")
    settings = settings_factory()

    # Confirm no cache row exists pre-refresh
    pre = await db.get_cached_prices(["anon-alien"])
    assert pre == {}

    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                SIMPLE_PRICE_PATTERN,
                payload={
                    "anon-alien": {
                        "usd": 0.00035,
                        "usd_market_cap": 350_000,
                        "usd_24h_change": -2.0,
                    }
                },
            )
            raw = await fetch_held_position_prices(session, settings, db)

    # Caller (main.py) merges raw into all_raw and calls cache_prices.
    # Simulate that here:
    await db.cache_prices(raw)

    post = await db.get_cached_prices(["anon-alien"])
    assert "anon-alien" in post
    assert post["anon-alien"]["usd"] == 0.00035
    assert post["anon-alien"]["change_7d"] is None  # never had a 7d value


async def test_429_handled_gracefully(db, settings_factory, patch_module_sleep):
    """A 429 from CoinGecko should produce empty result, not raise."""
    patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")
    await _insert_open_trade(db, "bitcoin", "BTC")
    settings = settings_factory()

    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(SIMPLE_PRICE_PATTERN, status=429, repeat=True)
            result = await fetch_held_position_prices(session, settings, db)

    # _get_with_backoff returns None on persistent 429 → empty response
    # → _shape_for_cache_prices returns []
    assert result == []
