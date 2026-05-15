"""BL-064 resolver — CA/ticker → ResolvedToken with mcap/price/vol/safety.

Combines resolution + enrichment + safety check in one module per the
v2 design (collapsed enricher.py into resolver.py per architecture
reviewer SMELL #1). Returns a ResolutionResult with state ∈
{RESOLVED, UNRESOLVED_TRANSIENT, UNRESOLVED_TERMINAL}.

Brand-new tokens often miss CG indexing for ~60s. The state machine
distinguishes a transient miss (retry once after the configured delay)
from a terminal miss (alert-only forever).
"""

from __future__ import annotations

import asyncio
import math
from typing import Iterable

import aiohttp
import structlog

from scout.config import Settings
from scout.ratelimit import coingecko_limiter
from scout.safety import is_safe_strict
from scout.social.telegram.models import (
    ContractRef,
    ResolutionResult,
    ResolutionState,
    ResolvedToken,
)

log = structlog.get_logger()

CG_BASE = "https://api.coingecko.com/api/v3"
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens"

# CG asset_platform_id values keyed by our chain tag.
_CG_PLATFORM = {
    "ethereum": "ethereum",
    "base": "base",
    "polygon": "polygon-pos",
    "arbitrum": "arbitrum-one",
    "solana": "solana",
}


class _Outcome:
    """Discriminated resolver-fetch outcome.

    NOT_FOUND     404 — the entity doesn't exist; retry is wasted.
    TRANSIENT     429/5xx/timeout/network — retry might help.
    AUTH_ERROR    401/403 — operator action required (revoked key / IP block).
    OK            2xx with parsed JSON body.
    """

    OK = "OK"
    NOT_FOUND = "NOT_FOUND"
    TRANSIENT = "TRANSIENT"
    AUTH_ERROR = "AUTH_ERROR"


