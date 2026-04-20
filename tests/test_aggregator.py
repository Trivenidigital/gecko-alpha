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


from scout.aggregator import apply_boost_decorations
from scout.ingestion.dexscreener import BoostInfo

EVM_ADDR_UPPER = "0xAbC0000000000000000000000000000000000001"
EVM_ADDR_LOWER = EVM_ADDR_UPPER.lower()
SOL_ADDR_A = "7GAGFk8aJMbNSRtCh8bB9x6eVpKZwxzMnB3UsNYukgmo"
SOL_ADDR_B = (
    "7GAGFk8aJMbNSRtCh8bB9x6eVpKZwxzMnB3UsNYukgMO"  # differs in last two chars' case
)


def test_apply_boost_decorations_match_evm_case_insensitive(token_factory):
    cand = token_factory(contract_address=EVM_ADDR_UPPER, chain="ethereum")
    boost = BoostInfo(chain="ethereum", address=EVM_ADDR_LOWER, total_amount=1500.0)
    result = apply_boost_decorations([cand], [boost])
    assert len(result) == 1
    assert result[0].boost_total_amount == 1500.0
    assert result[0].boost_rank == 1


def test_apply_boost_decorations_match_reverse_case(token_factory):
    cand = token_factory(contract_address=EVM_ADDR_LOWER, chain="base")
    boost = BoostInfo(chain="base", address=EVM_ADDR_UPPER, total_amount=900.0)
    result = apply_boost_decorations([cand], [boost])
    assert result[0].boost_total_amount == 900.0
    assert result[0].boost_rank == 1


def test_apply_boost_decorations_solana_case_sensitive(token_factory):
    cand = token_factory(contract_address=SOL_ADDR_A, chain="solana")
    # A Solana boost whose address differs only by case MUST NOT match.
    boost = BoostInfo(chain="solana", address=SOL_ADDR_B, total_amount=1500.0)
    result = apply_boost_decorations([cand], [boost])
    assert result[0].boost_total_amount is None
    assert result[0].boost_rank is None


def test_apply_boost_decorations_solana_exact_match(token_factory):
    cand = token_factory(contract_address=SOL_ADDR_A, chain="solana")
    boost = BoostInfo(chain="solana", address=SOL_ADDR_A, total_amount=1500.0)
    result = apply_boost_decorations([cand], [boost])
    assert result[0].boost_total_amount == 1500.0


def test_apply_boost_decorations_no_match_leaves_candidate(token_factory):
    cand = token_factory(contract_address="0xY", chain="ethereum")
    boost = BoostInfo(chain="ethereum", address="0xX", total_amount=1500.0)
    result = apply_boost_decorations([cand], [boost])
    assert result[0].boost_total_amount is None
    assert result[0].boost_rank is None


def test_apply_boost_decorations_rank_order(token_factory):
    a = token_factory(contract_address="0xaaa1" + "0" * 36, chain="ethereum")
    b = token_factory(contract_address="0xbbb2" + "0" * 36, chain="ethereum")
    c = token_factory(contract_address="0xccc3" + "0" * 36, chain="ethereum")
    boosts = [
        BoostInfo(chain="ethereum", address="0xbbb2" + "0" * 36, total_amount=3000.0),
        BoostInfo(chain="ethereum", address="0xaaa1" + "0" * 36, total_amount=2000.0),
        BoostInfo(chain="ethereum", address="0xccc3" + "0" * 36, total_amount=1000.0),
    ]
    result = apply_boost_decorations([a, b, c], boosts)
    by_addr = {t.contract_address: t for t in result}
    assert by_addr["0xaaa1" + "0" * 36].boost_rank == 2
    assert by_addr["0xbbb2" + "0" * 36].boost_rank == 1
    assert by_addr["0xccc3" + "0" * 36].boost_rank == 3


def test_apply_boost_decorations_chain_must_match(token_factory):
    """Same address on two chains: only the matching-chain candidate is decorated."""
    cand_sol = token_factory(contract_address="SAME_ADDR_XYZ", chain="solana")
    cand_base = token_factory(contract_address="SAME_ADDR_XYZ", chain="base")
    boost = BoostInfo(chain="solana", address="SAME_ADDR_XYZ", total_amount=777.0)
    result = apply_boost_decorations([cand_sol, cand_base], [boost])
    by_chain = {t.chain: t for t in result}
    assert by_chain["solana"].boost_total_amount == 777.0
    assert by_chain["base"].boost_total_amount is None


def test_apply_boost_decorations_empty_inputs(token_factory):
    assert apply_boost_decorations([], []) == []
    cand = token_factory()
    assert apply_boost_decorations([cand], []) == [cand]
    assert apply_boost_decorations([], [BoostInfo("solana", "x", 100.0)]) == []
