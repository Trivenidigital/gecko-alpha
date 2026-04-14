"""Tests for candidate aggregator."""

from scout.aggregator import aggregate


def test_aggregate_dedup_by_contract_address(token_factory):
    t1 = token_factory(contract_address="0xabc", volume_24h_usd=50000)
    t2 = token_factory(contract_address="0xabc", volume_24h_usd=99999)  # same addr, newer data
    t3 = token_factory(contract_address="0xdef", volume_24h_usd=30000)

    result = aggregate([t1, t2, t3])

    assert len(result) == 2
    by_addr = {t.contract_address: t for t in result}
    assert by_addr["0xabc"].volume_24h_usd == 99999  # last-write-wins
    assert by_addr["0xdef"].volume_24h_usd == 30000


def test_aggregate_empty_input():
    assert aggregate([]) == []


def test_aggregate_single_token(token_factory):
    t = token_factory()
    result = aggregate([t])
    assert len(result) == 1
    assert result[0].contract_address == "0xtest"


def test_aggregate_preserves_all_fields(token_factory):
    t = token_factory(contract_address="0xfull", quant_score=75, holder_count=500)
    result = aggregate([t])
    assert result[0].quant_score == 75
    assert result[0].holder_count == 500
