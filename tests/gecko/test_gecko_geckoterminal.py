import aiohttp
from aioresponses import aioresponses
from structlog.testing import capture_logs
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
LATER = "2026-10-10T00:00:00Z"


async def nosleep(_):
    return None


def ohlcv(rows):
    return {"data": {"attributes": {"ohlcv_list": rows}}}


def pool(
    addr="So1anaTokenAddr11111111111111111111",
    network="solana",
    reserve="350000.5",
    pool_addr="PoolAddr1",
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
    assert (
        token_address_from("solana_So1anaTokenAddr11111111111111111111", "solana")
        == "So1anaTokenAddr11111111111111111111"
    )
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
            payload={
                "data": [
                    pool(
                        addr="0xEEE0000000000000000000000000000000000000",
                        network="eth",
                        pool_addr="0xpool",
                    )
                ]
            },
        )
        async with aiohttp.ClientSession() as s:
            r = await collect_pools(store, settings, s, NOW, sleep=nosleep)
    assert r.rows_written == 2
    rows = await (
        await store.conn.execute(
            "SELECT token_id, chain, pool_created_at FROM tokens ORDER BY token_id"
        )
    ).fetchall()
    assert rows == [
        (
            "ethereum:0xeee0000000000000000000000000000000000000",
            "ethereum",
            "2026-09-20T10:00:00Z",
        ),
        (
            "solana:So1anaTokenAddr11111111111111111111",
            "solana",
            "2026-09-20T10:00:00Z",
        ),
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
            r = await collect_pools(store, settings, s, NOW, sleep=nosleep)
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
            r = await collect_ohlcv(store, settings, s, NOW, sleep=nosleep)
    assert r.rows_written == 1
    obs = await store.observations("solana:a", as_of=NOW, source="geckoterminal_ohlcv")
    assert obs[0].features["candles"] == [
        [100, 1, 1, 1, 1, 3.0],
        [200, 1, 1, 1, 1, 5.0],
    ]
    assert obs[0].features["pool_address"] == "pa"
    assert obs[0].features["status"] == "ok"
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
            r = await collect_ohlcv(store, settings, s, NOW, sleep=nosleep)
    assert r.rows_written == 1
    obs = await store.observations("solana:b", as_of=NOW, source="geckoterminal_ohlcv")
    assert obs[0].features == {
        "pool_address": "pb",
        "candles": [[100.0, 1.0, 1.0, 1.0, 1.0, 3.0]],
        "status": "ok",
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
            r = await collect_ohlcv(store, settings, s, NOW, sleep=nosleep)
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


async def _two_pooled_tokens(store, pools=("pa", "pb")):
    for tid, pa in zip(("solana:a", "solana:b"), pools):
        await store.upsert_token(tid, "solana", tid[-1], "X", "X", NOW, "geckoterminal")
        await store.add_observation(
            tid, "geckoterminal", NOW, {"pool_address": pa, "liquidity_usd": 3e5}
        )


async def test_pools_paced_one_wait_between_each_request(store, settings):
    settings.chains = ["solana", "ethereum"]
    slept = []

    async def rec(d):
        slept.append(d)

    with aioresponses() as m:
        for net in ("solana", "eth"):
            for lst in ("new_pools", "trending_pools"):
                m.get(POOLS_URL.format(network=net, list=lst), payload={"data": []})
        async with aiohttp.ClientSession() as s:
            await collect_pools(store, settings, s, NOW, sleep=rec)
    assert len(slept) == 3  # 4 requests -> 3 paced gaps
    assert all(0 < d <= settings.gt_min_interval_sec for d in slept)


async def test_ohlcv_paced_one_wait_between_each_request(store, settings):
    await _two_pooled_tokens(store)
    slept = []

    async def rec(d):
        slept.append(d)

    with aioresponses() as m:
        m.get(
            OHLCV_URL.format(network="solana", pool="pa"),
            payload=ohlcv([[1, 1, 1, 1, 1, 1]]),
        )
        m.get(
            OHLCV_URL.format(network="solana", pool="pb"),
            payload=ohlcv([[1, 1, 1, 1, 1, 1]]),
        )
        async with aiohttp.ClientSession() as s:
            r = await collect_ohlcv(store, settings, s, NOW, sleep=rec)
    assert r.rows_written == 2 and len(slept) == 1


async def test_ohlcv_dead_pool_rotates_instead_of_starving(store, settings):
    settings.ohlcv_max_per_run = 1
    await _two_pooled_tokens(store, pools=("dead", "pb"))
    with aioresponses() as m:
        m.get(OHLCV_URL.format(network="solana", pool="dead"), status=404)
        m.get(
            OHLCV_URL.format(network="solana", pool="pb"),
            payload=ohlcv([[1, 1, 1, 1, 1, 1]]),
        )
        async with aiohttp.ClientSession() as s:
            await collect_ohlcv(store, settings, s, NOW, sleep=nosleep)
            await collect_ohlcv(store, settings, s, LATER, sleep=nosleep)
    dead = await store.observations(
        "solana:a", as_of=LATER, source="geckoterminal_ohlcv"
    )
    good = await store.observations(
        "solana:b", as_of=LATER, source="geckoterminal_ohlcv"
    )
    assert [o.features for o in dead] == [
        {"pool_address": "dead", "candles": [], "status": "not_found"}
    ]
    assert [(o.observed_at, o.features["status"]) for o in good] == [(LATER, "ok")]


async def test_ohlcv_empty_list_writes_empty_marker(store, settings):
    await store.upsert_token("solana:a", "solana", "a", "X", "X", NOW, "geckoterminal")
    await store.add_observation(
        "solana:a", "geckoterminal", NOW, {"pool_address": "pa", "liquidity_usd": 3e5}
    )
    with aioresponses() as m:
        m.get(OHLCV_URL.format(network="solana", pool="pa"), payload=ohlcv([]))
        async with aiohttp.ClientSession() as s:
            await collect_ohlcv(store, settings, s, NOW, sleep=nosleep)
    obs = await store.observations("solana:a", as_of=NOW, source="geckoterminal_ohlcv")
    assert [o.features for o in obs] == [
        {"pool_address": "pa", "candles": [], "status": "empty"}
    ]


async def test_ohlcv_error_writes_marker_and_continues(store, settings):
    await _two_pooled_tokens(store)
    with aioresponses() as m:
        m.get(OHLCV_URL.format(network="solana", pool="pa"), status=401)
        m.get(
            OHLCV_URL.format(network="solana", pool="pb"),
            payload=ohlcv([[1, 1, 1, 1, 1, 5]]),
        )
        async with aiohttp.ClientSession() as s:
            r = await collect_ohlcv(store, settings, s, NOW, sleep=nosleep)
    assert r.rows_written == 2
    (a,) = await store.observations("solana:a", as_of=NOW, source="geckoterminal_ohlcv")
    assert a.features == {"pool_address": "pa", "candles": [], "status": "error"}
    (b,) = await store.observations("solana:b", as_of=NOW, source="geckoterminal_ohlcv")
    assert b.features["status"] == "ok"


async def test_ohlcv_run_fails_only_when_every_fetch_failed(store, settings):
    import pytest

    await _two_pooled_tokens(store)
    with aioresponses() as m:
        m.get(OHLCV_URL.format(network="solana", pool="pa"), status=401)
        m.get(OHLCV_URL.format(network="solana", pool="pb"), status=403)
        async with aiohttp.ClientSession() as s:
            with pytest.raises(RuntimeError, match="all 2 geckoterminal ohlcv"):
                await collect_ohlcv(store, settings, s, NOW, sleep=nosleep)
    # markers were still written, so both tokens rotate to the back
    for tid in ("solana:a", "solana:b"):
        obs = await store.observations(tid, as_of=NOW, source="geckoterminal_ohlcv")
        assert obs[0].features["status"] == "error"


async def test_ohlcv_picks_deepest_pool_in_latest_snapshot(store, settings):
    await store.upsert_token("solana:a", "solana", "a", "X", "X", NOW, "geckoterminal")
    older = "2026-10-08T00:00:00Z"
    await store.add_observation(
        "solana:a",
        "geckoterminal",
        older,
        {"pool_address": "old", "liquidity_usd": 9e9},
    )
    # deep sits between two shallower pools, so neither a first-row nor a
    # last-row pick (whatever the tie order of equal observed_at) can pass
    for pa, liq in (("shallow1", 3e5), ("deep", 8e5), ("shallow2", 4e5)):
        await store.add_observation(
            "solana:a", "geckoterminal", NOW, {"pool_address": pa, "liquidity_usd": liq}
        )
    await store.add_observation(
        "solana:a", "dexscreener", NOW, {"pair_address": "dspair", "liquidity_usd": 9e9}
    )
    with aioresponses() as m:
        m.get(
            OHLCV_URL.format(network="solana", pool="deep"),
            payload=ohlcv([[1, 1, 1, 1, 1, 1]]),
        )
        async with aiohttp.ClientSession() as s:
            await collect_ohlcv(store, settings, s, NOW, sleep=nosleep)
    obs = await store.observations("solana:a", as_of=NOW, source="geckoterminal_ohlcv")
    assert obs[0].features["pool_address"] == "deep"


async def test_ohlcv_equal_liquidity_tiebreak_by_pool_address(store, settings):
    await store.upsert_token("solana:a", "solana", "a", "X", "X", NOW, "geckoterminal")
    for pa in ("zz", "mm"):
        await store.add_observation(
            "solana:a", "geckoterminal", NOW, {"pool_address": pa, "liquidity_usd": 3e5}
        )
    with aioresponses() as m:
        m.get(
            OHLCV_URL.format(network="solana", pool="mm"),
            payload=ohlcv([[1, 1, 1, 1, 1, 1]]),
        )
        async with aiohttp.ClientSession() as s:
            await collect_ohlcv(store, settings, s, NOW, sleep=nosleep)
    obs = await store.observations("solana:a", as_of=NOW, source="geckoterminal_ohlcv")
    assert obs[0].features["pool_address"] == "mm"


async def test_ohlcv_malformed_candle_skipped_not_fatal(store, settings):
    await store.upsert_token("solana:a", "solana", "a", "X", "X", NOW, "geckoterminal")
    await store.add_observation(
        "solana:a", "geckoterminal", NOW, {"pool_address": "pa", "liquidity_usd": 3e5}
    )
    rows = [
        [300, 1, 1, 1, 1, 2.0],
        [200, None, 1, 1, 1, 1.0],
        [100, 1, 1, 1, 1, "x"],
        [50, 1, 1, 1, 1, 4.0],
    ]
    with aioresponses() as m:
        m.get(OHLCV_URL.format(network="solana", pool="pa"), payload=ohlcv(rows))
        async with aiohttp.ClientSession() as s:
            r = await collect_ohlcv(store, settings, s, NOW, sleep=nosleep)
    assert r.rows_written == 1
    obs = await store.observations("solana:a", as_of=NOW, source="geckoterminal_ohlcv")
    assert obs[0].features["candles"] == [[50, 1, 1, 1, 1, 4.0], [300, 1, 1, 1, 1, 2.0]]
    assert obs[0].features["status"] == "ok"


async def test_collect_pools_skips_invalid_address_and_bad_pool_timestamp(
    store, settings
):
    settings.chains = ["solana"]
    bad_ts = pool(addr="So1anaSecond" + "1" * 30)
    bad_ts["attributes"]["pool_created_at"] = "yesterday"
    with aioresponses() as m:
        m.get(
            POOLS_URL.format(network="solana", list="new_pools"),
            payload={"data": [pool(addr="not/valid"), bad_ts, pool()]},
        )
        m.get(
            POOLS_URL.format(network="solana", list="trending_pools"),
            payload={"data": []},
        )
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                r = await collect_pools(store, settings, s, NOW, sleep=nosleep)
    assert r.rows_written == 1
    toks = await (await store.conn.execute("SELECT token_id FROM tokens")).fetchall()
    assert toks == [("solana:So1anaTokenAddr" + "1" * 20,)]
    done = [e for e in logs if e["event"] == "geckoterminal_pools_done"]
    assert done[0]["invalid"] == 1 and done[0]["skipped"] == 2
    assert [e["value"] for e in logs if e["event"] == "gt_pool_created_at_invalid"] == [
        "'yesterday'"
    ]


def _failing_network(monkeypatch, network, status=500):
    import gecko.collectors.geckoterminal as gt
    from gecko.http import UpstreamError

    real = gt.get_json

    async def stub(session, url, **kw):
        if f"/networks/{network}/" in url:
            raise UpstreamError(status, url)
        return await real(session, url, **kw)

    monkeypatch.setattr(gt, "get_json", stub)


async def test_one_network_down_does_not_stop_the_others(store, settings, monkeypatch):
    settings.chains = ["solana", "ethereum"]
    _failing_network(monkeypatch, "solana")
    evm = "0x" + "e" * 40
    with aioresponses() as m:
        m.get(POOLS_URL.format(network="eth", list="new_pools"), payload={"data": []})
        m.get(
            POOLS_URL.format(network="eth", list="trending_pools"),
            payload={"data": [pool(addr=evm, network="eth", pool_addr="0xpool")]},
        )
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                r = await collect_pools(store, settings, s, NOW, sleep=nosleep)
    assert r.rows_written == 1
    failed = [e for e in logs if e["event"] == "geckoterminal_list_failed"]
    assert [(e["network"], e["status"]) for e in failed] == [("solana", 500)] * 2
    done = [e for e in logs if e["event"] == "geckoterminal_pools_done"][0]
    assert (done["lists"], done["lists_failed"]) == (4, 2)


async def test_every_list_failing_fails_the_run(store, settings, monkeypatch):
    import pytest

    settings.chains = ["solana"]
    _failing_network(monkeypatch, "solana")
    async with aiohttp.ClientSession() as s:
        with pytest.raises(RuntimeError, match="all 2 geckoterminal pool lists"):
            await collect_pools(store, settings, s, NOW, sleep=nosleep)


async def test_stale_candidate_back_in_band_pool_is_revived(store, settings):
    settings.chains = ["solana"]
    addr = "So1anaTokenAddr" + "1" * 20
    tid = f"solana:{addr}"
    await store.upsert_token(tid, "solana", addr, "A", "A", "2026-09-01T00:00:00Z", "x")
    await store.set_token_status(tid, "disqualified", "stale_candidate")
    with aioresponses() as m:
        m.get(
            POOLS_URL.format(network="solana", list="new_pools"),
            payload={"data": [pool()]},
        )
        m.get(
            POOLS_URL.format(network="solana", list="trending_pools"),
            payload={"data": []},
        )
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                await collect_pools(store, settings, s, NOW, sleep=nosleep)
    row = await (
        await store.conn.execute(
            "SELECT status, disqualify_reason, first_seen_at FROM tokens"
        )
    ).fetchone()
    assert row == ("candidate", None, NOW)
    done = [e for e in logs if e["event"] == "geckoterminal_pools_done"][0]
    assert done["revived"] == 1
