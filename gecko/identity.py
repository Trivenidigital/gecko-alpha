from __future__ import annotations

import re

# Hex (EVM) addresses are case-insensitive; Solana base58 mints are case-sensitive.
EVM_CHAINS = frozenset({"base", "ethereum", "bsc"})


def norm_address(chain: str, addr: str) -> str:
    addr = addr.strip()
    return addr.lower() if chain in EVM_CHAINS else addr


def token_id(chain: str, addr: str) -> str:
    return f"{chain}:{norm_address(chain, addr)}"


_EVM_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_B58_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def is_valid_address(chain: str, addr: str) -> bool:
    """Well-formed contract address for ``chain``; checked before any URL or insert."""
    if not isinstance(addr, str):
        return False
    if chain in EVM_CHAINS:
        return _EVM_RE.fullmatch(addr) is not None
    if chain == "solana":
        return _B58_RE.fullmatch(addr) is not None
    return False
