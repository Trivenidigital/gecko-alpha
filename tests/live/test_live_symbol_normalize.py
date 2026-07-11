"""symbol_normalize — canonical extraction + venue-pair lookup.

Tests for scout/live/symbol_normalize.py:
- canonical_from_ccxt_market(symbol: str | dict) → str
- lookup_canonical(db, canonical, venue) → str | None
"""

import pytest

from scout.db import Database
from scout.live.symbol_normalize import canonical_from_ccxt_market, lookup_canonical


class TestCanonicalFromCcxtMarket:
    """Extract canonical ticker from CCXT market symbol or object."""

    def test_canonical_from_ccxt_market_strips_quote(self):
        """BTC/USDT → BTC"""
        assert canonical_from_ccxt_market("BTC/USDT") == "btc"

    def test_canonical_from_ccxt_market_strips_perp_suffix(self):
        """BTC/USDT:USDT (perp) → BTC"""
        assert canonical_from_ccxt_market("BTC/USDT:USDT") == "btc"

    def test_canonical_handles_1inch_style(self):
        """1INCH/USDT → 1INCH"""
        assert canonical_from_ccxt_market("1INCH/USDT") == "1inch"

    def test_canonical_handles_solana_meme_with_prefix(self):
        """$PEPE-SOL (Solana memecoin style) → pepe.

        Meme convention: $PREFIX-SOL on Solana. Extract base (PEPE),
        de-prefix $, drop -SOL suffix.
        """
        assert canonical_from_ccxt_market("$PEPE-SOL/USDC") == "pepe"

    def test_canonical_handles_solana_meme_without_prefix(self):
        """PEPE-SOL (Solana memecoin without $) → pepe"""
        assert canonical_from_ccxt_market("PEPE-SOL/USDC") == "pepe"

    def test_canonical_preserves_numbers_in_base(self):
        """1INCH, 1SOL, etc."""
        assert canonical_from_ccxt_market("1SOL/USDT") == "1sol"

    def test_canonical_handles_dict_ccxt_market_object(self):
        """CCXT market object with 'symbol' key → same as symbol string.

        Real CCXT market object example:
        {
            'id': 'btcusdt',
            'symbol': 'BTC/USDT',
            'base': 'BTC',
            'quote': 'USDT',
        }
        """
        market = {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT"}
        assert canonical_from_ccxt_market(market) == "btc"

    def test_canonical_from_dict_respects_base_asset_override(self):
        """If base_asset_override provided, use it instead of dict-extracted base.

        Useful for non-standard CCXT markets where symbol extraction
        doesn't match the canonical we want.
        """
        market = {"symbol": "BTC/USDT", "base": "BTC"}
        # Override to use explicit base asset instead of symbol extraction.
        assert canonical_from_ccxt_market(market, base_asset_override="ETH") == "eth"


class TestLookupCanonical:
    """Query symbol_aliases table by canonical + venue."""

    async def test_lookup_canonical_returns_pair_on_match(self, tmp_path):
        """canonical='BTC', venue='binance' → 'BTCUSDT'"""
        db = Database(tmp_path / "test.db")
        await db.initialize()

        # Insert a test mapping using low-level _conn API
        await db._conn.execute(
            """
            INSERT INTO symbol_aliases (canonical, venue, venue_symbol)
            VALUES (?, ?, ?)
            """,
            ("btc", "binance", "BTCUSDT"),
        )
        await db._conn.commit()

        # Query should return the pair
        result = await lookup_canonical(db, "btc", "binance")
        assert result == "BTCUSDT"
        await db.close()

    async def test_lookup_canonical_returns_none_on_missing(self, tmp_path):
        """canonical='XYZ', venue='binance' not in table → None"""
        db = Database(tmp_path / "test.db")
        await db.initialize()

        result = await lookup_canonical(db, "xyz", "binance")
        assert result is None
        await db.close()

    async def test_lookup_canonical_different_venues(self, tmp_path):
        """Same canonical, different venues → return venue-specific pair"""
        db = Database(tmp_path / "test.db")
        await db.initialize()

        # Insert mappings for different venues
        await db._conn.execute(
            """
            INSERT INTO symbol_aliases (canonical, venue, venue_symbol)
            VALUES (?, ?, ?), (?, ?, ?)
            """,
            ("btc", "binance", "BTCUSDT", "btc", "kraken", "XBTUSDT"),
        )
        await db._conn.commit()

        # Query should return venue-specific pair
        binance_result = await lookup_canonical(db, "btc", "binance")
        kraken_result = await lookup_canonical(db, "btc", "kraken")

        assert binance_result == "BTCUSDT"
        assert kraken_result == "XBTUSDT"
        await db.close()

    async def test_lookup_canonical_case_insensitive_canonical(self, tmp_path):
        """Canonical lookup should be case-insensitive in both directions."""
        db = Database(tmp_path / "test.db")
        await db.initialize()

        # Insert with lowercase
        await db._conn.execute(
            """
            INSERT INTO symbol_aliases (canonical, venue, venue_symbol)
            VALUES (?, ?, ?)
            """,
            ("btc", "binance", "BTCUSDT"),
        )
        await db._conn.commit()

        # Query with uppercase should still match (case-insensitive)
        result_lower = await lookup_canonical(db, "btc", "binance")
        result_upper = await lookup_canonical(db, "BTC", "binance")

        assert result_lower == "BTCUSDT"
        assert result_upper == "BTCUSDT"
        await db.close()
