import aiohttp
from aioresponses import aioresponses
from structlog.testing import capture_logs
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
    addr="0xAbC0000000000000000000000000000000000000",
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
    assert (
        best_pair(pairs, "0xabc0000000000000000000000000000000000000", "base")[
            "liquidity"
        ]["usd"]
        == 500_000
    )


def test_pair_missing_fields_skipped():
    p = pair()
    p["priceUsd"] = None
    p["liquidity"] = None
    assert best_pair([p], "0xabc0000000000000000000000000000000000000", "base") is None
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
                {
                    "chainId": "base",
                    "tokenAddress": "0xAbC0000000000000000000000000000000000000",
                },
                {"chainId": "tron", "tokenAddress": "Txyz"},
            ],
        )
        m.get(
            PROFILES_URL,
            payload=[
                {
                    "chainId": "base",
                    "tokenAddress": "0xabc0000000000000000000000000000000000000",
                },
                {
                    "chainId": "solana",
                    "tokenAddress": "So1ana111111111111111111111111111111",
                },
            ],
        )
        m.get(
            TOKENS_URL.format(
                chain="base", addresses="0xabc0000000000000000000000000000000000000"
            ),
            payload=[pair()],
        )
        # Solana base58 addresses are case-sensitive: requested verbatim, not lowercased.
        m.get(
            TOKENS_URL.format(
                chain="solana", addresses="So1ana111111111111111111111111111111"
            ),
            payload=[
                pair(
                    addr="So1ana111111111111111111111111111111", chain="solana", liq=100
                )
            ],
        )  # below floor
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW)
    assert r.rows_written == 1
    tok = await (
        await store.conn.execute(
            "SELECT token_id, chain, symbol, status, pool_created_at FROM tokens"
        )
    ).fetchall()
    assert tok == [
        (
            "base:0xabc0000000000000000000000000000000000000",
            "base",
            "ABC",
            "candidate",
            "2025-09-27T19:06:40Z",
        )
    ]
    obs = await store.observations(
        "base:0xabc0000000000000000000000000000000000000", as_of=NOW
    )
    assert (
        obs[0].features["liquidity_usd"] == 400_000
        and obs[0].features["buys_24h"] == 120
    )


async def test_collect_preserves_solana_address_case(store, settings):
    with aioresponses() as m:
        m.get(
            BOOSTS_URL,
            payload=[
                {
                    "chainId": "solana",
                    "tokenAddress": "So1ana111111111111111111111111111111Mint",
                }
            ],
        )
        m.get(PROFILES_URL, payload=[])
        m.get(
            TOKENS_URL.format(
                chain="solana", addresses="So1ana111111111111111111111111111111Mint"
            ),
            payload=[
                pair(addr="So1ana111111111111111111111111111111Mint", chain="solana")
            ],
        )
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW)
    assert r.rows_written == 1
    tok = await (
        await store.conn.execute("SELECT token_id, address FROM tokens")
    ).fetchall()
    assert tok == [
        (
            "solana:So1ana111111111111111111111111111111Mint",
            "So1ana111111111111111111111111111111Mint",
        )
    ]


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
    assert best_pair([p], "So1ana111111111111111111111111111111Mint", "solana") is None
    assert (
        best_pair(
            [pair(addr="So1ana111111111111111111111111111111Mint", chain="solana")],
            "So1ana111111111111111111111111111111Mint",
            "solana",
        )
        is not None
    )


def test_best_pair_evm_is_case_insensitive():
    assert (
        best_pair(
            [pair(addr="0xABC0000000000000000000000000000000000000")],
            "0xabc0000000000000000000000000000000000000",
            "base",
        )
        is not None
    )


async def test_collect_skips_unpriced_pair_without_raising(store, settings):
    with aioresponses() as m:
        m.get(
            BOOSTS_URL,
            payload=[
                {
                    "chainId": "base",
                    "tokenAddress": "0xaaa0000000000000000000000000000000000000",
                },
                {
                    "chainId": "base",
                    "tokenAddress": "0xbbb0000000000000000000000000000000000000",
                },
            ],
        )
        m.get(PROFILES_URL, payload=[])
        m.get(
            TOKENS_URL.format(
                chain="base",
                addresses="0xaaa0000000000000000000000000000000000000,0xbbb0000000000000000000000000000000000000",
            ),
            payload=[
                pair(addr="0xaaa0000000000000000000000000000000000000", price=None),
                pair(addr="0xbbb0000000000000000000000000000000000000"),
            ],
        )
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW)
    assert r.rows_written == 1
    tok = await (await store.conn.execute("SELECT token_id FROM tokens")).fetchall()
    assert tok == [("base:0xbbb0000000000000000000000000000000000000",)]


