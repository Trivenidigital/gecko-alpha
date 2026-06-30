"""GeckoTerminal CA->pool resolution + pool OHLCV client.

C0 for the X-influencer performance-accrual build (design #392). Provides the
two foundational primitives the later forward-only snapshot writer (C2) will
consume:

* :func:`resolve_pool_address` — deterministic, source-tagged CA -> pool hop.
* :func:`fetch_pool_ohlcv` — pool OHLCV candles (ascending by timestamp).

Failure semantics (reviewer focus): a *provider failure* (HTTP 4xx/5xx after
bounded retries, malformed payload, transport error) raises
:class:`~scout.exceptions.PriceProviderError` and **never** returns a fabricated
price. A *missing pool / empty series* is a normal empty return (``None`` /
``[]``), not an error — callers map that to ``dead_pool_no_liquidity``, and a
raised error to ``price_provider_error``. Every value object is tagged
``source="gt"``; this module does not mix price sources.

This module is read-only against external APIs and writes nothing to the DB —
it does not touch ``source_calls`` or any performance field.
"""

import asyncio

import aiohttp
import structlog
from pydantic import BaseModel, ConfigDict

from scout.exceptions import PriceProviderError
from scout.ingestion.geckoterminal import (
    GECKO_BASE,
    _geckoterminal_network_for_chain,
)

logger = structlog.get_logger()

PRICE_SOURCE = "gt"
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


class PoolRef(BaseModel):
    """A resolved DEX pool for a token contract, source-tagged."""

    model_config = ConfigDict(frozen=True)

    network: str
    pool_address: str
    base_token_address: str | None = None
    reserve_usd: float | None = None
    source: str = PRICE_SOURCE


class OhlcvCandle(BaseModel):
    """One OHLCV candle from a pool, source-tagged. ``timestamp`` is unix seconds."""

    model_config = ConfigDict(frozen=True)

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume_usd: float | None = None
    source: str = PRICE_SOURCE


def _pool_address(pool: dict) -> str | None:
    attrs = pool.get("attributes") or {}
    addr = attrs.get("address")
    if addr:
        return str(addr)
    pid = pool.get("id")
    if isinstance(pid, str) and "_" in pid:
        # GT pool ids look like "<network>_<pool_address>".
        return pid.split("_", 1)[1] or None
    return None


def _reserve_usd(pool: dict) -> float:
    attrs = pool.get("attributes") or {}
    try:
        return float(attrs.get("reserve_in_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def _gt_get_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    chain: str,
    max_attempts: int = MAX_ATTEMPTS,
) -> list | dict:
    """GET GeckoTerminal JSON, **raising** PriceProviderError on provider failure.

    Bounded exponential backoff on 429 / 5xx and transport errors (respects the
    rate-limit budget); raises rather than returning ``None`` so a provider
    failure is observable and never masquerades as "no data".
    """
    for attempt in range(1, max_attempts + 1):
        try:
            async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 429 or resp.status >= 500:
                    if attempt < max_attempts:
                        wait = 2 ** (attempt - 1)
                        logger.warning(
                            "gt_ohlcv_retrying",
                            chain=chain,
                            url=url,
                            status=resp.status,
                            attempt=attempt,
                            wait=wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    raise PriceProviderError(
                        "geckoterminal", f"http_{resp.status}", url
                    )
                if resp.status != 200:
                    raise PriceProviderError(
                        "geckoterminal", f"http_{resp.status}", url
                    )
                try:
                    return await resp.json()
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    raise PriceProviderError(
                        "geckoterminal", "malformed_json", url
                    ) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt < max_attempts:
                wait = 2 ** (attempt - 1)
                logger.warning(
                    "gt_ohlcv_request_error",
                    chain=chain,
                    url=url,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    attempt=attempt,
                )
                await asyncio.sleep(wait)
                continue
            raise PriceProviderError("geckoterminal", type(exc).__name__, url) from exc
    raise PriceProviderError("geckoterminal", "retries_exhausted", url)


async def resolve_pool_address(
    session: aiohttp.ClientSession,
    *,
    chain: str,
    contract_address: str,
) -> PoolRef | None:
    """Resolve a token contract to its top DEX pool on GeckoTerminal.

    Deterministic: picks the highest ``reserve_in_usd`` pool, breaking ties by
    pool address, independent of the API's listing order. Returns ``None`` when
    the token has no pools (missing pool — not an error); raises
    :class:`PriceProviderError` on a provider failure or malformed payload.
    """
    network = _geckoterminal_network_for_chain(chain)
    url = f"{GECKO_BASE}/networks/{network}/tokens/{contract_address}/pools"
    data = await _gt_get_json(session, url, chain=chain)

    if not isinstance(data, dict):
        raise PriceProviderError("geckoterminal", "malformed_pools", url)
    pools = data.get("data")
    if not pools:
        return None
    if not isinstance(pools, list):
        raise PriceProviderError("geckoterminal", "malformed_pools", url)

    # Only pools with an extractable address can be priced. Filter first so a
    # malformed address-less entry can never shadow a valid pool — selection
    # stays deterministic and independent of API order/content.
    addressable = [p for p in pools if _pool_address(p)]
    if not addressable:
        raise PriceProviderError("geckoterminal", "malformed_pools", url)

    best = sorted(
        addressable,
        key=lambda p: (-_reserve_usd(p), _pool_address(p) or ""),
    )[0]
    pool_address = _pool_address(best)

    base_rel = (best.get("relationships") or {}).get("base_token") or {}
    base_token = (base_rel.get("data") or {}).get("id")
    return PoolRef(
        network=network,
        pool_address=pool_address,
        base_token_address=base_token,
        reserve_usd=_reserve_usd(best),
    )


async def fetch_pool_ohlcv(
    session: aiohttp.ClientSession,
    *,
    network: str,
    pool_address: str,
    timeframe: str = "minute",
    aggregate: int = 1,
    before_timestamp: int | None = None,
    limit: int = 100,
    chain: str | None = None,
) -> list[OhlcvCandle]:
    """Fetch pool OHLCV candles, returned **ascending** by timestamp.

    Returns ``[]`` for an empty series (dead/missing pool — not an error);
    raises :class:`PriceProviderError` on provider failure or malformed payload.
    GeckoTerminal returns candles newest-first; this normalises to oldest-first
    so the forward-return math (C2) can index by call-relative horizon.
    """
    chain = chain or network
    url = (
        f"{GECKO_BASE}/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}"
        f"?aggregate={aggregate}&limit={limit}"
    )
    if before_timestamp is not None:
        url += f"&before_timestamp={int(before_timestamp)}"

    data = await _gt_get_json(session, url, chain=chain)
    try:
        ohlcv_list = data["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError) as exc:
        raise PriceProviderError("geckoterminal", "malformed_ohlcv", url) from exc
    if ohlcv_list is None:
        raise PriceProviderError("geckoterminal", "malformed_ohlcv", url)
    if not ohlcv_list:
        return []

    candles: list[OhlcvCandle] = []
    for row in ohlcv_list:
        try:
            volume = row[5] if len(row) > 5 else None
            candles.append(
                OhlcvCandle(
                    timestamp=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume_usd=float(volume) if volume is not None else None,
                )
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise PriceProviderError("geckoterminal", "malformed_candle", url) from exc

    candles.sort(key=lambda c: c.timestamp)
    return candles
