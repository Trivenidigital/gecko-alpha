"""Tests for CoinGecko ingestion module."""

import re

import pytest
import aiohttp
from aioresponses import aioresponses

from scout.ingestion import coingecko as cg_module
from scout.ingestion.coingecko import (
    fetch_top_movers,
    fetch_trending,
    fetch_by_volume,
    fetch_midcap_gainers,
    get_last_watchdog_samples,
    _get_with_backoff,
    _reset_midcap_scan_cycle_counter_for_tests,
)
from scout.ratelimit import coingecko_limiter

# -- Fixtures --

COINS_MARKETS_RESPONSE = [
    {
        "id": "pump-token",
        "symbol": "pump",
        "name": "PumpToken",
        "market_cap": 200_000,
        "total_volume": 500_000,
        "price_change_percentage_1h_in_currency": 8.5,
        "price_change_percentage_24h": 12.0,
    },
    {
        "id": "tiny-cap",
        "symbol": "tiny",
        "name": "TinyCap",
        "market_cap": 500,  # below MIN_MARKET_CAP
        "total_volume": 100,
        "price_change_percentage_1h_in_currency": 20.0,
        "price_change_percentage_24h": 25.0,
    },
]

TRENDING_RESPONSE = {
    "coins": [
        {
            "item": {
                "id": f"coin-{i}",
                "symbol": f"c{i}",
                "name": f"Coin{i}",
                "market_cap_rank": 100 + i,
                "score": i,
            }
        }
        for i in range(15)
    ]
}

CG_BASE = "https://api.coingecko.com/api/v3"
MARKETS_PATTERN = re.compile(r"https://api\.coingecko\.com/api/v3/coins/markets")
TRENDING_PATTERN = re.compile(r"https://api\.coingecko\.com/api/v3/search/trending")


@pytest.fixture(autouse=True)
async def _clear_rate_limit():
    """Clear shared rate limiter state between tests."""
    await coingecko_limiter.reset()
    _reset_midcap_scan_cycle_counter_for_tests()
    yield
    await coingecko_limiter.reset()
    _reset_midcap_scan_cycle_counter_for_tests()


# -- Tests --


@pytest.mark.asyncio
async def test_fetch_top_movers_parses_correctly(settings_factory):
    """FR-01: /coins/markets response parsed into CandidateToken with correct fields."""
    settings = settings_factory(MIN_MARKET_CAP=1000, MAX_MARKET_CAP=1_000_000)
    with aioresponses() as mocked:
        mocked.get(MARKETS_PATTERN, payload=COINS_MARKETS_RESPONSE)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_top_movers(session, settings)

    # tiny-cap filtered out by market cap
    assert len(tokens) == 1
    t = tokens[0]
    assert t.ticker == "pump"
    assert t.token_name == "PumpToken"
    assert t.market_cap_usd == 200_000
    assert t.volume_24h_usd == 500_000
    assert t.price_change_1h == 8.5
    assert t.price_change_24h == 12.0


@pytest.mark.asyncio
async def test_fetch_trending_populates_rank(settings_factory):
    """FR-02: /search/trending populates cg_trending_rank on returned tokens."""
    settings = settings_factory()
    with aioresponses() as mocked:
        mocked.get(TRENDING_PATTERN, payload=TRENDING_RESPONSE)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending(session, settings)

    assert len(tokens) > 0
    assert tokens[0].cg_trending_rank == 1  # 1-indexed
    assert tokens[1].cg_trending_rank == 2