async def _get_json(
    session: aiohttp.ClientSession, url: str, params: dict | None = None
) -> tuple[str, dict | list | None]:
    """Single GET returning (outcome, body). Caller decides retry semantics
    based on the outcome rather than guessing from None."""
    try:
        is_coingecko = url.startswith(CG_BASE)
        if is_coingecko:
            await coingecko_limiter.acquire()
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                return (_Outcome.OK, await resp.json())
            if resp.status == 404:
                # Normal for unresolved CAs / unknown tickers
                log.info("resolver_not_found", url=url)
                return (_Outcome.NOT_FOUND, None)
            if resp.status in (401, 403):
                log.warning("resolver_auth_error", url=url, status=resp.status)
                return (_Outcome.AUTH_ERROR, None)
            if resp.status == 429 or resp.status >= 500:
                if resp.status == 429 and is_coingecko:
                    await coingecko_limiter.report_429()
                log.warning("resolver_transient", url=url, status=resp.status)
                return (_Outcome.TRANSIENT, None)
            log.warning("resolver_unexpected_status", url=url, status=resp.status)
            return (_Outcome.TRANSIENT, None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("resolver_network_error", url=url, error=str(e))
        return (_Outcome.TRANSIENT, None)


async def _resolve_ca_via_cg(
    session: aiohttp.ClientSession, ref: ContractRef
) -> tuple[str, ResolvedToken | None]:
    """CG by-contract lookup. Returns (outcome, token-or-None)."""
    platform = _CG_PLATFORM.get(ref.chain)
    if platform is None:
        return (_Outcome.NOT_FOUND, None)
    outcome, data = await _get_json(
        session, f"{CG_BASE}/coins/{platform}/contract/{ref.address}"
    )
    if outcome != _Outcome.OK or not isinstance(data, dict) or not data.get("id"):
        return (outcome, None)
    market = data.get("market_data") or {}
    return (
        _Outcome.OK,
        ResolvedToken(
            token_id=data["id"],
            symbol=str(data.get("symbol") or "").upper(),
            chain=ref.chain,
            contract_address=ref.address,
            mcap=_safe_float(market.get("market_cap", {}).get("usd")),
            price_usd=_safe_float(market.get("current_price", {}).get("usd")),
            volume_24h_usd=_safe_float(market.get("total_volume", {}).get("usd")),
            age_days=None,
        ),
    )


async def _resolve_ca_via_dexscreener(
    session: aiohttp.ClientSession, ref: ContractRef
) -> tuple[str, ResolvedToken | None]:
    """DexScreener fallback when CG misses (brand-new pools). Returns (outcome, token).

    DexScreener returns the canonical chainId per pair. We RE-ATTRIBUTE the
    token's chain from this field rather than trusting the parser's default
    "ethereum" for any 0x+40-hex string — closes round-2-review Medium #2
    (Optimism/BSC/Avalanche CAs were silently going to is_safe_strict with
    chain="ethereum", returning wrong verdicts).
    """
    outcome, data = await _get_json(session, f"{DEXSCREENER_BASE}/{ref.address}")
    if outcome != _Outcome.OK or not isinstance(data, dict):
        return (outcome, None)
    pairs = data.get("pairs") or []
    if not pairs:
        return (_Outcome.NOT_FOUND, None)
    best = max(pairs, key=lambda p: _safe_float(p.get("liquidity", {}).get("usd")) or 0)
    base = best.get("baseToken") or {}
    # Re-attribute chain from DexScreener's chainId. Defaults to parser's
    # original tag if the field is missing.
    canonical_chain = best.get("chainId") or ref.chain
    return (
        _Outcome.OK,
        ResolvedToken(
            token_id=f"dex:{canonical_chain}:{ref.address}",
            symbol=str(base.get("symbol") or "").upper(),
            chain=canonical_chain,
            contract_address=ref.address,
            mcap=_safe_float(best.get("fdv")) or _safe_float(best.get("marketCap")),
            price_usd=_safe_float(best.get("priceUsd")),
            volume_24h_usd=_safe_float((best.get("volume") or {}).get("h24")),
            age_days=None,
        ),
    )


async def _resolve_ticker_top3(
    session: aiohttp.ClientSession, ticker: str
) -> tuple[str, list[ResolvedToken]]:
    """CG search by ticker, return (outcome, top-3 by mcap). Cashtag-only
    resolution NEVER triggers a paper trade (gate 2 in dispatcher)."""
    outcome, data = await _get_json(
        session, f"{CG_BASE}/search", params={"query": ticker}
    )
    if outcome != _Outcome.OK or not isinstance(data, dict):
        return (outcome, [])
    coins = (data.get("coins") or [])[:10]
    if not coins:
        return (_Outcome.NOT_FOUND, [])
    ids = ",".join(c["id"] for c in coins if c.get("id"))
    if not ids:
        return (_Outcome.NOT_FOUND, [])
    outcome2, market = await _get_json(
        session,
        f"{CG_BASE}/coins/markets",
        params={"vs_currency": "usd", "ids": ids, "per_page": "10"},
    )
    if outcome2 != _Outcome.OK or not isinstance(market, list):
        return (outcome2, [])
    market.sort(key=lambda m: m.get("market_cap") or 0, reverse=True)
    out: list[ResolvedToken] = []
    for m in market[:3]:
        out.append(
            ResolvedToken(
                token_id=m["id"],
                symbol=str(m.get("symbol") or "").upper(),
                chain=None,
                contract_address=None,
                mcap=_safe_float(m.get("market_cap")),
                price_usd=_safe_float(m.get("current_price")),
                volume_24h_usd=_safe_float(m.get("total_volume")),
                age_days=None,
            )
        )
    return (_Outcome.OK, out)


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


async def _check_safety(
    session: aiohttp.ClientSession, token: ResolvedToken
) -> ResolvedToken:
    """Mutate-and-return: stamp safety_skipped_no_ca / safety_pass / completed.

    Cashtag-only resolutions (no CA) get safety_skipped_no_ca=True, which the
    alerter renders as a distinct badge ("⊘ no CA — check skipped") instead
    of the misleading "❌ FAILED safety check". Dispatcher gate 2 (no_ca)
    rejects them before gate 4 (safety) is consulted, so behaviour is
    unchanged; only the alert text becomes truthful.
    """
    if not token.contract_address or not token.chain:
        token.safety_skipped_no_ca = True
        token.safety_check_completed = False
        token.safety_pass = False
        return token
    is_safe, completed = await is_safe_strict(
        token.contract_address, token.chain, session
    )
    token.safety_pass = is_safe
    token.safety_check_completed = completed
    return token


async def resolve_and_enrich(
    parsed_contracts: Iterable[ContractRef],
    parsed_cashtags: Iterable[str],
    *,
    session: aiohttp.ClientSession,
    settings: Settings,
    is_retry: bool = False,
) -> ResolutionResult:
    """Top-level entry: parsed extracts → ResolutionResult.

    Resolution priority:
      1. Each contract: CG by-contract → DexScreener fallback
      2. If no contracts AND any cashtags: CG search top-3 (cashtag-only path)

    Safety check runs for any RESOLVED token with a CA. Cashtag-only candidates
    never get safety-checked (no CA to query) — they're alert-only by design.

    `is_retry` is set True by the listener on the second resolution attempt
    after a TRANSIENT miss; it short-circuits any further retry signaling.
    """
    contracts = list(parsed_contracts)
    cashtags = list(parsed_cashtags)

    if contracts:
        resolved: list[ResolvedToken] = []
        any_transient = False
        for ref in contracts:
            outcome, tok = await _resolve_ca_via_cg(session, ref)
            if outcome == _Outcome.TRANSIENT:
                any_transient = True
            if tok is None:
                outcome2, tok = await _resolve_ca_via_dexscreener(session, ref)
                if outcome2 == _Outcome.TRANSIENT:
                    any_transient = True
            if tok is not None:
                tok = await _check_safety(session, tok)
                resolved.append(tok)
        if resolved:
            return ResolutionResult(state=ResolutionState.RESOLVED, tokens=resolved)
        # No contracts resolved — only retry on transient (404 won't change next minute)
        if any_transient and not is_retry:
            return ResolutionResult(
                state=ResolutionState.UNRESOLVED_TRANSIENT,
                tokens=[],
                error_text=f"transient miss on contracts: {[r.address for r in contracts]}",
            )
        return ResolutionResult(
            state=ResolutionState.UNRESOLVED_TERMINAL,
            tokens=[],
            error_text=f"no resolution for contracts: {[r.address for r in contracts]}",
        )

    if cashtags:
        first = cashtags[0]
        outcome, candidates = await _resolve_ticker_top3(session, first)
        if candidates:
            for c in candidates:
                await _check_safety(session, c)
            return ResolutionResult(
                state=ResolutionState.RESOLVED,
                tokens=[],
                candidates_top3=candidates,
            )
        if outcome == _Outcome.TRANSIENT and not is_retry:
            return ResolutionResult(
                state=ResolutionState.UNRESOLVED_TRANSIENT,
                tokens=[],
                error_text=f"transient miss on cashtag '{first}'",
            )
        return ResolutionResult(
            state=ResolutionState.UNRESOLVED_TERMINAL,
            tokens=[],
            error_text=f"no CG search match for cashtag '{first}'",
        )

    return ResolutionResult(state=ResolutionState.UNRESOLVED_TERMINAL, tokens=[])
