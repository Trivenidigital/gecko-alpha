"""Pin the gt_trending_rank CandidateToken field (BL-052)."""

from scout.models import CandidateToken


def test_gt_trending_rank_defaults_to_none():
    token = CandidateToken(
        contract_address="0xabc",
        chain="base",
        token_name="Test",
        ticker="TST",
    )
    assert token.gt_trending_rank is None


def test_gt_trending_rank_accepts_int():
    token = CandidateToken(
        contract_address="0xabc",
        chain="base",
        token_name="Test",
        ticker="TST",
        gt_trending_rank=3,
    )
    assert token.gt_trending_rank == 3
