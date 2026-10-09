import aiohttp
from aioresponses import aioresponses
from gecko.collectors.dexscreener import (
    best_pair,
    pair_features,
    collect,
    BOOSTS_URL,
    PROFILES_URL,
    TOKENS_URL,
)

NOW = "2026-10-09T00:00:00Z"


def pair(
    addr="0xAbC",
    chain="base",
    price="1.25",
    liq=400_000,
    fdv=2e6,
    mcap=1.8e6,
    created=1_759_000_000_000,
):
    return {
        "chainId": chain,
        "dexId": "uniswap",
        "pairAddress": "0xpair",
        "baseToken": {"address": addr, "name": "Abc Coin", "symbol": "ABC"},
        "quoteToken": {"address": "0xweth", "symbol": "WETH"},
        "priceUsd": price,
        "txns": {"h24": {"buys": 120, "sells": 80}},
        "volume": {"h24": 55_000.5},
        "liquidity": {"usd": liq},
        "fdv": fdv,
        "marketCap": mcap,
        "pairCreatedAt": created,
    }


def test_best_pair_picks_deepest_matching_base():
    pairs = [pair(liq=10), pair(liq=500_000), pair(addr="0xother", liq=9e9)]
    assert best_pair(pairs, "0xabc", "base")["liquidity"]["usd"] == 500_000


def test_pair_missing_fields_skipped():
    p = pair()
    p["priceUsd"] = None
    p["liquidity"] = None
    assert best_pair([p], "0xabc", "base") is None
    q = pair()
    del q["marketCap"]
    q["volume"] = {}
    f = pair_features(q)
    assert f["mcap_usd"] is None and f["vol_24h_usd"] is None and f["price_usd"] == 1.25


def test_pair_features_iso_created():
    assert pair_features(pair())["pair_created_at"] == "2025-09-27T19:06:40Z"


async def test_collect_writes_tokens_and_observations(store, settings):
    with aioresponses() as m:
        m.get(
            BOOSTS_URL,
            payload=[
                {"chainId": "base", "tokenAddress": "0xAbC"},
                {"chainId": "tron", "tokenAddress": "Txyz"},
            ],
        )
        m.get(
            PROFILES_URL,
            payload=[
                {"chainId": "base", "tokenAddress": "0xabc"},
                {"chainId": "solana", "tokenAddress": "So1ana"},
            ],
        )
        m.get(TOKENS_URL.format(chain="base", addresses="0xabc"), payload=[pair()])
        # Solana base58 addresses are case-sensitive: requested verbatim, not lowercased.
        m.get(
            TOKENS_URL.format(chain="solana", addresses="So1ana"),
            payload=[pair(addr="So1ana", chain="solana", liq=100)],
        )  # below floor
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW)
    assert r.rows_written == 1
    tok = await (
        await store.conn.execute(
            "SELECT token_id, chain, symbol, status, pool_created_at FROM tokens"
        )
    ).fetchall()
    assert tok == [("base:0xabc", "base", "ABC", "candidate", "2025-09-27T19:06:40Z")]
    obs = await store.observations("base:0xabc", as_of=NOW)
    assert (
        obs[0].features["liquidity_usd"] == 400_000
        and obs[0].features["buys_24h"] == 120
    )


async def test_collect_preserves_solana_address_case(store, settings):
    with aioresponses() as m:
        m.get(BOOSTS_URL, payload=[{"chainId": "solana", "tokenAddress": "So1anaMint"}])
        m.get(PROFILES_URL, payload=[])
        m.get(
            TOKENS_URL.format(chain="solana", addresses="So1anaMint"),
            payload=[pair(addr="So1anaMint", chain="solana")],
        )
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW)
    assert r.rows_written == 1
    tok = await (
        await store.conn.execute("SELECT token_id, address FROM tokens")
    ).fetchall()
    assert tok == [("solana:So1anaMint", "So1anaMint")]


async def test_collect_batches_thirty(store, settings):
    addrs = [f"0x{i:040x}" for i in range(31)]
    with aioresponses() as m:
        m.get(
            BOOSTS_URL, payload=[{"chainId": "base", "tokenAddress": a} for a in addrs]
        )
        m.get(PROFILES_URL, payload=[])
        m.get(
            TOKENS_URL.format(chain="base", addresses=",".join(addrs[:30])),
            payload=[pair(addr=a) for a in addrs[:30]],
        )
        m.get(
            TOKENS_URL.format(chain="base", addresses=addrs[30]),
            payload=[pair(addr=addrs[30])],
        )
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW)
    assert r.rows_written == 31


def test_best_pair_solana_is_case_exact():
    p = pair(addr="so1anamint", chain="solana")
    assert best_pair([p], "So1anaMint", "solana") is None
    assert (
        best_pair([pair(addr="So1anaMint", chain="solana")], "So1anaMint", "solana")
        is not None
    )


def test_best_pair_evm_is_case_insensitive():
    assert best_pair([pair(addr="0xABC")], "0xabc", "base") is not None


async def test_collect_skips_unpriced_pair_without_raising(store, settings):
    with aioresponses() as m:
        m.get(
            BOOSTS_URL,
            payload=[
                {"chainId": "base", "tokenAddress": "0xaaa"},
                {"chainId": "base", "tokenAddress": "0xbbb"},
            ],
        )
        m.get(PROFILES_URL, payload=[])
        m.get(
            TOKENS_URL.format(chain="base", addresses="0xaaa,0xbbb"),
            payload=[pair(addr="0xaaa", price=None), pair(addr="0xbbb")],
        )
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW)
    assert r.rows_written == 1
    tok = await (await store.conn.execute("SELECT token_id FROM tokens")).fetchall()
    assert tok == [("base:0xbbb",)]


async def test_collect_zero_mcap_not_replaced_by_fdv(store, settings):
    # mcap 0.0 is out of band; an `or` fallback would wrongly admit it via fdv.
    with aioresponses() as m:
        m.get(BOOSTS_URL, payload=[{"chainId": "base", "tokenAddress": "0xabc"}])
        m.get(PROFILES_URL, payload=[])
        m.get(
            TOKENS_URL.format(chain="base", addresses="0xabc"), payload=[pair(mcap=0.0)]
        )
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW)
    assert r.rows_written == 0
