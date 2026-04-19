"""Holder data enrichment via Helius (Solana) and Moralis (EVM)."""

import structlog

import aiohttp

from scout.config import Settings
from scout.models import CandidateToken

logger = structlog.get_logger()

# Chain mappings for Moralis
MORALIS_CHAIN_MAP = {
    "ethereum": "eth",
    "base": "base",
    "polygon": "polygon",
}


async def enrich_holders(
    token: CandidateToken,
    session: aiohttp.ClientSession,
    settings: Settings,
) -> CandidateToken:
    """Enrich a token with holder count data.

    - Solana -> Helius DAS API
    - EVM chains -> Moralis ERC20 owners
    - Missing API key -> return unenriched (graceful degradation)
    - API failure -> log warning, return unenriched
    """
    if token.chain == "solana":
        if not settings.HELIUS_API_KEY:
            return token
        return await _enrich_solana(token, session, settings)
    elif token.chain in MORALIS_CHAIN_MAP:
        if not settings.MORALIS_API_KEY:
            return token
        return await _enrich_evm(token, session, settings)
    return token


async def _enrich_solana(
    token: CandidateToken,
    session: aiohttp.ClientSession,
    settings: Settings,
) -> CandidateToken:
    """Fetch holder count from Helius DAS API (getTokenAccounts)."""
    url = f"https://mainnet.helius-rpc.com/?api-key={settings.HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id": "holder-enrichment",
        "method": "getTokenAccounts",
        "params": {"mint": token.contract_address, "limit": 1},
    }
    try:
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
            total = data.get("result", {}).get("total", 0)
            return token.model_copy(update={"holder_count": total})
    except Exception:
        logger.warning(
            "Helius holder lookup failed",
            contract_address=token.contract_address,
            exc_info=True,
        )
        return token


async def _enrich_evm(
    token: CandidateToken,
    session: aiohttp.ClientSession,
    settings: Settings,
) -> CandidateToken:
    """Fetch holder count from Moralis ERC20 owners endpoint."""
    chain = MORALIS_CHAIN_MAP[token.chain]
    url = (
        f"https://deep-index.moralis.io/api/v2.2/erc20/"
        f"{token.contract_address}/owners?chain={chain}"
    )
    headers = {"X-API-Key": settings.MORALIS_API_KEY}
    try:
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            holders = data.get("result", [])
            return token.model_copy(update={"holder_count": len(holders)})
    except Exception:
        logger.warning(
            "Moralis holder lookup failed",
            contract_address=token.contract_address,
            exc_info=True,
        )
        return token
