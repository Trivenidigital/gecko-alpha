"""Test gt_trending_rank preservation through aggregate() (BL-052)."""

from scout.aggregator import aggregate
from scout.models import CandidateToken


def _tok(addr, rank=None, source_chain="solana"):
    return CandidateToken(
        contract_address=addr,
        chain=source_chain,
        token_name="Test",
        ticker="TST",
        gt_trending_rank=rank,
    )


def test_gt_trending_rank_preserved_when_gt_arrives_first():
    # GT first (rank=3), then DexScreener (rank=None)
    result = aggregate([_tok("0xabc", rank=3), _tok("0xabc", rank=None)])
    assert len(result) == 1
    assert result[0].gt_trending_rank == 3


def test_gt_trending_rank_preserved_when_dex_arrives_first():
    # DexScreener first (rank=None), then GT (rank=3)
    result = aggregate([_tok("0xabc", rank=None), _tok("0xabc", rank=3)])
    assert len(result) == 1
    assert result[0].gt_trending_rank == 3


def test_gt_trending_rank_unchanged_for_non_duplicate():
    result = aggregate([_tok("0xabc", rank=5)])
    assert len(result) == 1
    assert result[0].gt_trending_rank == 5


def test_gt_trending_rank_none_stays_none_for_non_duplicate():
    result = aggregate([_tok("0xabc", rank=None)])
    assert len(result) == 1
    assert result[0].gt_trending_rank is None
