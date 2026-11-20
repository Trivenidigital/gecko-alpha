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


async def test_resolver_failure_records_attempt_and_ttl_skips_next_pass(db, settings_factory):
    settings = settings_factory(
        DEX_RESOLVER_BUDGET_PER_CYCLE=5, DEX_RESOLVER_NEGATIVE_TTL_SEC=3600
    )
    with aioresponses() as m:
        m.get(_url("ghost-coin"), status=404)
        async with aiohttp.ClientSession() as s:
            r1 = await run_resolver_pass(["ghost-coin"], s, db, settings)
    assert r1["failed"] == 1
    assert await db.coin_id_attempt_fresh("ghost-coin", 3600) is True
    # next pass: TTL skips it -> no budget consumed, no fetch needed
    with aioresponses():
        async with aiohttp.ClientSession() as s:
            r2 = await run_resolver_pass(["ghost-coin"], s, db, settings)
    assert r2["attempted"] == 0


# --- a resolved mapping is never downgraded to "unresolved" ------------------
# The new-pools lane stamps coin_id=NULL for every pool it sees, on the same
# (contract_address, chain) key the resolver writes. The upsert used to copy
# that NULL over an existing resolution whenever the chain names matched.


async def _map_row(db, contract, chain):
    cur = await db._conn.execute(
        "SELECT coin_id, source, confidence FROM contract_coin_map "
        "WHERE contract_address = ? AND chain = ?",
        (contract, chain),
    )
    row = await cur.fetchone()
    return None if row is None else tuple(row)


async def test_null_write_does_not_erase_a_resolved_mapping(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db.record_contract_coin_map("MintAAA", "solana", "bonk", "platforms", "high")

    await db.record_contract_coin_map("MintAAA", "solana", None, "gt_new_pools", None)

    assert await _map_row(db, "MintAAA", "solana") == ("bonk", "platforms", "high")
    await db.close()


async def test_resolution_still_upserts_over_an_attempted_marker(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db.record_contract_coin_map("MintBBB", "solana", None, "gt_new_pools", None)

    await db.record_contract_coin_map("MintBBB", "solana", "wif", "platforms", "high")

    assert await _map_row(db, "MintBBB", "solana") == ("wif", "platforms", "high")
    await db.close()


async def test_a_new_resolution_replaces_an_older_one(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db.record_contract_coin_map("MintCCC", "solana", "old-id", "platforms", "low")

    await db.record_contract_coin_map(
        "MintCCC", "solana", "new-id", "platforms", "high"
    )

    assert await _map_row(db, "MintCCC", "solana") == ("new-id", "platforms", "high")
    await db.close()


async def test_repeated_attempted_marker_refreshes_its_timestamp(tmp_path):
    """NULL over NULL is still written (the attempted marker's timestamp
    moves). Nothing reads that timestamp today — the resolver's negative TTL
    lives in the separate ``__attempt__`` rows — so this only pins that the
    guard is no wider than "do not erase a resolution"."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db.record_contract_coin_map("MintDDD", "solana", None, "gt_new_pools", None)
    await db._conn.execute(
        "UPDATE contract_coin_map SET resolved_at = '2020-01-01T00:00:00+00:00' "
        "WHERE contract_address = 'MintDDD'"
    )
    await db._conn.commit()

    await db.record_contract_coin_map("MintDDD", "solana", None, "gt_new_pools", None)

    cur = await db._conn.execute(
        "SELECT resolved_at FROM contract_coin_map WHERE contract_address = 'MintDDD'"
    )
    assert (await cur.fetchone())[0] > "2026"
    await db.close()
