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
from typing import Iterable

import aiohttp
import structlog

from scout.config import Settings
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


async def _get_json(
    session: aiohttp.ClientSession, url: str, params: dict | None = None
) -> dict | list | None:
    """Single GET with explicit error tolerance — caller decides retry semantics."""
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 429:
                log.warning("resolver_rate_limited", url=url)
                return None
            if resp.status >= 500:
                log.warning("resolver_5xx", url=url, status=resp.status)
                return None
            if resp.status != 200:
                # 404 is normal for unresolved CAs — log info, not warning
                log.info("resolver_non_200", url=url, status=resp.status)
                return None
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("resolver_network_error", url=url, error=str(e))
        return None


async def _resolve_ca_via_cg(
    session: aiohttp.ClientSession, ref: ContractRef
) -> ResolvedToken | None:
    """CG by-contract lookup. Returns enriched ResolvedToken or None on miss."""
    platform = _CG_PLATFORM.get(ref.chain)
    if platform is None:
        return None
    data = await _get_json(
        session, f"{CG_BASE}/coins/{platform}/contract/{ref.address}"
    )
    if not isinstance(data, dict) or not data.get("id"):
        return None
    market = data.get("market_data") or {}
    return ResolvedToken(
        token_id=data["id"],
        symbol=str(data.get("symbol") or "").upper(),
        chain=ref.chain,
        contract_address=ref.address,
        mcap=_safe_float(market.get("market_cap", {}).get("usd")),
        price_usd=_safe_float(market.get("current_price", {}).get("usd")),
        volume_24h_usd=_safe_float(market.get("total_volume", {}).get("usd")),
        age_days=None,
    )


async def _resolve_ca_via_dexscreener(
    session: aiohttp.ClientSession, ref: ContractRef
) -> ResolvedToken | None:
    """DexScreener fallback when CG misses (brand-new pools)."""
    data = await _get_json(session, f"{DEXSCREENER_BASE}/{ref.address}")
    if not isinstance(data, dict):
        return None
    pairs = data.get("pairs") or []
    if not pairs:
        return None
    # Pick the highest-liquidity pair as the canonical source for this token.
    best = max(pairs, key=lambda p: _safe_float(p.get("liquidity", {}).get("usd")) or 0)
    base = best.get("baseToken") or {}
    return ResolvedToken(
        token_id=f"dex:{ref.chain}:{ref.address}",  # synthetic id when CG misses
        symbol=str(base.get("symbol") or "").upper(),
        chain=ref.chain,
        contract_address=ref.address,
        mcap=_safe_float(best.get("fdv")) or _safe_float(best.get("marketCap")),
        price_usd=_safe_float(best.get("priceUsd")),
        volume_24h_usd=_safe_float((best.get("volume") or {}).get("h24")),
        age_days=None,
    )


async def _resolve_ticker_top3(
    session: aiohttp.ClientSession, ticker: str
) -> list[ResolvedToken]:
    """CG search by ticker, return top-3 by mcap. Used for cashtag-only posts.

    Cashtag-only resolution NEVER triggers a paper trade (gate 2 in
    dispatcher); we surface candidates so the operator can verify.
    """
    data = await _get_json(session, f"{CG_BASE}/search", params={"query": ticker})
    if not isinstance(data, dict):
        return []
    coins = (data.get("coins") or [])[:10]
    if not coins:
        return []
    # /search doesn't include mcap, so do a follow-up /coins/markets
    ids = ",".join(c["id"] for c in coins if c.get("id"))
    if not ids:
        return []
    market = await _get_json(
        session,
        f"{CG_BASE}/coins/markets",
        params={"vs_currency": "usd", "ids": ids, "per_page": "10"},
    )
    if not isinstance(market, list):
        return []
    market.sort(key=lambda m: m.get("market_cap") or 0, reverse=True)
    out: list[ResolvedToken] = []
    for m in market[:3]:
        out.append(
            ResolvedToken(
                token_id=m["id"],
                symbol=str(m.get("symbol") or "").upper(),
                chain=None,  # cross-chain ticker — chain unknown here
                contract_address=None,
                mcap=_safe_float(m.get("market_cap")),
                price_usd=_safe_float(m.get("current_price")),
                volume_24h_usd=_safe_float(m.get("total_volume")),
                age_days=None,
            )
        )
    return out


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


async def _check_safety(
    session: aiohttp.ClientSession, token: ResolvedToken
) -> ResolvedToken:
    """Mutate-and-return: stamp safety_pass + safety_check_completed on the token."""
    if not token.contract_address or not token.chain:
        # Cashtag-only resolutions can't be safety-checked; treat as completed=True
        # but pass=False so the dispatcher's gate 4 keeps it alert-only. Combined
        # with gate 2 (CA-required) this never reaches a trade either way.
        token.safety_check_completed = True
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
        for ref in contracts:
            tok = await _resolve_ca_via_cg(session, ref)
            if tok is None:
                tok = await _resolve_ca_via_dexscreener(session, ref)
            if tok is not None:
                tok = await _check_safety(session, tok)
                resolved.append(tok)
        if resolved:
            return ResolutionResult(state=ResolutionState.RESOLVED, tokens=resolved)
        # No contracts resolved — transient on first try, terminal on retry
        return ResolutionResult(
            state=(
                ResolutionState.UNRESOLVED_TERMINAL
                if is_retry
                else ResolutionState.UNRESOLVED_TRANSIENT
            ),
            tokens=[],
            error_text=f"no resolution for contracts: {[r.address for r in contracts]}",
        )

    if cashtags:
        # Cashtag-only path — surface top-3 candidates per ticker; first ticker only.
        # Multi-cashtag posts are rare; tally rule lives in dispatcher if needed.
        first = cashtags[0]
        candidates = await _resolve_ticker_top3(session, first)
        if candidates:
            for c in candidates:
                await _check_safety(session, c)
            return ResolutionResult(
                state=ResolutionState.RESOLVED,
                tokens=[],  # ticker-only never trade-eligible
                candidates_top3=candidates,
            )
        return ResolutionResult(
            state=(
                ResolutionState.UNRESOLVED_TERMINAL
                if is_retry
                else ResolutionState.UNRESOLVED_TRANSIENT
            ),
            tokens=[],
            error_text=f"no CG search match for cashtag '{first}'",
        )

    return ResolutionResult(state=ResolutionState.UNRESOLVED_TERMINAL, tokens=[])
