"""Tests for the DEX-vs-CG address classifier (I-instrumentation, observe-only).

Pure-function module (no aiohttp) so it runs in isolation even on the
OpenSSL-blocked Windows dev box.
"""

from scout.instrumentation.classify import classify_contract, is_dex


def test_classify_evm_dex_address():
    # 0x + 40 hex -> EVM DEX contract
    assert classify_contract("0xae3e205c3235c9c3a8a8d0fa72cd3cf5f7e9c8b1") == "evm"


def test_classify_solana_mint():
    # base58, 32-44 chars, no hyphen -> Solana DEX mint (the ANSEM pump.fun mint)
    assert classify_contract("9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump") == "solana"


def test_classify_coingecko_slug_hyphenated():
    assert classify_contract("the-black-bull") == "coingecko"


def test_classify_coingecko_slug_single_word():
    # single-word slugs (no hyphen) must still classify as coingecko, not unknown
    assert classify_contract("myro") == "coingecko"
    assert classify_contract("bitcoin") == "coingecko"


def test_classify_coingecko_wrapped_hyphen_not_base58():
    # hyphen excludes it from the base58 (solana) bucket
    assert classify_contract("wrapped-bitcoin") == "coingecko"


def test_is_dex_true_for_chain_contracts():
    assert is_dex("0xae3e205c3235c9c3a8a8d0fa72cd3cf5f7e9c8b1") is True
    assert is_dex("9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump") is True


def test_is_dex_false_for_cg_slug():
    assert is_dex("the-black-bull") is False
    assert is_dex("myro") is False