async def test_collect_zero_mcap_not_replaced_by_fdv(store, settings):
    # mcap 0.0 is out of band; an `or` fallback would wrongly admit it via fdv.
    with aioresponses() as m:
        m.get(
            BOOSTS_URL,
            payload=[
                {
                    "chainId": "base",
                    "tokenAddress": "0xabc0000000000000000000000000000000000000",
                }
            ],
        )
        m.get(PROFILES_URL, payload=[])
        m.get(
            TOKENS_URL.format(
                chain="base", addresses="0xabc0000000000000000000000000000000000000"
            ),
            payload=[pair(mcap=0.0)],
        )
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW)
    assert r.rows_written == 0


async def _known(store, addr, chain="base", status="candidate"):
    tid = f"{chain}:{addr}"
    await store.upsert_token(tid, chain, addr, "K", "K", NOW, "dexscreener")
    if status != "candidate":
        await store.set_token_status(tid, status, "x")
    return tid


async def test_refresh_reobserves_known_tokens_off_feed_below_floor(store, settings):
    cand = await _known(store, "0xaaa0000000000000000000000000000000000000")
    act = await _known(
        store, "0xbbb0000000000000000000000000000000000000", status="active"
    )
    held = await _known(store, "0xccc", status="disqualified")
    await store.watchlist_upsert(held, NOW, "F4", 1, 1.0, {})
    await _known(store, "0xddd", status="dead")  # not refreshed
    await _known(store, "0xeee")  # no pair returned: nothing written
    await store.upsert_token("cg:x", "cg", "x", "X", "X", NOW, "coingecko")
    await store.upsert_token(
        "benchmark:btc", "benchmark", "btc", "BTC", "B", NOW, "coingecko"
    )
    with aioresponses() as m:
        m.get(BOOSTS_URL, payload=[])
        m.get(PROFILES_URL, payload=[])
        m.get(
            TOKENS_URL.format(
                chain="base",
                addresses="0xaaa0000000000000000000000000000000000000,0xbbb0000000000000000000000000000000000000,0xccc,0xeee",
            ),
            payload=[
                pair(addr="0xaaa0000000000000000000000000000000000000", liq=10_000),
                pair(
                    addr="0xbbb0000000000000000000000000000000000000",
                    liq=300_000,
                    mcap=9e9,
                ),
                pair(addr="0xccc", liq=5),
            ],
        )
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                r = await collect(store, settings, s, NOW)
        assert len(m.requests) == 3  # boosts, profiles, one refresh batch
    assert r.rows_written == 3
    for tid, liq in ((cand, 10_000), (act, 300_000), (held, 5)):
        (o,) = await store.observations(tid, as_of=NOW)
        assert o.features["refresh"] is True and o.features["liquidity_usd"] == liq
    assert await store.observations("base:0xeee", as_of=NOW) == []
    assert await store.observations("base:0xddd", as_of=NOW) == []
    done = [e for e in logs if e["event"] == "dexscreener_refresh_done"]
    assert done[0]["refreshed"] == 3 and done[0]["missing"] == 1
    # refresh never re-upserts identity
    st = await (
        await store.conn.execute("SELECT status FROM tokens WHERE token_id=?", (held,))
    ).fetchone()
    assert st == ("disqualified",)


async def test_known_token_in_feed_below_floor_is_kept_and_fetched_once(
    store, settings
):
    tid = await _known(store, "0xabc0000000000000000000000000000000000000")
    with aioresponses() as m:
        m.get(
            BOOSTS_URL,
            payload=[
                {
                    "chainId": "base",
                    "tokenAddress": "0xAbC0000000000000000000000000000000000000",
                }
            ],
        )
        m.get(PROFILES_URL, payload=[])
        m.get(
            TOKENS_URL.format(
                chain="base", addresses="0xabc0000000000000000000000000000000000000"
            ),
            payload=[pair(liq=50_000)],
        )
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW)
        assert len(m.requests) == 3
    assert r.rows_written == 1
    (o,) = await store.observations(tid, as_of=NOW)
    assert o.features["refresh"] is True and o.features["liquidity_usd"] == 50_000


async def test_refresh_batches_thirty_and_skips_other_chains(store, settings):
    settings.chains = ["base"]
    addrs = [f"0x{i:040x}" for i in range(31)]
    for a in addrs:
        await _known(store, a)
    await _known(store, "So1", chain="solana")
    with aioresponses() as m:
        m.get(BOOSTS_URL, payload=[])
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


async def test_malformed_feed_addresses_never_reach_a_url(store, settings):
    good = "0x" + "a" * 40
    with aioresponses() as m:
        m.get(
            BOOSTS_URL,
            payload=[
                {"chainId": "base", "tokenAddress": "0xabc/../x"},
                {"chainId": "solana", "tokenAddress": "bad?mint=1"},
                {"chainId": "base", "tokenAddress": good},
            ],
        )
        m.get(PROFILES_URL, payload=[])
        m.get(
            TOKENS_URL.format(chain="base", addresses=good), payload=[pair(addr=good)]
        )
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                r = await collect(store, settings, s, NOW)
        assert len(m.requests) == 3
    assert r.rows_written == 1
    done = [e for e in logs if e["event"] == "dexscreener_done"]
    assert done[0]["invalid"] == 2
