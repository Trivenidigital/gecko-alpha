"""BL-NEW-LIVE-HYBRID M1 v2.1: symbol_normalize tests."""

from __future__ import annotations

import pytest

from scout.db import Database
from scout.live.symbol_normalize import canonical_from_ccxt_market, lookup_canonical


def test_canonical_from_ccxt_spot():
    assert canonical_from_ccxt_market("BTC/USDT") == "BTC"


def test_canonical_from_ccxt_strips_perp_suffix():
    assert canonical_from_ccxt_market("BTC/USDT:USDT") == "BTC"


def test_canonical_handles_1inch_style():
    assert canonical_from_ccxt_market("1INCH/USDT") == "1INCH"


def test_canonical_handles_usd_quote():
    assert canonical_from_ccxt_market("ETH/USD") == "ETH"


def test_canonical_handles_perp_with_usd_settle():
    assert canonical_from_ccxt_market("BTC/USD:USD") == "BTC"


@pytest.mark.asyncio
async def test_lookup_canonical_returns_none_when_alias_absent(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    result = await lookup_canonical(db, "binance", "BTCUSDT")
    assert result is None
    await db.close()


@pytest.mark.asyncio
async def test_lookup_canonical_resolves_recorded_alias(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "INSERT INTO symbol_aliases (canonical, venue, venue_symbol) "
        "VALUES (?, ?, ?)",
        ("BTC", "binance", "BTCUSDT"),
    )
    await db._conn.commit()
    result = await lookup_canonical(db, "binance", "BTCUSDT")
    assert result == "BTC"
    await db.close()
