"""Tests for candidate aggregator."""

from scout.aggregator import aggregate


def test_aggregate_dedup_by_contract_address(token_factory):
    t1 = token_factory(contract_address="0xabc", volume_24h_usd=50000)
    t2 = token_factory(
        contract_address="0xabc", volume_24h_usd=99999
    )  # same addr, newer data
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


def test_aggregate_preserves_quote_symbol_from_dexscreener(token_factory):
    """R7 PR review MUST-FIX: BL-NEW-QUOTE-PAIR `quote_symbol` and `dex_id`
    are populated only by DexScreener. A later CG/GT entry with None must
    NOT null-out the DexScreener value."""
    ds_first = token_factory(
        contract_address="0xshared",
        quote_symbol="USDC",
        dex_id="raydium",
    )
    cg_second = token_factory(
        contract_address="0xshared",
        quote_symbol=None,
        dex_id=None,
    )
    result = aggregate([ds_first, cg_second])
    assert len(result) == 1
    # last-write-wins for most fields, but _PRESERVE_FIELDS keeps these.
    assert result[0].quote_symbol == "USDC"
    assert result[0].dex_id == "raydium"


def test_aggregate_lets_dexscreener_overwrite_none(token_factory):
    """Reverse direction: CG entry first (None), then DexScreener wins outright."""
    cg_first = token_factory(
        contract_address="0xshared",
        quote_symbol=None,
        dex_id=None,
    )
    ds_second = token_factory(
        contract_address="0xshared",
        quote_symbol="USDT",
        dex_id="uniswap",
    )
    result = aggregate([cg_first, ds_second])
    assert len(result) == 1
    assert result[0].quote_symbol == "USDT"
    assert result[0].dex_id == "uniswap"
