"""GoPlus Security API token safety check."""

import asyncio

import aiohttp
import structlog

logger = structlog.get_logger()

# GoPlus uses chain IDs: 1 = ethereum, 56 = bsc, etc.
# For named chains, they also accept the name directly.
CHAIN_ID_MAP = {
    "ethereum": "1",
    "base": "8453",
    "polygon": "137",
    "solana": "solana",
}

GOPLUS_BASE = "https://api.gopluslabs.io/api/v1/token_security"


async def is_safe(
    contract_address: str, chain: str, session: aiohttp.ClientSession
) -> bool:
    """Legacy FAIL-OPEN safety check — returns True on transport errors,
    5xx, timeouts, missing record. Existing pre-BL-064 callers expect this
    contract (alerts shouldn't block on GoPlus outages).

    Reduced to a thin wrapper around `is_safe_strict` per round-2 Nit #12 —
    the prior duplicated logic was a drift hazard. Callers that need
    fail-CLOSED semantics (BL-064 dispatcher) should use `is_safe_strict`
    directly.
    """
    is_safe_verdict, completed = await is_safe_strict(contract_address, chain, session)
    if not completed:
        # Fail-open: 5xx / timeout / missing record → True.
        return True
    return is_safe_verdict


async def is_safe_strict(
    contract_address: str, chain: str, session: aiohttp.ClientSession
) -> tuple[bool, bool]:
    """BL-064 safety check with FAIL-CLOSED discriminator.

    Returns (is_safe, check_completed):
      check_completed=True   GoPlus returned a usable verdict (pass or fail).
      check_completed=False  GoPlus 5xx / timeout / missing record. Caller
                             should treat as "safety unknown" and refuse to
                             paper-trade. Closes the BL-063 fail-open.

    For chain='coingecko' we return (True, True) — CG-native tokens don't
    need GoPlus and the check is trivially complete.
    """
    if chain == "coingecko":
        return (True, True)

    chain_id = CHAIN_ID_MAP.get(chain, chain)
    url = f"{GOPLUS_BASE}/{chain_id}"

    try:
        async with session.get(
            url,
            params={"contract_addresses": contract_address},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning(
                    "goplus_api_returned_error_strict",
                    status=resp.status,
                    contract_address=contract_address,
                )
                return (False, False)
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(
            "goplus_api_error_strict",
            contract_address=contract_address,
            error=str(e),
        )
        return (False, False)

    results = data.get("result") or {}
    result = results.get(contract_address.lower(), {})
    if not result:
        result = results.get(contract_address, {})
    if not result:
        logger.warning("goplus_no_result_strict", contract_address=contract_address)
        return (False, False)

    if result.get("is_honeypot") == "1":
        return (False, True)
    if result.get("is_blacklisted") == "1":
        return (False, True)
    if float(result.get("buy_tax", "0") or "0") >= 0.10:
        return (False, True)
    if float(result.get("sell_tax", "0") or "0") >= 0.10:
        return (False, True)

    return (True, True)
