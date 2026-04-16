"""Tests for briefing collector — each external API fetch + internal queries."""

import json

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.briefing.collector import (
    collect_briefing_data,
    collect_internal_data,
    fetch_cg_global,
    fetch_crypto_news,
    fetch_defi_tvl,
    fetch_fear_greed,
    fetch_funding_rates,
    fetch_liquidations,
)


@pytest.fixture
def mock_aio():
    with aioresponses() as m:
        yield m


# ---------------------------------------------------------------------------
# Fear & Greed
# ---------------------------------------------------------------------------


class TestFetchFearGreed:
    async def test_success(self, mock_aio):
        mock_aio.get(
            "https://api.alternative.me/fng/?limit=2",
            payload={
                "data": [
                    {"value": "72", "value_classification": "Greed"},
                    {"value": "65", "value_classification": "Fear"},
                ]
            },
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_fear_greed(session)
        assert result == {"value": 72, "classification": "Greed", "previous": 65}

    async def test_http_error(self, mock_aio):
        mock_aio.get("https://api.alternative.me/fng/?limit=2", status=500)
        async with aiohttp.ClientSession() as session:
            result = await fetch_fear_greed(session)
        assert result is None

    async def test_empty_data(self, mock_aio):
        mock_aio.get(
            "https://api.alternative.me/fng/?limit=2",
            payload={"data": []},
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_fear_greed(session)
        assert result is None

    async def test_network_error(self, mock_aio):
        mock_aio.get(
            "https://api.alternative.me/fng/?limit=2",
            exception=aiohttp.ClientError("timeout"),
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_fear_greed(session)
        assert result is None


# ---------------------------------------------------------------------------
# CoinGecko Global
# ---------------------------------------------------------------------------


class TestFetchCgGlobal:
    async def test_success(self, mock_aio):
        mock_aio.get(
            "https://api.coingecko.com/api/v3/global",
            payload={
                "data": {
                    "total_market_cap": {"usd": 2_500_000_000_000},
                    "market_cap_change_percentage_24h_usd": 3.4,
                    "market_cap_percentage": {"btc": 56.9, "eth": 10.2},
                    "active_cryptocurrencies": 15000,
                }
            },
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_cg_global(session, api_key="test-key")
        assert result["total_mcap"] == 2_500_000_000_000
        assert result["mcap_change_24h"] == 3.4
        assert result["btc_dominance"] == 56.9
        assert result["eth_dominance"] == 10.2

    async def test_http_error(self, mock_aio):
        mock_aio.get("https://api.coingecko.com/api/v3/global", status=429)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cg_global(session)
        assert result is None


# ---------------------------------------------------------------------------
# CoinGlass Funding Rates
# ---------------------------------------------------------------------------


class TestFetchFundingRates:
    async def test_success(self, mock_aio):
        mock_aio.get(
            "https://open-api.coinglass.com/public/v2/funding",
            payload={
                "data": [
                    {"symbol": "BTC", "uMarginFundingRate": 0.01},
                    {"symbol": "ETH", "uMarginFundingRate": 0.03},
                    {"symbol": "SOL", "uMarginFundingRate": 0.02},
                ]
            },
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_funding_rates(session)
        assert result == {"btc": 0.01, "eth": 0.03}

    async def test_auth_failure(self, mock_aio):
        mock_aio.get(
            "https://open-api.coinglass.com/public/v2/funding",
            status=401,
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_funding_rates(session)
        assert result is None

    async def test_forbidden(self, mock_aio):
        mock_aio.get(
            "https://open-api.coinglass.com/public/v2/funding",
            status=403,
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_funding_rates(session, api_key="bad-key")
        assert result is None


# ---------------------------------------------------------------------------
# CoinGlass Liquidations
# ---------------------------------------------------------------------------


class TestFetchLiquidations:
    async def test_success(self, mock_aio):
        mock_aio.get(
            "https://open-api.coinglass.com/public/v2/liquidation_history",
            payload={
                "data": [{"volUsd": 142_000_000, "longRate": 35, "shortRate": 65}]
            },
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_liquidations(session)
        assert result["total_24h"] == 142_000_000
        assert result["long_pct"] == 35
        assert result["short_pct"] == 65

    async def test_auth_failure(self, mock_aio):
        mock_aio.get(
            "https://open-api.coinglass.com/public/v2/liquidation_history",
            status=403,
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_liquidations(session)
        assert result is None

    async def test_empty_data(self, mock_aio):
        mock_aio.get(
            "https://open-api.coinglass.com/public/v2/liquidation_history",
            payload={"data": []},
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_liquidations(session)
        assert result is None


# ---------------------------------------------------------------------------
# DeFi Llama TVL
# ---------------------------------------------------------------------------


class TestFetchDefiTvl:
    async def test_success(self, mock_aio):
        mock_aio.get(
            "https://api.llama.fi/v2/chains",
            payload=[
                {"name": "Ethereum", "tvl": 45_000_000_000, "change_1d": 0.8},
                {"name": "Solana", "tvl": 10_000_000_000, "change_1d": 2.1},
                {"name": "Arbitrum", "tvl": 5_000_000_000, "change_1d": -0.5},
            ],
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_defi_tvl(session)
        assert result is not None
        assert result["total"] == 60_000_000_000
        assert len(result["top_chains"]) == 3
        assert result["top_chains"][0]["name"] == "Ethereum"

    async def test_http_error(self, mock_aio):
        mock_aio.get("https://api.llama.fi/v2/chains", status=500)
        async with aiohttp.ClientSession() as session:
            result = await fetch_defi_tvl(session)
        assert result is None


# ---------------------------------------------------------------------------
# CryptoCompare News
# ---------------------------------------------------------------------------


class TestFetchCryptoNews:
    async def test_success(self, mock_aio):
        articles = [
            {
                "title": f"Article {i}",
                "source": f"Source{i}",
                "source_info": {"name": f"Source{i}"},
                "url": f"https://example.com/{i}",
                "categories": "BTC|ETH",
            }
            for i in range(15)
        ]
        mock_aio.get(
            "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&sortOrder=popular",
            payload={"Data": articles},
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_crypto_news(session)
        assert result is not None
        assert len(result) == 10  # capped at 10
        assert result[0]["title"] == "Article 0"

    async def test_http_error(self, mock_aio):
        mock_aio.get(
            "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&sortOrder=popular",
            status=500,
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_crypto_news(session)
        assert result is None


# ---------------------------------------------------------------------------
# Internal DB queries
# ---------------------------------------------------------------------------


class TestCollectInternalData:
    async def test_with_empty_db(self, tmp_path):
        from scout.db import Database

        db = Database(tmp_path / "test.db")
        await db.initialize()
        try:
            result = await collect_internal_data(db)
            assert isinstance(result, dict)
            assert result["heating_categories"] == []
            assert result["cooling_categories"] == []
            assert result["early_catches"] == []
            assert result["predictions"] == []
            assert result["volume_spikes"] == []
            assert result["chain_completions"] == []
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Master collect function
# ---------------------------------------------------------------------------


class TestCollectBriefingData:
    async def test_all_apis_fail_gracefully(self, mock_aio, tmp_path):
        """Even when all external APIs fail, collect_briefing_data returns a valid dict."""
        # All external APIs return 500
        mock_aio.get("https://api.alternative.me/fng/?limit=2", status=500)
        mock_aio.get("https://api.coingecko.com/api/v3/global", status=500)
        mock_aio.get("https://open-api.coinglass.com/public/v2/funding", status=500)
        mock_aio.get(
            "https://open-api.coinglass.com/public/v2/liquidation_history", status=500
        )
        mock_aio.get("https://api.llama.fi/v2/chains", status=500)
        mock_aio.get(
            "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&sortOrder=popular",
            status=500,
        )

        from scout.db import Database

        db = Database(tmp_path / "test.db")
        await db.initialize()
        try:
            from scout.config import Settings

            settings = Settings(
                TELEGRAM_BOT_TOKEN="t",
                TELEGRAM_CHAT_ID="c",
                ANTHROPIC_API_KEY="k",
            )
            async with aiohttp.ClientSession() as session:
                result = await collect_briefing_data(session, db, settings)
            assert "timestamp" in result
            assert result["fear_greed"] is None
            assert result["global_market"] is None
            assert isinstance(result["internal"], dict)
        finally:
            await db.close()
