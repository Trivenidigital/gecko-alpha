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


def test_is_valid_address():
    from gecko.identity import is_valid_address

    evm = "0x" + "aB" * 20
    assert is_valid_address("base", evm) and is_valid_address("bsc", evm.lower())
    assert not is_valid_address("ethereum", evm[:-1])
    assert not is_valid_address("base", evm + "0")
    assert not is_valid_address("base", "0x" + "g" * 40)
    assert not is_valid_address("base", evm + "\n")
    sol = "So11111111111111111111111111111111111111112"
    assert is_valid_address("solana", sol)
    assert is_valid_address("solana", "1" * 32) and is_valid_address("solana", "z" * 44)
    for bad in ("1" * 31, "1" * 45, "O" + "1" * 40, "l" + "1" * 40, "../" + "1" * 40):
        assert not is_valid_address("solana", bad)
    assert not is_valid_address("solana", evm)
    assert not is_valid_address("tron", sol)
    assert not is_valid_address("solana", None)
