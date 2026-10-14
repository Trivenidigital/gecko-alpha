import aiohttp
from aioresponses import aioresponses
from structlog.testing import capture_logs
from gecko.calls import insert_call
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
            r = await collect(store, settings, s, NOW, sleep=nosleep)
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
            r = await collect(store, settings, s, NOW, sleep=nosleep)
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
            r = await collect(store, settings, s, NOW, sleep=nosleep)
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
            r = await collect(store, settings, s, NOW, sleep=nosleep)
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
            r = await collect(store, settings, s, NOW, sleep=nosleep)
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
                r = await collect(store, settings, s, NOW, sleep=nosleep)
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
            r = await collect(store, settings, s, NOW, sleep=nosleep)
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
            r = await collect(store, settings, s, NOW, sleep=nosleep)
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
                r = await collect(store, settings, s, NOW, sleep=nosleep)
        assert len(m.requests) == 3
    assert r.rows_written == 1
    done = [e for e in logs if e["event"] == "dexscreener_done"]
    assert done[0]["invalid"] == 2


async def nosleep(_):
    return None


class RecordingSleep:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


A40 = "0x" + "1" * 40
B40 = "0x" + "2" * 40
C40 = "0x" + "3" * 40


async def test_every_request_is_paced(store, settings):
    settings.ds_min_interval_sec = 0.25
    await _known(store, B40)
    sleep = RecordingSleep()
    with aioresponses() as m:
        m.get(BOOSTS_URL, payload=[{"chainId": "base", "tokenAddress": A40}])
        m.get(PROFILES_URL, payload=[])
        m.get(TOKENS_URL.format(chain="base", addresses=A40), payload=[pair(addr=A40)])
        m.get(TOKENS_URL.format(chain="base", addresses=B40), payload=[pair(addr=B40)])
        async with aiohttp.ClientSession() as s:
            await collect(store, settings, s, NOW, sleep=sleep)
        n = sum(len(v) for v in m.requests.values())
    assert n == 4 and len(sleep.calls) == n - 1
    assert all(0 < d <= 0.25 for d in sleep.calls)


async def test_refresh_cap_takes_oldest_reading_first(store, settings):
    settings.ds_refresh_max_per_run = 2
    for a in (A40, B40, C40):
        await _known(store, a)
    await store.add_observation(
        f"base:{A40}", "dexscreener", "2026-10-01T00:00:00Z", {}
    )
    await store.add_observation(
        f"base:{C40}", "dexscreener", "2026-10-08T00:00:00Z", {}
    )
    # B was never read: first; then A (oldest); C (newest) is deferred
    with aioresponses() as m:
        m.get(BOOSTS_URL, payload=[])
        m.get(PROFILES_URL, payload=[])
        m.get(
            TOKENS_URL.format(chain="base", addresses=f"{A40},{B40}"),
            payload=[pair(addr=A40), pair(addr=B40)],
        )
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                r = await collect(store, settings, s, NOW, sleep=nosleep)
        assert len(m.requests) == 3
    assert r.rows_written == 2
    done = [e for e in logs if e["event"] == "dexscreener_refresh_done"][0]
    assert done["planned"] == 2 and done["deferred"] == 1
    assert len(await store.observations(f"base:{C40}", as_of=NOW)) == 1  # untouched


async def test_failed_batch_does_not_stop_next_batch_or_admission(store, settings):
    addrs = [f"0x{i:040x}" for i in range(31)]
    for a in addrs:
        await _known(store, a)
    with aioresponses() as m:
        m.get(BOOSTS_URL, payload=[])
        m.get(PROFILES_URL, payload=[])
        m.get(
            TOKENS_URL.format(chain="base", addresses=",".join(addrs[:30])), status=400
        )
        m.get(
            TOKENS_URL.format(chain="base", addresses=addrs[30]),
            payload=[pair(addr=addrs[30])],
        )
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                r = await collect(store, settings, s, NOW, sleep=nosleep)
    assert r.rows_written == 1
    failed = [e["status"] for e in logs if e["event"] == "dexscreener_batch_failed"]
    assert failed == [400]
    done = [e for e in logs if e["event"] == "dexscreener_done"][0]
    assert (done["batches"], done["batches_failed"]) == (2, 1)
    assert "promoted" in done  # admission ran


def _failing_feeds(monkeypatch, urls, status=503):
    import gecko.collectors.dexscreener as ds
    from gecko.http import UpstreamError

    real = ds.get_json

    async def stub(session, url, **kw):
        if url in urls:
            raise UpstreamError(status, url)
        return await real(session, url, **kw)

    monkeypatch.setattr(ds, "get_json", stub)


async def test_all_batches_failing_with_feeds_up_does_not_fail_the_run(store, settings):
    await _known(store, A40)
    with aioresponses() as m:
        m.get(BOOSTS_URL, payload=[])
        m.get(PROFILES_URL, payload=[])
        m.get(TOKENS_URL.format(chain="base", addresses=A40), status=400)
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW, sleep=nosleep)
    assert r.rows_written == 0


async def test_run_fails_only_when_every_feed_and_batch_failed(
    store, settings, monkeypatch
):
    import pytest

    _failing_feeds(monkeypatch, {BOOSTS_URL, PROFILES_URL})
    await _known(store, A40)
    with aioresponses() as m:
        m.get(TOKENS_URL.format(chain="base", addresses=A40), status=400)
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                with pytest.raises(RuntimeError, match="every dexscreener feed"):
                    await collect(store, settings, s, NOW, sleep=nosleep)
    assert [e for e in logs if e["event"] == "dexscreener_done"]  # admission ran first


