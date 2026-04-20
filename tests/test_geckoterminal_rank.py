"""Test gt_trending_rank capture in fetch_trending_pools (BL-052)."""

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.config import Settings
from scout.ingestion.geckoterminal import fetch_trending_pools


def _pool(addr, name="TestPool / SOL", fdv=100_000.0, liq=20_000.0, vol=80_000.0):
    return {
        "attributes": {
            "name": name,
            "fdv_usd": fdv,
            "reserve_in_usd": liq,
            "volume_usd": {"h24": vol},
        },
        "relationships": {"base_token": {"data": {"id": f"solana_{addr}"}}},
    }


@pytest.fixture
def settings(settings_factory):
    # Use the shared settings_factory fixture from tests/conftest.py for
    # consistency with tests/test_geckoterminal.py. This avoids hand-rolling
    # required kwargs and keeps the test's config surface aligned with sibling
    # tests.
    return settings_factory(
        CHAINS=["solana"],
        MIN_MARKET_CAP=10_000,
        MAX_MARKET_CAP=500_000,
    )


async def test_fetch_trending_pools_assigns_rank_by_index(settings):
    pools = [_pool("addr1"), _pool("addr2"), _pool("addr3")]
    with aioresponses() as m:
        m.get(
            "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools",
            payload={"data": pools},
        )
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_pools(session, settings)

    ranks = [t.gt_trending_rank for t in tokens]
    addrs = [t.contract_address for t in tokens]
    assert ranks == [1, 2, 3]
    assert addrs == ["addr1", "addr2", "addr3"]


async def test_fetch_trending_pools_empty_data_emits_nothing(settings):
    with aioresponses() as m:
        m.get(
            "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools",
            payload={"data": []},
        )
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_pools(session, settings)
    assert tokens == []


async def test_fetch_trending_pools_skips_malformed_but_preserves_rank_order(settings):
    # idx 0 = valid, idx 1 = malformed (fdv_usd raises ValueError on float()), idx 2 = valid.
    # NB: a truly empty {"attributes": {}, "relationships": {}} does NOT raise in
    # from_geckoterminal (it produces contract_address="" + mcap=0 which is then
    # filtered by the mcap floor, NOT the except path). Using a non-numeric fdv
    # triggers the intended exception path.
    pools = [
        _pool("good1"),
        {"attributes": {"fdv_usd": "KABOOM"}, "relationships": {}},
        _pool("good3"),
    ]
    with aioresponses() as m:
        m.get(
            "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools",
            payload={"data": pools},
        )
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_pools(session, settings)

    # Rank 2 is "burned" (idx 1 failed); ranks stay positional, not compacted.
    ranks = [t.gt_trending_rank for t in tokens]
    addrs = [t.contract_address for t in tokens]
    assert addrs == ["good1", "good3"]
    assert ranks == [1, 3]
