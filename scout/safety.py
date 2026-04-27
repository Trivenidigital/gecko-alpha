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
    """Check if a token is safe via GoPlus Security API.

    Returns True if:
    - honeypot = 0
    - is_blacklisted = 0
    - buy_tax < 10%
    - sell_tax < 10%

    On API failure: log warning, return True (fail open — don't block alerts).
    """
    if chain == "coingecko":
        return True  # CG tokens don't need GoPlus safety check

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
                    "GoPlus API returned error",
                    status=resp.status,
                    contract_address=contract_address,
                )
                return True
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(
            "GoPlus API error", contract_address=contract_address, error=str(e)
        )
        return True

    results = data.get("result") or {}
    result = results.get(contract_address.lower(), {})
    if not result:
        # Also check without lowercasing for Solana addresses
        result = results.get(contract_address, {})
    if not result:
        logger.warning("GoPlus: no result", contract_address=contract_address)
        return True

    if result.get("is_honeypot") == "1":
        return False
    if result.get("is_blacklisted") == "1":
        return False
    if float(result.get("buy_tax", "0") or "0") >= 0.10:
        return False
    if float(result.get("sell_tax", "0") or "0") >= 0.10:
        return False

    return True


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