async def test_both_feeds_down_still_refreshes(store, settings, monkeypatch):
    _failing_feeds(monkeypatch, {BOOSTS_URL, PROFILES_URL})
    await _known(store, A40)
    with aioresponses() as m:
        m.get(TOKENS_URL.format(chain="base", addresses=A40), payload=[pair(addr=A40)])
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW, sleep=nosleep)
    assert r.rows_written == 1


async def test_failed_boosts_feed_still_discovers_profiles_and_refreshes(
    store, settings, monkeypatch
):
    _failing_feeds(monkeypatch, {BOOSTS_URL})
    await _known(store, B40)
    with aioresponses() as m:
        m.get(PROFILES_URL, payload=[{"chainId": "base", "tokenAddress": A40}])
        m.get(TOKENS_URL.format(chain="base", addresses=A40), payload=[pair(addr=A40)])
        m.get(TOKENS_URL.format(chain="base", addresses=B40), payload=[pair(addr=B40)])
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                r = await collect(store, settings, s, NOW, sleep=nosleep)
    assert r.rows_written == 2
    failed = [e for e in logs if e["event"] == "dexscreener_feed_failed"]
    assert [(e["url"], e["status"]) for e in failed] == [(BOOSTS_URL, 503)]
    done = [e for e in logs if e["event"] == "dexscreener_done"][0]
    assert done["feeds_failed"] == 1
    (o,) = await store.observations(f"base:{B40}", as_of=NOW)
    assert o.features["refresh"] is True


async def test_stale_candidates_are_aged_out_and_not_refreshed(store, settings):
    for a, seen in ((A40, "2026-09-24T00:00:00Z"), (B40, "2026-09-26T00:00:00Z")):
        await store.upsert_token(f"base:{a}", "base", a, "K", "K", seen, "dexscreener")
    with aioresponses() as m:
        m.get(BOOSTS_URL, payload=[])
        m.get(PROFILES_URL, payload=[])
        m.get(TOKENS_URL.format(chain="base", addresses=B40), payload=[pair(addr=B40)])
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW, sleep=nosleep)
        assert len(m.requests) == 3
    assert r.rows_written == 1
    rows = await (
        await store.conn.execute(
            "SELECT token_id, status, disqualify_reason FROM tokens ORDER BY token_id"
        )
    ).fetchall()
    assert rows == [
        (f"base:{A40}", "disqualified", "stale_candidate"),  # 15 days old
        (f"base:{B40}", "candidate", None),  # 13 days old
    ]


async def test_stale_candidate_back_in_band_is_revived(store, settings):
    tid = f"base:{A40}"
    await store.upsert_token(tid, "base", A40, "K", "K", "2026-09-01T00:00:00Z", "x")
    await store.set_token_status(tid, "disqualified", "stale_candidate")
    with aioresponses() as m:
        m.get(BOOSTS_URL, payload=[{"chainId": "base", "tokenAddress": A40}])
        m.get(PROFILES_URL, payload=[])
        m.get(TOKENS_URL.format(chain="base", addresses=A40), payload=[pair(addr=A40)])
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                await collect(store, settings, s, NOW, sleep=nosleep)
    row = await (
        await store.conn.execute(
            "SELECT status, disqualify_reason, first_seen_at FROM tokens"
        )
    ).fetchone()
    assert row == ("candidate", None, NOW)
    done = [e for e in logs if e["event"] == "dexscreener_done"][0]
    assert done["revived"] == 1


async def test_called_address_is_discovered_with_telegram_source(store, settings):
    addr = "0xabc0000000000000000000000000000000000000"
    settings.chains = ["base"]
    await insert_call(
        store,
        source_type="telegram",
        source_id="@c",
        event_id="tg:@c:1",
        posted_at="2026-10-08T23:00:00Z",
        text=f"ape {addr}",
    )
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BOOSTS_URL, payload=[])
            m.get(PROFILES_URL, payload=[])
            m.get(
                TOKENS_URL.format(chain="base", addresses=addr),
                payload=[pair(addr=addr)],
            )
            await collect(store, settings, s, NOW, sleep=nosleep)
    row = await (
        await store.conn.execute(
            "SELECT first_source FROM tokens WHERE token_id=?", (f"base:{addr}",)
        )
    ).fetchone()
    assert row == ("telegram",)


async def test_seed_also_in_feed_keeps_dexscreener_source(store, settings):
    addr = "0xabc0000000000000000000000000000000000000"
    settings.chains = ["base"]
    await insert_call(
        store,
        source_type="telegram",
        source_id="@c",
        event_id="tg:@c:1",
        posted_at="2026-10-08T23:00:00Z",
        text=addr,
    )
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BOOSTS_URL, payload=[{"chainId": "base", "tokenAddress": addr}])
            m.get(PROFILES_URL, payload=[])
            m.get(
                TOKENS_URL.format(chain="base", addresses=addr),
                payload=[pair(addr=addr)],
            )
            await collect(store, settings, s, NOW, sleep=nosleep)
    row = await (
        await store.conn.execute(
            "SELECT first_source FROM tokens WHERE token_id=?", (f"base:{addr}",)
        )
    ).fetchone()
    assert row == ("dexscreener",)
