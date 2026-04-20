"""Tests for BL-051 boost fields on CandidateToken."""

from scout.models import CandidateToken


def test_candidate_token_boost_fields_default_to_none():
    t = CandidateToken(
        contract_address="0xabc",
        chain="solana",
        token_name="T",
        ticker="T",
    )
    assert t.boost_total_amount is None
    assert t.boost_rank is None


def test_candidate_token_boost_fields_accept_values():
    t = CandidateToken(
        contract_address="0xabc",
        chain="solana",
        token_name="T",
        ticker="T",
        boost_total_amount=1500.0,
        boost_rank=1,
    )
    assert t.boost_total_amount == 1500.0
    assert t.boost_rank == 1
