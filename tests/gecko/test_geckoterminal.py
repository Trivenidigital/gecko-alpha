import aiohttp
from aioresponses import aioresponses
from gecko.collectors.geckoterminal import (
    pool_features,
    token_address_from,
    collect_pools,
    collect_ohlcv,
    POOLS_URL,
    OHLCV_URL,
    NETWORK_MAP,
)

NOW = "2026-10-09T00:00:00Z"


def pool(
    addr="So1anaTokenAddr", network="solana", reserve="350000.5", pool_addr="PoolAddr1"
):
    return {
        "id": f"{network}_{pool_addr}",
        "attributes": {
            "address": pool_addr,
            "name": "ABC / SOL",
            "base_token_price_usd": "0.0123",
            "pool_created_at": "2026-09-20T10:00:00Z",
            "fdv_usd": "1500000",
            "market_cap_usd": None,
            "reserve_in_usd": reserve,
            "volume_usd": {"h24": "88000.1"},
            "transactions": {"h24": {"buys": 300, "sells": 210}},
        },
        "relationships": {"base_token": {"data": {"id": f"{network}_{addr}"}}},
    }


def test_network_map_covers_supported_chains():
    from gecko.settings import SUPPORTED_CHAINS

    assert set(NETWORK_MAP) == set(SUPPORTED_CHAINS)


def test_token_address_from():
    # raw (case-preserved) address; normalization is gecko.identity's job
    assert token_address_from("solana_So1anaTokenAddr", "solana") == "So1anaTokenAddr"
    assert token_address_from("eth_0xAbC_weird", "eth") == "0xAbC_weird"


def test_pool_features_types():
    f = pool_features(pool()["attributes"], "new_pools")
    assert (
        f["price_usd"] == 0.0123
        and f["liquidity_usd"] == 350000.5
        and f["mcap_usd"] is None
    )
    assert (
        f["fdv_usd"] == 1500000.0
        and f["buys_24h"] == 300
        and f["source_list"] == "new_pools"
    )
    assert (
        f["pool_created_at"] == "2026-09-20T10:00:00Z"
        and f["pool_address"] == "PoolAddr1"
    )
    assert set(f) == {
        "price_usd",
        "liquidity_usd",
        "fdv_usd",
        "mcap_usd",
        "vol_24h_usd",
        "buys_24h",
        "sells_24h",
        "pool_created_at",
        "pool_address",
        "source_list",
    }


async def test_collect_pools_all_chains_and_band(store, settings):
    settings.chains = ["solana", "ethereum"]
    with aioresponses() as m:
        m.get(
            POOLS_URL.format(network="solana", list="new_pools"),
            payload={"data": [pool()]},
        )
        m.get(
            POOLS_URL.format(network="solana", list="trending_pools"),
            payload={"data": [pool(reserve="10")]},
        )
        m.get(POOLS_URL.format(network="eth", list="new_pools"), payload={"data": []})
        m.get(
            POOLS_URL.format(network="eth", list="trending_pools"),
            payload={"data": [pool(addr="0xEEE", network="eth", pool_addr="0xpool")]},
        )
        async with aiohttp.ClientSession() as s:
            r = await collect_pools(store, settings, s, NOW)
    assert r.rows_written == 2
    rows = await (
        await store.conn.execute(
            "SELECT token_id, chain, pool_created_at FROM tokens ORDER BY token_id"
        )
    ).fetchall()
    assert rows == [
        ("ethereum:0xeee", "ethereum", "2026-09-20T10:00:00Z"),
        ("solana:So1anaTokenAddr", "solana", "2026-09-20T10:00:00Z"),
    ]


async def test_collect_pools_zero_mcap_is_not_replaced_by_fdv(store, settings):
    # mcap 0.0 is a reported value (out of band), not "missing": no fdv fallback
    settings.chains = ["solana"]
    p = pool()
    p["attributes"]["market_cap_usd"] = "0"
    with aioresponses() as m:
        m.get(
            POOLS_URL.format(network="solana", list="new_pools"), payload={"data": [p]}
        )
        m.get(
            POOLS_URL.format(network="solana", list="trending_pools"),
            payload={"data": []},
        )
        async with aiohttp.ClientSession() as s:
            r = await collect_pools(store, settings, s, NOW)
    assert r.rows_written == 0


