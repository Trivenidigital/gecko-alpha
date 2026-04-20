"""Tests for BL-051 normalization helpers in dexscreener.py."""

import dataclasses

import pytest

from scout.ingestion.dexscreener import (
    BoostInfo,
    _normalize_chain_id,
    _normalize_address,
)


def test_boost_info_is_frozen():
    b = BoostInfo(chain="solana", address="ABC", total_amount=1500.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.chain = "base"  # frozen dataclass


def test_boost_info_fields():
    b = BoostInfo(chain="solana", address="ABC", total_amount=1500.0)
    assert b.chain == "solana"
    assert b.address == "ABC"
    assert b.total_amount == 1500.0


def test_normalize_chain_id_known():
    assert _normalize_chain_id("solana") == "solana"
    assert _normalize_chain_id("base") == "base"
    assert _normalize_chain_id("ethereum") == "ethereum"


def test_normalize_chain_id_unknown_passes_through_lower():
    assert _normalize_chain_id("SomeChain") == "somechain"


def test_normalize_address_evm_lowercases():
    assert _normalize_address("ethereum", "0xAbC123") == "0xabc123"
    assert _normalize_address("base", "0xDEADBEEF") == "0xdeadbeef"


def test_normalize_address_solana_preserves_case():
    solana_addr = "7GAGFk8aJMbNSRtCh8bB9x6eVpKZwxzMnB3UsNYukgmo"
    assert _normalize_address("solana", solana_addr) == solana_addr


def test_normalize_address_sui_preserves_case():
    sui_addr = "0xABCDef"
    assert _normalize_address("sui", sui_addr) == sui_addr
