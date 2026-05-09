"""BL-NEW-QUOTE-PAIR: parser-side tests for quote_symbol + dex_id extraction.

Covers `CandidateToken.from_dexscreener` parsing of the optional
`quoteToken.symbol` and top-level `dexId` fields. Pre-cutover rows
(CG / GT-sourced) leave both fields at default None.
"""

from __future__ import annotations

from datetime import datetime, timezone

from scout.models import CandidateToken


def _ds_pair_dict(**overrides) -> dict:
    """Minimum DexScreener pair dict; tests override per-case."""
    base = {
        "baseToken": {"address": "0xabc", "name": "Foo", "symbol": "FOO"},
        "chainId": "solana",
        "pairCreatedAt": int((datetime.now(timezone.utc).timestamp() - 86400) * 1000),
        "fdv": 100_000,
        "liquidity": {"usd": 75_000},
        "volume": {"h24": 50_000},
    }
    base.update(overrides)
    return base


def test_from_dexscreener_extracts_quote_symbol_and_dex_id():
    raw = _ds_pair_dict(
        quoteToken={"address": "0xqt", "name": "USD Coin", "symbol": "USDC"},
        dexId="raydium",
    )
    token = CandidateToken.from_dexscreener(raw)
    assert token.quote_symbol == "USDC"
    assert token.dex_id == "raydium"


def test_from_dexscreener_handles_missing_quote_token():
    raw = _ds_pair_dict()  # no quoteToken / dexId keys
    token = CandidateToken.from_dexscreener(raw)
    assert token.quote_symbol is None
    assert token.dex_id is None


def test_from_dexscreener_handles_null_quote_token():
    """R2 NIT: explicit null quoteToken (vs absent) must not raise AttributeError."""
    raw = _ds_pair_dict(quoteToken=None, dexId=None)
    token = CandidateToken.from_dexscreener(raw)
    assert token.quote_symbol is None
    assert token.dex_id is None


def test_from_dexscreener_handles_empty_quote_token():
    """Empty dict quoteToken (no symbol field) — graceful degradation."""
    raw = _ds_pair_dict(quoteToken={}, dexId="uniswap")
    token = CandidateToken.from_dexscreener(raw)
    assert token.quote_symbol is None
    assert token.dex_id == "uniswap"


def test_candidate_token_default_none_for_quote_fields():
    """Direct constructor (no DexScreener parser) defaults to None."""
    token = CandidateToken(
        contract_address="0xabc",
        chain="solana",
        token_name="Foo",
        ticker="FOO",
    )
    assert token.quote_symbol is None
    assert token.dex_id is None


def test_from_coingecko_leaves_quote_fields_none():
    """CG-sourced tokens have no DEX pair info — both fields stay None."""
    raw = {
        "id": "foo-token",
        "name": "Foo Token",
        "symbol": "foo",
        "market_cap": 1_000_000,
        "total_volume": 100_000,
    }
    token = CandidateToken.from_coingecko(raw)
    assert token.quote_symbol is None
    assert token.dex_id is None