async def test_collect_ohlcv_sorted_ascending_and_budgeted(store, settings):
    settings.ohlcv_max_per_run = 1
    for tid, pa in (("solana:a", "pa"), ("solana:b", "pb")):
        await store.upsert_token(tid, "solana", tid[-1], "X", "X", NOW, "geckoterminal")
        await store.add_observation(
            tid, "geckoterminal", NOW, {"pool_address": pa, "liquidity_usd": 3e5}
        )
    with aioresponses() as m:
        m.get(
            OHLCV_URL.format(network="solana", pool="pa"),
            payload={
                "data": {
                    "attributes": {
                        "ohlcv_list": [[200, 1, 1, 1, 1, 5.0], [100, 1, 1, 1, 1, 3.0]]
                    }
                }
            },
        )
        async with aiohttp.ClientSession() as s:
            r = await collect_ohlcv(store, settings, s, NOW)
    assert r.rows_written == 1
    obs = await store.observations("solana:a", as_of=NOW, source="geckoterminal_ohlcv")
    assert obs[0].features["candles"] == [
        [100, 1, 1, 1, 1, 3.0],
        [200, 1, 1, 1, 1, 5.0],
    ]
    assert obs[0].features["pool_address"] == "pa"
    assert (
        await store.observations("solana:b", as_of=NOW, source="geckoterminal_ohlcv")
        == []
    )


async def test_collect_ohlcv_poolless_tokens_do_not_consume_budget(store, settings):
    # A token with no known pool sorts first (never had OHLCV) forever; it must not
    # starve the budget and block tokens that do have a pool.
    settings.ohlcv_max_per_run = 1
    await store.upsert_token("solana:a", "solana", "a", "X", "X", NOW, "coingecko")
    await store.upsert_token("solana:b", "solana", "b", "X", "X", NOW, "dexscreener")
    await store.add_observation(
        "solana:b", "dexscreener", NOW, {"pair_address": "pb", "liquidity_usd": 3e5}
    )
    with aioresponses() as m:
        m.get(
            OHLCV_URL.format(network="solana", pool="pb"),
            payload={"data": {"attributes": {"ohlcv_list": [[100, 1, 1, 1, 1, 3.0]]}}},
        )
        async with aiohttp.ClientSession() as s:
            r = await collect_ohlcv(store, settings, s, NOW)
    assert r.rows_written == 1
    obs = await store.observations("solana:b", as_of=NOW, source="geckoterminal_ohlcv")
    assert obs[0].features == {
        "pool_address": "pb",
        "candles": [[100.0, 1.0, 1.0, 1.0, 1.0, 3.0]],
    }


async def test_collect_ohlcv_oldest_first(store, settings):
    settings.ohlcv_max_per_run = 1
    for tid, pa in (("solana:a", "pa"), ("solana:b", "pb")):
        await store.upsert_token(tid, "solana", tid[-1], "X", "X", NOW, "geckoterminal")
        await store.add_observation(
            tid, "geckoterminal", NOW, {"pool_address": pa, "liquidity_usd": 3e5}
        )
    await store.add_observation(
        "solana:a", "geckoterminal_ohlcv", "2026-10-08T00:00:00Z", {"candles": []}
    )
    with aioresponses() as m:
        m.get(
            OHLCV_URL.format(network="solana", pool="pb"),
            payload={"data": {"attributes": {"ohlcv_list": [[100, 1, 1, 1, 1, 3.0]]}}},
        )
        async with aiohttp.ClientSession() as s:
            r = await collect_ohlcv(store, settings, s, NOW)
    assert r.rows_written == 1
    assert (
        len(
            await store.observations(
                "solana:b", as_of=NOW, source="geckoterminal_ohlcv"
            )
        )
        == 1
    )


def test_jobs_registered_and_ohlcv_is_an_observation_writer():
    from gecko.__main__ import _load_jobs
    from gecko.freshness import WRITER_JOBS
    from gecko.jobs import JOBS

    _load_jobs()
    assert {"geckoterminal", "geckoterminal-ohlcv"} <= set(JOBS)
    assert {"geckoterminal", "geckoterminal-ohlcv"} <= set(WRITER_JOBS["observations"])
