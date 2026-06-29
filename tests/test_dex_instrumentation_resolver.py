"""C2 (HTTP) — I1 resolver orchestration. CI-only (imports aiohttp).

Verifies platforms parsing, best-effort failure handling, per-cycle budget, and
the already-resolved TTL skip. Observe-only.
"""

import re

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.db import Database
from scout.instrumentation.resolver import resolve_coin_platforms, run_resolver_pass

SOL = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
WSOL = "So11111111111111111111111111111111111111112"


@pytest.fixture(autouse=True)
def _clear_detail_cache():
    from scout.counter import detail

    detail._detail_cache.clear()
    yield
    detail._detail_cache.clear()


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "resolver.db")
    await d.initialize()
    yield d
    await d.close()


def _url(coin_id: str):
    return re.compile(
        rf"https://api\.coingecko\.com/api/v3/coins/{coin_id}(\?.*)?$"
    )


async def test_resolve_records_platform_contracts(db, settings_factory):
    settings = settings_factory()
    with aioresponses() as m:
        m.get(
            _url("the-black-bull"),
            payload={"id": "the-black-bull", "platforms": {"solana": SOL}},
        )
        async with aiohttp.ClientSession() as s:
            n = await resolve_coin_platforms("the-black-bull", s, db, settings)
    assert n == 1
    assert await db.coin_id_resolved("the-black-bull") is True


async def test_resolve_fetch_failure_returns_none(db, settings_factory):
    settings = settings_factory()
    with aioresponses() as m:
        m.get(_url("ghost-coin"), status=500)
        async with aiohttp.ClientSession() as s:
            n = await resolve_coin_platforms("ghost-coin", s, db, settings)
    assert n is None


async def test_resolver_pass_respects_budget(db, settings_factory):
    settings = settings_factory(DEX_RESOLVER_BUDGET_PER_CYCLE=1)
    with aioresponses() as m:
        m.get(_url("coin-one"), payload={"id": "coin-one", "platforms": {"solana": WSOL}})
        m.get(_url("coin-two"), payload={"id": "coin-two", "platforms": {"ethereum": "0x" + "a" * 40}})
        async with aiohttp.ClientSession() as s:
            result = await run_resolver_pass(["coin-one", "coin-two"], s, db, settings)
    assert result["attempted"] == 1


async def test_resolver_pass_skips_already_resolved(db, settings_factory):
    settings = settings_factory(DEX_RESOLVER_BUDGET_PER_CYCLE=5)
    await db.record_contract_coin_map(WSOL, "solana", "coin-one", "platforms", "high")
    with aioresponses() as m:
        m.get(_url("coin-two"), payload={"id": "coin-two", "platforms": {"ethereum": "0x" + "b" * 40}})
        async with aiohttp.ClientSession() as s:
            result = await run_resolver_pass(["coin-one", "coin-two"], s, db, settings)
    assert result["attempted"] == 1  # coin-one skipped (already resolved)
