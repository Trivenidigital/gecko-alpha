"""BL-071a' v3: DexScreener FDV fetcher for chain_match outcome hydration.

Used by `scout/chains/tracker.py` at two points:
1. `_record_completion` (write time) — captures `mcap_at_completion`.
2. `update_chain_outcomes` (hydration time) — fetches current FDV to
   compute pct change vs the captured completion FDV.

Uses the chain-agnostic `/latest/dex/tokens/{contract}` endpoint so the
caller does NOT need to know the chain. Returns the FDV of the first
pair (DexScreener orders by liquidity desc by default).

Returns FetchResult(fdv, status) — the status enum lets the hydrator
distinguish 429 (rate-limited, don't punish session-health) from other
errors (transient, malformed, etc.). Without this distinction, routine
DS rate-limiting would trigger the chain_tracker_session_unhealthy
ERROR with misleading 'restart service' guidance (per design-review
R1-M1 + R2-2).

Fail-soft: never raises. Callers are responsible for graceful
degradation based on (fdv is None, status).

Logging convention (per design-review R2-6): `chain_outcome_*` for
per-row events; `chain_outcomes_*` (plural) for aggregate events.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TYPE_CHECKING, Any, NamedTuple

import structlog

if TYPE_CHECKING:
    import aiohttp

logger = structlog.get_logger()

DS_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{contract}"
# Module-level constant resolved at first call (avoids importing aiohttp at
# module-load time, which triggers an OpenSSL DLL conflict on Windows dev
# envs and prevents test collection on otherwise-pure-Python tests like
# the NamedTuple unpacking checks).
_REQUEST_TIMEOUT_TOTAL_S = 15
_REQUEST_TIMEOUT_CONNECT_S = 5


class FetchStatus(str, Enum):
    """Outcome classification for fetch_token_fdv result."""

    OK = "ok"  # fdv is non-None and positive
    NO_DATA = "no_data"  # 200 + empty pairs / missing fdv field / fdv<=0
    NOT_FOUND = "not_found"  # 404 (contract may be delisted)
    RATE_LIMITED = "rate_limited"  # 429 (DS free-tier throttle)
    TRANSIENT = "transient"  # timeout / connection error / non-200/404/429
    MALFORMED = "malformed"  # JSON decode failure / unexpected shape


class FetchResult(NamedTuple):
    """Result of fetch_token_fdv. fdv is None for any non-OK status."""

    fdv: float | None
    status: FetchStatus


# McapFetcher is the injected-dependency type for tests. Single Callable
# alias is lighter than a Protocol (per design-review R2-1). Uses Any for
# the session type because aiohttp is lazy-imported inside the function
# body (Windows OpenSSL DLL workaround); the runtime contract is still
# aiohttp.ClientSession.
McapFetcher = Callable[[Any, str], Awaitable[FetchResult]]


async def fetch_token_fdv(
    session: Any,  # aiohttp.ClientSession; lazy-imported below
    contract: str,
) -> FetchResult:
    """Fetch current FDV for a token contract from DexScreener.

    Returns FetchResult(fdv, status). fdv is non-None ONLY when status==OK.
    Never raises.
    """
    # Lazy import: see module docstring on Windows OpenSSL workaround.
    import aiohttp

    url = DS_TOKEN_URL.format(contract=contract)
    timeout = aiohttp.ClientTimeout(
        total=_REQUEST_TIMEOUT_TOTAL_S, connect=_REQUEST_TIMEOUT_CONNECT_S
    )
    try:
        async with session.get(url, timeout=timeout) as resp:
            status = resp.status
            if status == 404:
                return FetchResult(None, FetchStatus.NOT_FOUND)
            if status == 429:
                return FetchResult(None, FetchStatus.RATE_LIMITED)
            if status != 200:
                logger.debug("ds_fetch_non_200", contract=contract, status=status)
                return FetchResult(None, FetchStatus.TRANSIENT)
            try:
                data = await resp.json()
            except (aiohttp.ContentTypeError, ValueError) as exc:
                # ValueError covers json.JSONDecodeError (per R1-S1)
                logger.debug(
                    "ds_fetch_malformed",
                    contract=contract,
                    error_type=type(exc).__name__,
                )
                return FetchResult(None, FetchStatus.MALFORMED)
    except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
        logger.debug(
            "ds_fetch_error",
            contract=contract,
            error_type=type(exc).__name__,
        )
        return FetchResult(None, FetchStatus.TRANSIENT)

    pairs = data.get("pairs") if isinstance(data, dict) else None
    if not pairs or not isinstance(pairs, list):
        return FetchResult(None, FetchStatus.NO_DATA)

    fdv_raw = pairs[0].get("fdv") if isinstance(pairs[0], dict) else None
    if fdv_raw is None:
        return FetchResult(None, FetchStatus.NO_DATA)
    try:
        fdv = float(fdv_raw)
    except (TypeError, ValueError):
        return FetchResult(None, FetchStatus.NO_DATA)
    if fdv <= 0:
        return FetchResult(None, FetchStatus.NO_DATA)
    return FetchResult(fdv, FetchStatus.OK)
