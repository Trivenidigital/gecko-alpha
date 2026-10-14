"""GoPlus token security + holders (keyless REST), one address per call.

The free tier silently drops every address after the first in a batch, and
answers an unknown token with ``code: 1`` and an empty result; both are
handled here so "unknown" can never read as "safe".
"""

from __future__ import annotations

from gecko.identity import EVM_CHAINS, is_valid_address, norm_address
from gecko.numeric import as_int, num

GOPLUS_EVM_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
GOPLUS_SOL_URL = "https://api.gopluslabs.io/api/v1/solana/token_security"
CHAIN_IDS = {"ethereum": "1", "bsc": "56", "base": "8453"}
OK_CODES = (
    1,
    2,
)  # 2 = partial data: parsed, and any missing core flag reads as unknown
MAX_TAX = 0.10
RENOUNCED = {
    "",
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}
BURN = RENOUNCED | {"1nc1nerator11111111111111111111111111111111"}
# Solana AMM authorities that hold pool vaults (Raydium AMM v4, Raydium CPMM).
SOL_AMM_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",
}


class GoPlusError(Exception):
    def __init__(self, code):
        super().__init__(f"goplus code {code}")
        self.code = code


def pick_result(chain: str, address: str, body) -> dict | None:
    """This token's entry, or None when GoPlus does not know the token."""
    if not isinstance(body, dict) or body.get("code") not in OK_CODES:
        raise GoPlusError(body.get("code") if isinstance(body, dict) else None)
    res = body.get("result")
    if res in (None, {}, []):
        return None
    if not isinstance(res, dict):
        raise GoPlusError(body.get("code"))
    want = norm_address(chain, address)
    for k, v in res.items():
        if norm_address(chain, str(k)) == want and isinstance(v, dict):
            return v
    return None


def _flag(v) -> bool | None:
    """GoPlus "0"/"1" (str or int) -> bool; anything else -> None."""
    s = str(v) if v is not None else ""
    return {"0": False, "1": True}.get(s)


def _status(v) -> bool | None:
    return _flag(v.get("status")) if isinstance(v, dict) else None


def _evm_security(r: dict) -> tuple[list[str], bool]:
    """Core flags are honeypot/cannot_sell_all/blacklist; taxes are optional.

    A mintable or proxy contract needs ``owner_address`` to be judged: when the
    key is absent the result is unknown, never renounced.
    """
    owned = str(r.get("owner_address") or "").lower() not in RENOUNCED
    owner_needed = _flag(r.get("is_mintable")) or _flag(r.get("is_proxy"))
    owner_known = not owner_needed or "owner_address" in r
    checks = {
        "honeypot": _flag(r.get("is_honeypot")),
        "cannot_sell_all": _flag(r.get("cannot_sell_all")),
        "blacklist": _flag(r.get("is_blacklisted")),
    }
    flags = [k for k, v in checks.items() if v]
    for key in ("buy_tax", "sell_tax"):
        tax = num(r.get(key))
        if tax is not None and tax > MAX_TAX:
            flags.append(key)
    if owner_known and owned and _flag(r.get("is_mintable")):
        flags.append("mintable_owned")
    if owner_known and owned and _flag(r.get("is_proxy")):
        flags.append("proxy_owned")
    known = owner_known and all(v is not None for v in checks.values())
    return flags, known


def _sol_security(r: dict) -> tuple[list[str], bool]:
    checks = {
        "mintable": _status(r.get("mintable")),
        "freezable": _status(r.get("freezable")),
        "balance_mutable": _status(r.get("balance_mutable_authority")),
        "non_transferable": _flag(r.get("non_transferable")),
    }
    flags = [k for k, v in checks.items() if v]
    hook = r.get("transfer_hook")
    if isinstance(hook, list) and hook:
        flags.append("transfer_hook")
    return flags, all(v is not None for v in checks.values())


def _list(v) -> list:
    return v if isinstance(v, list) else []


def _top10(chain: str, r: dict) -> float | None:
    holders = r.get("holders")
    if not isinstance(holders, list) or not holders:
        return None
    skip = {
        norm_address(chain, str(d.get("pair") or d.get("id") or ""))
        for d in _list(r.get("dex"))
        if isinstance(d, dict)
    }
    skip |= {
        norm_address(chain, str(h.get("address") or ""))
        for h in _list(r.get("lp_holders"))
        if isinstance(h, dict)
    }
    skip |= BURN | SOL_AMM_AUTHORITIES
    total = 0.0
    for h in holders[:10]:
        if not isinstance(h, dict):
            continue
        addr = norm_address(chain, str(h.get("address") or h.get("account") or ""))
        if addr in skip or _flag(h.get("is_locked")):
            continue
        if chain in EVM_CHAINS and _flag(h.get("is_contract")):
            continue
        total += num(h.get("percent")) or 0.0
    return round(total * 100, 4)


def goplus_features(chain: str, r: dict) -> dict:
    if chain in EVM_CHAINS:
        flags, known = _evm_security(r)
        creator = str(r.get("creator_address") or "")
        deployer = (
            norm_address(chain, creator) if is_valid_address(chain, creator) else None
        )
    else:
        flags, known = _sol_security(r)
        deployer = None
        if _flag(r.get("trusted_token")):
            flags, known = [], True
    security_ok = False if flags else (True if known else None)
    return {
        "goplus_status": "ok",
        "security_ok": security_ok,
        "security_flags": sorted(flags),
        "holders": as_int(r.get("holder_count")),
        "top10_pct": _top10(chain, r),
        "deployer": deployer,
    }
