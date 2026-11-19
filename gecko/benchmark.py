"""Keyless BTC benchmark: WBTC on Ethereum, priced by GeckoTerminal.

The ledger cannot measure a return without ``benchmark:btc`` near both ends;
this keeps the benchmark alive when CoinGecko (``coingecko-hourly``) is dark.
"""

from __future__ import annotations

import asyncio

import aiohttp
import structlog

from gecko.collectors.geckoterminal import BASE, HEADERS
from gecko.http import Pacer, get_json
from gecko.jobs import register
from gecko.numeric import num
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, utcnow

log = structlog.get_logger("gecko.benchmark")
BTC_ID = "benchmark:btc"
WBTC = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"  # within ~0.2 % of BTC
PRICE_URL = BASE + "/simple/networks/eth/token_price/" + WBTC


async def upsert_btc(store: Store, now: str, source: str) -> None:
    """The ``benchmark:btc`` token row, never a candidate (admission, OHLCV and
    rank scans skip it). Shared by every BTC writer."""
    await store.upsert_token(
        BTC_ID, "benchmark", "btc", "BTC", "Bitcoin", now, source, cg_id="bitcoin"
    )
    await store.set_token_status(BTC_ID, "disqualified", "benchmark")


def _wbtc_price(body):
    """The raw WBTC price from a token_price body; None on any other shape."""
    node = body
    for key in ("data", "attributes", "token_prices"):
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        return None
    return {str(k).lower(): v for k, v in node.items()}.get(WBTC)


async def fetch_btc(
    store: Store,
    settings: GeckoSettings,
    session: aiohttp.ClientSession,
    now: str,
    sleep=asyncio.sleep,
) -> JobResult:
    now = canon_ts(now)
    await Pacer(settings.gt_min_interval_sec, sleep).wait()
    body = await get_json(
        session, PRICE_URL, settings=settings, headers=HEADERS, sleep=sleep
    )
    p = num(_wbtc_price(body))
    if p is None or p <= 0:
        raise RuntimeError("btc price invalid")
    await upsert_btc(store, now, "geckoterminal")
    await store.add_observation(BTC_ID, "geckoterminal", now, {"price_usd": p})
    log.info("benchmark_btc_written", price_usd=p)
    return JobResult(rows_written=1, credits_used=0)


@register("benchmark")
async def benchmark_job(settings: GeckoSettings, ns) -> int:
    now = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        return await fetch_btc(store, settings, session, now)

    return await run_job("benchmark", settings, body)
