"""Resolve a Solana SPL mint for a paper trade, mirroring the Minara alert path.

Native Solana tokens carry the SPL mint directly as their coin_id (CG slugs are
never 32-44 base58 chars; EVM 0x… ids contain '0', not in the base58 alphabet).
"""

from __future__ import annotations

from scout.trading.minara_alert import _looks_like_spl_address


def resolve_solana_mint(
    *, coin_id: str, contract_address: str | None = None
) -> str | None:
    if contract_address and _looks_like_spl_address(contract_address):
        return contract_address
    if _looks_like_spl_address(coin_id):
        return coin_id
    return None