@pytest.mark.asyncio
async def test_fetch_trending_hydrates_market_rows_and_preserves_rank(settings_factory):
    """Trending IDs are hydrated via /coins/markets for downstream raw-market signals."""
    cg_module.last_raw_trending.clear()
    settings = settings_factory(MIN_MARKET_CAP=1000, MAX_MARKET_CAP=5_000_000)
    trending = {
        "coins": [
            {"item": {"id": "alpha", "symbol": "alp", "name": "Alpha", "score": 0}},
            {"item": {"id": "beta", "symbol": "bet", "name": "Beta", "score": 1}},
        ]
    }
    hydrated = [
        {
            "id": "alpha",
            "symbol": "alp",
            "name": "Alpha",
            "current_price": 0.01,
            "market_cap": 1_000_000,
            "total_volume": 250_000,
            "price_change_percentage_1h_in_currency": 12.0,
            "price_change_percentage_24h": 45.0,
            "price_change_percentage_7d_in_currency": 80.0,
        },
        {
            "id": "beta",
            "symbol": "bet",
            "name": "Beta",
            "current_price": 0.02,
            "market_cap": 2_000_000,
            "total_volume": 350_000,
            "price_change_percentage_1h_in_currency": 5.0,
            "price_change_percentage_24h": 22.0,
            "price_change_percentage_7d_in_currency": 40.0,
        },
    ]

    with aioresponses() as mocked:
        mocked.get(TRENDING_PATTERN, payload=trending)
        mocked.get(MARKETS_PATTERN, payload=hydrated)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending(session, settings)

    assert [(t.contract_address, t.cg_trending_rank) for t in tokens] == [
        ("alpha", 1),
        ("beta", 2),
    ]
    assert tokens[0].market_cap_usd == 1_000_000
    assert tokens[0].volume_24h_usd == 250_000
    assert [row["id"] for row in cg_module.last_raw_trending] == ["alpha", "beta"]
    assert cg_module.last_raw_trending[0]["market_cap"] == 1_000_000


@pytest.mark.asyncio
async def test_fetch_trending_hydration_failure_keeps_rank_without_fake_mcap(
    settings_factory,
):
    """market_cap_rank is rank metadata, not a market cap fallback."""
    cg_module.last_raw_trending.clear()
    settings = settings_factory()
    trending = {
        "coins": [
            {
                "item": {
                    "id": "rank-only",
                    "symbol": "ro",
                    "name": "RankOnly",
                    "market_cap_rank": 123,
                    "data": {
                        "price": 0.03,
                        "price_change_percentage_24h": {"usd": 31.0},
                    },
                }
            }
        ]
    }

    with aioresponses() as mocked:
        mocked.get(TRENDING_PATTERN, payload=trending)
        mocked.get(MARKETS_PATTERN, status=500)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending(session, settings)

    assert len(tokens) == 1
    assert tokens[0].contract_address == "rank-only"
    assert tokens[0].cg_trending_rank == 1
    assert tokens[0].market_cap_usd == 0
    assert cg_module.last_raw_trending[0]["id"] == "rank-only"
    assert cg_module.last_raw_trending[0].get("market_cap") is None


@pytest.mark.asyncio
async def test_429_enters_global_cooldown_without_retry_amplification(
    patch_module_sleep,
):
    """A 429 should not be retried inside the same cycle.

    Runtime evidence showed one logical request could become four provider
    429s. On 429 we now trip the shared cooldown and fail soft for this call;
    the next cycle gets the next chance.
    """
    patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")

    with aioresponses() as mocked:
        mocked.get(MARKETS_PATTERN, status=429)
        mocked.get(MARKETS_PATTERN, payload=COINS_MARKETS_RESPONSE)
        async with aiohttp.ClientSession() as session:
            data = await _get_with_backoff(session, f"{CG_BASE}/coins/markets")

    assert data is None
    assert len(mocked.requests) == 1


@pytest.mark.asyncio
async def test_market_cap_filter_applied(settings_factory):
    """FR-01: Tokens outside MIN/MAX_MARKET_CAP are excluded."""
    settings = settings_factory(MIN_MARKET_CAP=100_000, MAX_MARKET_CAP=300_000)
    with aioresponses() as mocked:
        mocked.get(MARKETS_PATTERN, payload=COINS_MARKETS_RESPONSE)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_top_movers(session, settings)

    # pump-token (200k) passes, tiny-cap (500) filtered
    assert len(tokens) == 1
    assert tokens[0].ticker == "pump"


@pytest.mark.asyncio
async def test_coingecko_outage_does_not_crash_pipeline(settings_factory):
    """NFR: CoinGecko API outage returns empty list, does not raise."""
    settings = settings_factory()
    with aioresponses() as mocked:
        # Non-429 errors return None immediately on first attempt
        mocked.get(MARKETS_PATTERN, status=500)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_top_movers(session, settings)

    assert tokens == []


# -- Volume scan tests --

VOLUME_RESPONSE = [
    {
        "id": "high-vol-token",
        "symbol": "hvt",
        "name": "HighVolToken",
        "market_cap": 50_000_000,
        "total_volume": 10_000_000,
        "price_change_percentage_1h_in_currency": 2.0,
        "price_change_percentage_24h": 5.0,
    },
    {
        "id": "low-vol-token",
        "symbol": "lvt",
        "name": "LowVolToken",
        "market_cap": 100_000,
        "total_volume": 50_000,
        "price_change_percentage_1h_in_currency": 1.0,
        "price_change_percentage_24h": 3.0,
    },
]


