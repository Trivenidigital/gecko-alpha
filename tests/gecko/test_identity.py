from gecko.identity import EVM_CHAINS, norm_address, token_id


def test_evm_chains():
    assert EVM_CHAINS == frozenset({"base", "ethereum", "bsc"})


def test_norm_address_lowercases_evm_and_strips():
    for chain in ("base", "ethereum", "bsc"):
        assert norm_address(chain, " 0xAbCdEf ") == "0xabcdef"


def test_norm_address_keeps_solana_case():
    assert norm_address("solana", " So1anaMint ") == "So1anaMint"


def test_token_id():
    assert token_id("base", "0xABC") == "base:0xabc"
    assert token_id("solana", "So1anaMint") == "solana:So1anaMint"
