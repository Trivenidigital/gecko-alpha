from __future__ import annotations

# Hex (EVM) addresses are case-insensitive; Solana base58 mints are case-sensitive.
EVM_CHAINS = frozenset({"base", "ethereum", "bsc"})


def norm_address(chain: str, addr: str) -> str:
    addr = addr.strip()
    return addr.lower() if chain in EVM_CHAINS else addr


def token_id(chain: str, addr: str) -> str:
    return f"{chain}:{norm_address(chain, addr)}"