@pytest.mark.asyncio
async def test_fetch_by_volume_returns_tokens(settings_factory):
    """Volume scan returns tokens sorted by volume, wider cap range."""
    settings = settings_factory(MIN_MARKET_CAP=1000, MAX_MARKET_CAP=1_000_000)
    with aioresponses() as mocked:
        mocked.get(MARKETS_PATTERN, payload=VOLUME_RESPONSE)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_by_volume(session, settings)

    # Both tokens pass MIN_MARKET_CAP (1000); no MAX filter in fetch_by_volume
    assert len(tokens) == 2
    # Sorted by volume descending
    assert tokens[0].volume_24h_usd >= tokens[1].volume_24h_usd
    assert tokens[0].ticker == "hvt"


@pytest.mark.asyncio
async def test_fetch_by_volume_filters_below_min_mcap(settings_factory):
    """Volume scan filters out tokens below MIN_MARKET_CAP."""
    settings = settings_factory(MIN_MARKET_CAP=1_000_000)
    with aioresponses() as mocked:
        mocked.get(MARKETS_PATTERN, payload=VOLUME_RESPONSE)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_by_volume(session, settings)

    # Only high-vol-token (50M mcap) passes the 1M threshold
    assert len(tokens) == 1
    assert tokens[0].ticker == "hvt"


@pytest.mark.asyncio
async def test_fetch_by_volume_unions_page1_and_page2(settings_factory):
    """Page 1 + page 2 results are unioned and deduped.

    Broadens coverage from top-250 to top-500 by volume so mid-cap
    gainers outside the top-250 (e.g. Arcblock-class tokens) still
    reach the gainers tracker and scorer.
    """
    page1 = [
        {
            "id": "p1-token",
            "symbol": "p1",
            "name": "Page1Token",
            "market_cap": 50_000_000,
            "total_volume": 10_000_000,
            "price_change_percentage_1h_in_currency": 2.0,
            "price_change_percentage_24h": 5.0,
        }
    ]
    page2 = [
        {
            "id": "p2-token",
            "symbol": "p2",
            "name": "Page2Token",
            "market_cap": 30_000_000,
            "total_volume": 3_000_000,
            "price_change_percentage_1h_in_currency": 4.0,
            "price_change_percentage_24h": 25.0,
        }
    ]
    settings = settings_factory(MIN_MARKET_CAP=1_000_000)
    with aioresponses() as mocked:
        mocked.get(MARKETS_PATTERN, payload=page1)
        mocked.get(MARKETS_PATTERN, payload=page2)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_by_volume(session, settings)

    tickers = {t.ticker for t in tokens}
    assert tickers == {"p1", "p2"}, f"expected both pages unioned, got {tickers}"


@pytest.mark.asyncio
async def test_fetch_by_volume_uses_configured_page_count(settings_factory):
    """Configured breadth controls how many volume_desc pages are scanned."""
    settings = settings_factory(
        MIN_MARKET_CAP=1_000,
        COINGECKO_VOLUME_SCAN_PAGES=3,
    )
    pages = [
        [
            {
                "id": f"page-{page}",
                "symbol": f"p{page}",
                "name": f"Page{page}",
                "market_cap": 1_000_000 + page,
                "total_volume": 10_000_000 - page,
                "price_change_percentage_24h": 20.0 + page,
            }
        ]
        for page in range(1, 4)
    ]

    with aioresponses() as mocked:
        for payload in pages:
            mocked.get(MARKETS_PATTERN, payload=payload)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_by_volume(session, settings)

    assert {t.contract_address for t in tokens} == {"page-1", "page-2", "page-3"}


@pytest.mark.asyncio
async def test_fetch_by_volume_page2_failure_still_returns_page1(settings_factory):
    """If page 2 errors, page 1 results are still returned (graceful degradation)."""
    page1 = [
        {
            "id": "p1-token",
            "symbol": "p1",
            "name": "Page1Token",
            "market_cap": 50_000_000,
            "total_volume": 10_000_000,
            "price_change_percentage_1h_in_currency": 2.0,
            "price_change_percentage_24h": 5.0,
        }
    ]
    settings = settings_factory(MIN_MARKET_CAP=1_000_000)
    with aioresponses() as mocked:
        mocked.get(MARKETS_PATTERN, payload=page1)
        mocked.get(MARKETS_PATTERN, status=500)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_by_volume(session, settings)

    assert len(tokens) == 1
    assert tokens[0].ticker == "p1"


