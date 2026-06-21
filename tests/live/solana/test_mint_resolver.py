from __future__ import annotations

from scout.live.solana.mint_resolver import resolve_solana_mint

MINT = "So11111111111111111111111111111111111111112"


def test_native_coin_id_is_the_mint():
    assert resolve_solana_mint(coin_id=MINT) == MINT


def test_explicit_contract_address_wins():
    assert resolve_solana_mint(coin_id="some-cg-slug", contract_address=MINT) == MINT


def test_non_solana_slug_returns_none():
    assert resolve_solana_mint(coin_id="bitcoin") is None
    assert resolve_solana_mint(coin_id="0xabc123") is None  # EVM-shaped, has '0'
