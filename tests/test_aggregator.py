"""Tests for candidate aggregator."""

from scout.aggregator import aggregate
from scout.models import CandidateToken


def _make_token(**overrides) -> CandidateToken:
    defaults = dict(
        contract_address="0xtest", chain="solana", token_name="Test",
        ticker="TST", token_age_days=1.0, market_cap_usd=50000.0,
        liquidity_usd=10000.0, volume_24h_usd=80000.0,
        holder_count=100, holder_growth_1h=20,
    )
    defaults.update(overrides)
    return CandidateToken(**defaults)


def test_aggregate_dedup_by_contract_address():
    t1 = _make_token(contract_address="0xabc", volume_24h_usd=50000)
    t2 = _make_token(contract_address="0xabc", volume_24h_usd=99999)  # same addr, newer data
    t3 = _make_token(contract_address="0xdef", volume_24h_usd=30000)

    result = aggregate([t1, t2, t3])

    assert len(result) == 2
    by_addr = {t.contract_address: t for t in result}
    assert by_addr["0xabc"].volume_24h_usd == 99999  # last-write-wins
    assert by_addr["0xdef"].volume_24h_usd == 30000


def test_aggregate_empty_input():
    assert aggregate([]) == []


def test_aggregate_single_token():
    t = _make_token()
    result = aggregate([t])
    assert len(result) == 1
    assert result[0].contract_address == "0xtest"


def test_aggregate_preserves_all_fields():
    t = _make_token(contract_address="0xfull", quant_score=75, holder_count=500)
    result = aggregate([t])
    assert result[0].quant_score == 75
    assert result[0].holder_count == 500