@pytest.mark.asyncio
async def test_fetch_by_volume_outage_returns_empty(settings_factory):
    """Volume scan returns empty list on API failure."""
    settings = settings_factory()
    with aioresponses() as mocked:
        mocked.get(MARKETS_PATTERN, status=500)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_by_volume(session, settings)

    assert tokens == []


@pytest.mark.asyncio
async def test_fetch_midcap_gainers_filters_rank_band_and_sorts(settings_factory):
    """Rank-band scan keeps only quality 24h gainers and stores gated raw rows."""
    cg_module.last_raw_midcap_gainers.clear()
    settings = settings_factory(
        COINGECKO_MIDCAP_SCAN_ENABLED=True,
        COINGECKO_MIDCAP_SCAN_INTERVAL_CYCLES=1,
        COINGECKO_MIDCAP_SCAN_START_PAGE=2,
        COINGECKO_MIDCAP_SCAN_PAGES=2,
        COINGECKO_MIDCAP_SCAN_MIN_RANK=251,
        COINGECKO_MIDCAP_SCAN_MAX_RANK=1000,
        COINGECKO_MIDCAP_SCAN_MIN_24H_CHANGE=25.0,
        COINGECKO_MIDCAP_SCAN_MIN_VOLUME=250_000.0,
        COINGECKO_MIDCAP_SCAN_MIN_MCAP=10_000_000.0,
        COINGECKO_MIDCAP_SCAN_MAX_MCAP=500_000_000.0,
        COINGECKO_MIDCAP_SCAN_MAX_TOKENS_PER_CYCLE=2,
    )
    page2 = [
        {
            "id": "playnance-like",
            "symbol": "gcoin",
            "name": "PlaynanceLike",
            "market_cap_rank": 520,
            "market_cap": 90_000_000,
            "total_volume": 840_000,
            "current_price": 0.0023,
            "price_change_percentage_1h_in_currency": 3.0,
            "price_change_percentage_24h": 96.4,
            "price_change_percentage_7d_in_currency": 407.5,
        },
        {
            "id": "weak-gainer",
            "symbol": "weak",
            "name": "WeakGainer",
            "market_cap_rank": 600,
            "market_cap": 50_000_000,
            "total_volume": 900_000,
            "current_price": 0.02,
            "price_change_percentage_24h": 12.0,
        },
    ]
    page3 = [
        {
            "id": "safebit-like",
            "symbol": "safe",
            "name": "SAFEbitLike",
            "market_cap_rank": 683,
            "market_cap": 55_000_000,
            "total_volume": 860_000,
            "current_price": 0.084,
            "price_change_percentage_1h_in_currency": 1.2,
            "price_change_percentage_24h": 34.6,
            "price_change_percentage_7d_in_currency": 47.4,
        },
        {
            "id": "thin-volume",
            "symbol": "thin",
            "name": "ThinVolume",
            "market_cap_rank": 650,
            "market_cap": 40_000_000,
            "total_volume": 100_000,
            "current_price": 0.01,
            "price_change_percentage_24h": 80.0,
        },
    ]

    with aioresponses() as mocked:
        mocked.get(MARKETS_PATTERN, payload=page2)
        mocked.get(MARKETS_PATTERN, payload=page3)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_midcap_gainers(session, settings)

    assert [t.contract_address for t in tokens] == ["playnance-like", "safebit-like"]
    assert [row["id"] for row in cg_module.last_raw_midcap_gainers] == [
        "playnance-like",
        "safebit-like",
    ]
    samples = get_last_watchdog_samples()
    assert samples[-1].source == "coingecko:midcap"
    assert samples[-1].expected is True
    assert samples[-1].raw_count == 4
    assert samples[-1].usable_count == 2


