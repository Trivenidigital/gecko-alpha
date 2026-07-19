"""Single source of truth for the CoinGecko API tier switch.

Paid CG plans (Basic/Analyst/Lite — "Pro API") authenticate against a
DIFFERENT host and auth name than the free Demo tier:

    demo:  https://api.coingecko.com/api/v3      x-cg-demo-api-key / x_cg_demo_api_key
    pro:   https://pro-api.coingecko.com/api/v3  x-cg-pro-api-key  / x_cg_pro_api_key

Mixing them yields CG errors 10010 (Pro key on Demo URL) / 10011 (Demo key on
Pro URL). Both key formats start with "CG-", so the tier can never be inferred
from the key — it is an explicit setting (Settings.COINGECKO_API_TIER),
threaded to every call site through these helpers. No call site may hardcode
a CG base URL or auth name.
"""

DEMO_BASE = "https://api.coingecko.com/api/v3"
PRO_BASE = "https://pro-api.coingecko.com/api/v3"

VALID_TIERS = ("demo", "pro")

_BASES = {"demo": DEMO_BASE, "pro": PRO_BASE}
_QUERY_KEYS = {"demo": "x_cg_demo_api_key", "pro": "x_cg_pro_api_key"}
_HEADER_KEYS = {"demo": "x-cg-demo-api-key", "pro": "x-cg-pro-api-key"}


def _check(tier: str) -> str:
    if tier not in _BASES:
        raise ValueError(
            f"COINGECKO_API_TIER must be one of {VALID_TIERS}, got {tier!r}"
        )
    return tier


def base_url(tier: str) -> str:
    """API base URL for the tier."""
    return _BASES[_check(tier)]


def auth_query(api_key: str, tier: str) -> dict[str, str]:
    """Query-param auth mapping; empty when no key is configured."""
    _check(tier)
    if not api_key:
        return {}
    return {_QUERY_KEYS[tier]: api_key}


def auth_headers(api_key: str, tier: str) -> dict[str, str]:
    """Header auth mapping; empty when no key is configured."""
    _check(tier)
    if not api_key:
        return {}
    return {_HEADER_KEYS[tier]: api_key}
