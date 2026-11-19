"""Seam test: the real collectors, admission, ranking and ledger, end to end.

No observation is inserted directly; every row comes from a collector run
against mocked upstreams, so a defect at any hand-off between modules shows.
"""

import re
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aioresponses import aioresponses

from gecko.collectors import coingecko, dexscreener, geckoterminal
from gecko.ledger import CANARY_TOKENS, measure_due
from gecko.rank import rank_once

MINT = "GeckoSeamMint1111111111111111111111111111111"  # 44-char base58
TID = f"solana:{MINT}"
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
PAIR = "SeamPair11111111111111111111111111111111111"
TOKENS = dexscreener.TOKENS_URL.format(chain="solana", addresses=MINT)
OHLCV = geckoterminal.OHLCV_URL.format(network="solana", pool=PAIR)
TRENDING = re.compile(re.escape(coingecko.BASE + "/search/trending"))
PRICE = re.compile(re.escape(coingecko.BASE + "/simple/price") + r"\?.*")


def at(hours: float) -> str:
    return (T0 + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def nosleep(_):
    return None


def pair(liq: float, price: float = 1.0) -> dict:
    return {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": PAIR,
        "baseToken": {"address": MINT, "name": "Seam", "symbol": "SEAM"},
        "priceUsd": str(price),
        "liquidity": {"usd": liq},
        "volume": {"h24": 1e5},
        "marketCap": 2e6,
        "fdv": 2e6,
        "pairCreatedAt": int((T0 - timedelta(days=30)).timestamp() * 1000),
    }


async def dex_run(store, settings, hours, liq, *, in_feed, price=1.0):
    feed = [{"chainId": "solana", "tokenAddress": MINT}] if in_feed else []
    with aioresponses() as m:
        m.get(dexscreener.BOOSTS_URL, payload=feed)
        m.get(dexscreener.PROFILES_URL, payload=[])
        m.get(TOKENS, payload=[pair(liq, price)])
        async with aiohttp.ClientSession() as s:
            await dexscreener.collect(store, settings, s, at(hours), sleep=nosleep)


async def btc_run(store, settings, hours, usd):
    with aioresponses() as m:
        m.get(TRENDING, payload={"coins": []})
        m.get(PRICE, payload={"bitcoin": {"usd": usd}})
        async with aiohttp.ClientSession() as s:
            await coingecko.hourly(store, settings, s, at(hours), sleep=nosleep)


async def status(store):
    row = await (
        await store.conn.execute(
            "SELECT status, disqualify_reason FROM tokens WHERE token_id=?", (TID,)
        )
    ).fetchone()
    return row


async def test_discovery_to_measured_return_to_exit(store, settings):
    settings.chains = ["solana"]
    # (a) admission across a dip that only a refresh read can see
    await dex_run(store, settings, 0, 300_000, in_feed=True)
    assert await status(store) == ("candidate", None)
    await dex_run(store, settings, 12, 50_000, in_feed=True)  # dip, below floor
    await dex_run(store, settings, 25, 300_000, in_feed=True)
    assert await status(store) == ("candidate", None)  # dip reset the window
    await dex_run(store, settings, 49, 300_000, in_feed=True)
    await dex_run(store, settings, 73, 300_000, in_feed=True)
    assert (await status(store))[0] == "active"

    # (b) OHLCV from the pool DexScreener reported; rank enters the token
    entry_h = 74
    day0 = int(datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp())
    candles = [[day0 + i * 86400, 1, 1, 1, 1, 100 if i < 7 else 200] for i in range(14)]
    with aioresponses() as m:
        m.get(OHLCV, payload={"data": {"attributes": {"ohlcv_list": candles}}})
        async with aiohttp.ClientSession() as s:
            await geckoterminal.collect_ohlcv(
                store, settings, s, at(entry_h), sleep=nosleep
            )
    await btc_run(store, settings, entry_h, 60_000.0)
    await dex_run(store, settings, entry_h, 300_000, in_feed=True, price=1.0)
    r = await rank_once(store, settings, at(entry_h))
    assert r.entered == [TID]
    due = await store.due_returns(at(entry_h + 24 * 200))
    assert sorted(d.horizon_days for d in due) == [1, 7, 30, 90, 180]

    # (c) off the feed, still refreshed; the +1d return is measured
    await dex_run(store, settings, entry_h + 23, 300_000, in_feed=False, price=1.5)
    (o,) = await store.observations(TID, as_of=at(entry_h + 23), limit=1)
    assert o.observed_at == at(entry_h + 23) and o.features["refresh"] is True
    await btc_run(store, settings, entry_h + 24, 66_000.0)
    with aioresponses() as m:
        m.get(TOKENS, payload=[{"priceUsd": "2.0", "liquidity": {"usd": 300_000}}])
        m.get(
            dexscreener.TOKENS_URL.format(
                chain="solana", addresses=CANARY_TOKENS["solana"]
            ),
            payload=[{"priceUsd": "150", "liquidity": {"usd": 1e8}}],
        )
        async with aiohttp.ClientSession() as s:
            rep = await measure_due(store, settings, s, at(entry_h + 24), sleep=nosleep)
    assert rep.measured == 1
    row = await (
        await store.conn.execute(
            "SELECT entry_price, ret, ret_vs_btc FROM forward_returns "
            "WHERE token_id=? AND horizon_days=1",
            (TID,),
        )
    ).fetchone()
    assert row[0] == 1.0 and row[1] == pytest.approx(1.0)
    assert row[2] == pytest.approx(0.9)

    # (d) a later refresh reads 10k liquidity; the next rank exits on liquidity
    await dex_run(store, settings, entry_h + 36, 10_000, in_feed=False)
    r = await rank_once(store, settings, at(entry_h + 37))
    assert r.exited == [TID]
    reason = await (
        await store.conn.execute(
            "SELECT exit_reason FROM watchlist WHERE token_id=?", (TID,)
        )
    ).fetchone()
    assert reason == ("liquidity",)
    assert await status(store) == ("disqualified", "liquidity")