@pytest.mark.asyncio
async def test_fetch_midcap_gainers_page_failure_preserves_success(settings_factory):
    """A failed rank-band page does not discard successful pages."""
    settings = settings_factory(
        COINGECKO_MIDCAP_SCAN_ENABLED=True,
        COINGECKO_MIDCAP_SCAN_INTERVAL_CYCLES=1,
        COINGECKO_MIDCAP_SCAN_START_PAGE=2,
        COINGECKO_MIDCAP_SCAN_PAGES=2,
        COINGECKO_MIDCAP_SCAN_MIN_RANK=251,
        COINGECKO_MIDCAP_SCAN_MAX_RANK=1000,
        COINGECKO_MIDCAP_SCAN_MIN_24H_CHANGE=25.0,
        COINGECKO_MIDCAP_SCAN_MIN_VOLUME=250_000.0,
        COINGECKO_MIDCAP_SCAN_MIN_MCAP=10_000_000.0,
        COINGECKO_MIDCAP_SCAN_MAX_MCAP=500_000_000.0,
    )
    page2 = [
        {
            "id": "survivor",
            "symbol": "srv",
            "name": "Survivor",
            "market_cap_rank": 550,
            "market_cap": 60_000_000,
            "total_volume": 500_000,
            "current_price": 0.06,
            "price_change_percentage_24h": 25.0,
        }
    ]

    with aioresponses() as mocked:
        mocked.get(MARKETS_PATTERN, payload=page2)
        mocked.get(MARKETS_PATTERN, status=500)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_midcap_gainers(session, settings)

    assert [t.contract_address for t in tokens] == ["survivor"]


@pytest.mark.asyncio
async def test_fetch_midcap_gainers_no_data_clears_stale_raw_cache(settings_factory):
    """Total outage clears stale raw rows so they cannot replay next cycle."""
    cg_module.last_raw_midcap_gainers[:] = [{"id": "stale"}]
    settings = settings_factory(
        COINGECKO_MIDCAP_SCAN_ENABLED=True,
        COINGECKO_MIDCAP_SCAN_INTERVAL_CYCLES=1,
        COINGECKO_MIDCAP_SCAN_START_PAGE=2,
        COINGECKO_MIDCAP_SCAN_PAGES=1,
    )

    with aioresponses() as mocked:
        mocked.get(MARKETS_PATTERN, status=500)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_midcap_gainers(session, settings)

    assert tokens == []
    assert cg_module.last_raw_midcap_gainers == []


@pytest.mark.asyncio
async def test_fetch_midcap_gainers_disabled_clears_raw_cache(settings_factory):
    """Disabled scan makes no HTTP call and clears stale raw rows."""
    cg_module.last_raw_midcap_gainers[:] = [{"id": "stale"}]
    settings = settings_factory(COINGECKO_MIDCAP_SCAN_ENABLED=False)

    with aioresponses() as mocked:
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_midcap_gainers(session, settings)

    assert tokens == []
    assert cg_module.last_raw_midcap_gainers == []
    assert len(mocked.requests) == 0


@pytest.mark.asyncio
async def test_fetch_midcap_gainers_off_cadence_clears_raw_cache(settings_factory):
    """Cadence gating prevents extra calls and clears stale raw rows."""
    _reset_midcap_scan_cycle_counter_for_tests()
    cg_module.last_raw_midcap_gainers[:] = [{"id": "stale"}]
    settings = settings_factory(
        COINGECKO_MIDCAP_SCAN_ENABLED=True,
        COINGECKO_MIDCAP_SCAN_INTERVAL_CYCLES=3,
    )

    with aioresponses() as mocked:
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_midcap_gainers(session, settings)

    assert tokens == []
    assert cg_module.last_raw_midcap_gainers == []
    assert len(mocked.requests) == 0
    samples = get_last_watchdog_samples()
    assert samples[-1].source == "coingecko:midcap"
    assert samples[-1].expected is False


@pytest.mark.asyncio
async def test_fetch_top_movers_watchdog_uses_raw_count_not_candidate_count(
    settings_factory,
):
    """Healthy raw fetch with zero usable candidates must not look starved."""
    settings = settings_factory(MIN_MARKET_CAP=999_999_999)
    with aioresponses() as mocked:
        mocked.get(MARKETS_PATTERN, payload=COINS_MARKETS_RESPONSE)
        mocked.get(MARKETS_PATTERN, payload=[])
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_top_movers(session, settings)

    assert tokens == []
    sample = next(
        s for s in get_last_watchdog_samples() if s.source == "coingecko:markets"
    )
    assert sample.source == "coingecko:markets"
    assert sample.raw_count == len(COINS_MARKETS_RESPONSE)
    assert sample.usable_count == 0


@pytest.mark.asyncio
async def test_fetch_trending_outage_returns_empty(settings_factory):
    """NFR: fetch_trending with 500 returns empty list, does not raise."""
    settings = settings_factory()
    with aioresponses() as mocked:
        mocked.get(TRENDING_PATTERN, status=500)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending(session, settings)

    assert tokens == []
