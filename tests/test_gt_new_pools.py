"""DEX-first Phase 1 — GT new-pools research lane (design_dex_first_discovery_2026_07_20).

Observe-only: discoveries persist to dex_pool_discoveries + contract_coin_map;
no candidate emission, no scoring, no alerts, no paper trades. Flag off ⇒
byte-identical pipeline (no HTTP call, no rows).
"""

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.db import Database
from scout.ingestion import gt_new_pools

NEW_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"


def _pool(
    pool_addr="PoolAddr111",
    mint="Mint111",
    name="DOGCAT / SOL",
    fdv=50_000.0,
    reserve=5_000.0,
    created="2026-07-20T01:00:00Z",
):
    return {
        "id": f"solana_{pool_addr}",
        "attributes": {
            "address": pool_addr,
            "name": name,
            "pool_created_at": created,
            "fdv_usd": str(fdv),
            "reserve_in_usd": str(reserve),
            "volume_usd": {"h1": "1234.5"},
        },
        "relationships": {
            "base_token": {"data": {"id": f"solana_{mint}"}},
            "quote_token": {
                "data": {"id": "solana_So11111111111111111111111111111111111111112"}
            },
        },
    }


async def _db(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    return db


@pytest.fixture(autouse=True)
def _reset_cycle_counter():
    gt_new_pools._poll_cycle_counter = 0
    yield
    gt_new_pools._poll_cycle_counter = 0


# ---------------------------------------------------------------- migration


async def test_migration_creates_table_and_version(tmp_path):
    db = await _db(tmp_path)
    cur = await db._conn.execute(
        "SELECT description FROM schema_version WHERE version=20260720"
    )
    assert (await cur.fetchone())[0] == "dex_discovery_v1"
    cur = await db._conn.execute("SELECT COUNT(*) FROM dex_pool_discoveries")
    assert (await cur.fetchone())[0] == 0
    await db.close()


# ---------------------------------------------------------------- flag off


async def test_flag_off_no_http_no_rows(tmp_path, settings_factory):
    db = await _db(tmp_path)
    settings = settings_factory(DEX_DISCOVERY_ENABLED=False)
    with aioresponses():  # any HTTP call would raise (no mocks registered)
        async with aiohttp.ClientSession() as session:
            n = await gt_new_pools.discover_new_pools(session, db, settings)
    assert n == 0
    cur = await db._conn.execute("SELECT COUNT(*) FROM dex_pool_discoveries")
    assert (await cur.fetchone())[0] == 0
    await db.close()


# ---------------------------------------------------------------- happy path


async def test_discovery_records_pool_and_identity(tmp_path, settings_factory):
    db = await _db(tmp_path)
    settings = settings_factory(
        DEX_DISCOVERY_ENABLED=True,
        DEX_DISCOVERY_POLL_EVERY_N_CYCLES=1,
        DEX_DISCOVERY_MIN_LIQUIDITY_USD=1000.0,
    )
    with aioresponses() as m:
        m.get(NEW_POOLS_URL, payload={"data": [_pool()]})
        async with aiohttp.ClientSession() as session:
            n = await gt_new_pools.discover_new_pools(session, db, settings)
    assert n == 1
    cur = await db._conn.execute(
        "SELECT network, pool_address, base_token_address, base_token_symbol, "
        "quote_token_symbol, fdv_usd, liquidity_usd, volume_h1_usd, "
        "pool_created_at FROM dex_pool_discoveries"
    )
    row = await cur.fetchone()
    assert row[0] == "solana"
    assert row[1] == "PoolAddr111"
    assert row[2] == "Mint111"
    assert row[3] == "DOGCAT"
    assert row[4] == "SOL"
    assert row[5] == 50_000.0
    assert row[6] == 5_000.0
    assert row[7] == 1234.5
    assert row[8] == "2026-07-20T01:00:00Z"
    # forward identity: contract_coin_map row, coin_id NULL, source tagged
    cur = await db._conn.execute(
        "SELECT coin_id, source FROM contract_coin_map "
        "WHERE contract_address='Mint111' AND chain='solana'"
    )
    mrow = await cur.fetchone()
    assert mrow is not None
    assert mrow[0] is None
    assert mrow[1] == "gt_new_pools"
    await db.close()


# ---------------------------------------------------------------- dedup


async def test_rediscovery_is_deduped(tmp_path, settings_factory):
    db = await _db(tmp_path)
    settings = settings_factory(
        DEX_DISCOVERY_ENABLED=True, DEX_DISCOVERY_POLL_EVERY_N_CYCLES=1
    )
    with aioresponses() as m:
        m.get(NEW_POOLS_URL, payload={"data": [_pool()]})
        m.get(NEW_POOLS_URL, payload={"data": [_pool()]})
        async with aiohttp.ClientSession() as session:
            n1 = await gt_new_pools.discover_new_pools(session, db, settings)
            n2 = await gt_new_pools.discover_new_pools(session, db, settings)
    assert (n1, n2) == (1, 0)
    cur = await db._conn.execute("SELECT COUNT(*) FROM dex_pool_discoveries")
    assert (await cur.fetchone())[0] == 1
    await db.close()


# ---------------------------------------------------------------- filters


async def test_dust_pool_filtered_by_min_liquidity(tmp_path, settings_factory):
    db = await _db(tmp_path)
    settings = settings_factory(
        DEX_DISCOVERY_ENABLED=True,
        DEX_DISCOVERY_POLL_EVERY_N_CYCLES=1,
        DEX_DISCOVERY_MIN_LIQUIDITY_USD=1000.0,
    )
    with aioresponses() as m:
        m.get(NEW_POOLS_URL, payload={"data": [_pool(reserve=50.0)]})
        async with aiohttp.ClientSession() as session:
            n = await gt_new_pools.discover_new_pools(session, db, settings)
    assert n == 0
    await db.close()


async def test_malformed_pool_skipped_not_fatal(tmp_path, settings_factory):
    db = await _db(tmp_path)
    settings = settings_factory(
        DEX_DISCOVERY_ENABLED=True, DEX_DISCOVERY_POLL_EVERY_N_CYCLES=1
    )
    broken = {"id": "solana_x", "attributes": None, "relationships": {}}
    with aioresponses() as m:
        m.get(NEW_POOLS_URL, payload={"data": [broken, _pool()]})
        async with aiohttp.ClientSession() as session:
            n = await gt_new_pools.discover_new_pools(session, db, settings)
    assert n == 1
    await db.close()


# ---------------------------------------------------------------- cadence


async def test_poll_every_n_cycles_gates_http(tmp_path, settings_factory):
    db = await _db(tmp_path)
    settings = settings_factory(
        DEX_DISCOVERY_ENABLED=True, DEX_DISCOVERY_POLL_EVERY_N_CYCLES=3
    )
    with aioresponses() as m:
        # exactly ONE mock registered: only cycle 1 of 3 may call HTTP
        m.get(NEW_POOLS_URL, payload={"data": [_pool()]})
        async with aiohttp.ClientSession() as session:
            n1 = await gt_new_pools.discover_new_pools(session, db, settings)
            n2 = await gt_new_pools.discover_new_pools(session, db, settings)
            n3 = await gt_new_pools.discover_new_pools(session, db, settings)
    assert (n1, n2, n3) == (1, 0, 0)
    await db.close()


# ---------------------------------------------------------------- resilience


async def test_http_error_returns_zero_never_raises(tmp_path, settings_factory):
    db = await _db(tmp_path)
    settings = settings_factory(
        DEX_DISCOVERY_ENABLED=True, DEX_DISCOVERY_POLL_EVERY_N_CYCLES=1
    )
    with aioresponses() as m:
        m.get(NEW_POOLS_URL, status=404)
        async with aiohttp.ClientSession() as session:
            n = await gt_new_pools.discover_new_pools(session, db, settings)
    assert n == 0
    await db.close()


# ---------------------------------------------------------------- settings


def test_settings_defaults_are_safe(settings_factory):
    s = settings_factory()
    assert s.DEX_DISCOVERY_ENABLED is False
    assert s.DEX_DISCOVERY_NETWORKS == ["solana"]
    assert s.DEX_DISCOVERY_POLL_EVERY_N_CYCLES >= 1
    assert s.DEX_DISCOVERY_MIN_LIQUIDITY_USD >= 0
