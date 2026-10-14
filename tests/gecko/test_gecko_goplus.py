import pytest

from gecko.collectors.goplus import GoPlusError, goplus_features, pick_result

BRETT = "0x532f27101965dd16442e59d40670faf5ebb142e4"
POOL = "0x0ca6485b7e9cf814a3fd09d81672b07323535b64"
RAYDIUM_AUTH = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
WIF = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"


def evm(**over):
    r = {
        "is_honeypot": "0",
        "cannot_sell_all": "0",
        "is_blacklisted": "0",
        "buy_tax": "0",
        "sell_tax": "0",
        "is_mintable": "0",
        "is_proxy": "0",
        "owner_address": "",
        "holder_count": "1182384",
        "creator_address": "0x3C12B77aE8B7DD1FEB63D1D6a2A819AcdA0a41d2",
        "dex": [{"pair": POOL}],
        "lp_holders": [],
        # probed BRETT shape: #1 is a contract with 22.6%
        "holders": [
            {
                "address": "0x06a19654e0872ba71c2261ea691ecf8a0c677156",
                "percent": "0.2262",
                "is_contract": 1,
                "is_locked": 0,
            },
            {
                "address": "0x82d4cd9bf16017fd518511d8bbe6d0493af31a10",
                "percent": "0.0562",
                "is_contract": 0,
                "is_locked": 0,
            },
            {
                "address": "0xefef4558802bf373ce3307189c79a9cab0a4cb9c",
                "percent": "0.0544",
                "is_contract": 1,
                "is_locked": 0,
            },
            {
                "address": "0x9773c9e7d048ae91a4fd9a48cd0b984e31fa418c",
                "percent": "0.0530",
                "is_contract": 0,
                "is_locked": 0,
            },
            {
                "address": "0x000000000000000000000000000000000000dead",
                "percent": "0.1000",
                "is_contract": 0,
                "is_locked": 0,
            },
            {"address": POOL, "percent": "0.0900", "is_contract": 0, "is_locked": 0},
            {
                "address": "0x4cc69ff32638a7e2b5e2f0c4e6de6b0ab7f9b6c8",
                "percent": "0.0479",
                "is_contract": 0,
                "is_locked": 1,
            },
        ],
    }
    r.update(over)
    return r


def sol(**over):
    r = {
        "mintable": {"status": "0"},
        "freezable": {"status": "0"},
        "balance_mutable_authority": {"status": "0"},
        "non_transferable": "0",
        "transfer_hook": [],
        "trusted_token": 0,
        "holder_count": "262738",
        "dex": [{"id": "EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx"}],
        "holders": [
            {
                "account": "51yZyDSnec4xnUv7XLRVYcDyV4x3wUtzrDcRaYbmQU5j",
                "percent": "0.1372",
                "is_locked": 0,
            },
            {
                "account": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
                "percent": "0.0967",
                "is_locked": 0,
            },
            {"account": RAYDIUM_AUTH, "percent": "0.0143", "is_locked": 0},
            {
                "account": "EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx",
                "percent": "0.0500",
                "is_locked": 0,
            },
        ],
    }
    r.update(over)
    return r


def test_evm_clean_token():
    f = goplus_features("base", evm())
    assert f["goplus_status"] == "ok"
    assert f["security_ok"] is True and f["security_flags"] == []
    assert f["holders"] == 1182384
    assert f["deployer"] == "0x3c12b77ae8b7dd1feb63d1d6a2a819acda0a41d2"


def test_evm_top10_excludes_contracts_locked_burn_and_pools():
    # only the two EOAs count: 5.62% + 5.30%
    assert goplus_features("base", evm())["top10_pct"] == pytest.approx(10.92)


def test_evm_top10_also_excludes_lp_holders():
    r = evm(lp_holders=[{"address": "0x82d4cd9bf16017fd518511d8bbe6d0493af31a10"}])
    assert goplus_features("base", r)["top10_pct"] == pytest.approx(5.30)


@pytest.mark.parametrize(
    "over,flag",
    [
        ({"is_honeypot": "1"}, "honeypot"),
        ({"cannot_sell_all": "1"}, "cannot_sell_all"),
        ({"is_blacklisted": "1"}, "blacklist"),
        ({"sell_tax": "0.11"}, "sell_tax"),
        ({"buy_tax": "0.5"}, "buy_tax"),
        (
            {
                "is_mintable": "1",
                "owner_address": "0x1111111111111111111111111111111111111111",
            },
            "mintable_owned",
        ),
        (
            {
                "is_proxy": "1",
                "owner_address": "0x1111111111111111111111111111111111111111",
            },
            "proxy_owned",
        ),
    ],
)
def test_evm_each_disqualifier_names_itself(over, flag):
    f = goplus_features("base", evm(**over))
    assert f["security_ok"] is False and f["security_flags"] == [flag]


@pytest.mark.parametrize(
    "owner",
    [
        "",
        "0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead",
    ],
)
def test_evm_mintable_with_renounced_owner_is_ok(owner):
    f = goplus_features("base", evm(is_mintable="1", is_proxy="1", owner_address=owner))
    assert f["security_ok"] is True


def test_evm_tax_exactly_ten_percent_is_ok():
    assert goplus_features("base", evm(sell_tax="0.10"))["security_ok"] is True


def test_sol_clean_token_and_top10_excludes_amm_and_pool():
    f = goplus_features("solana", sol())
    assert f["security_ok"] is True and f["holders"] == 262738
    assert f["top10_pct"] == pytest.approx(23.39)  # 13.72 + 9.67
    assert f["deployer"] is None


@pytest.mark.parametrize(
    "over,flag",
    [
        ({"mintable": {"status": "1"}}, "mintable"),
        ({"freezable": {"status": "1"}}, "freezable"),
        ({"balance_mutable_authority": {"status": "1"}}, "balance_mutable"),
        ({"non_transferable": "1"}, "non_transferable"),
        ({"transfer_hook": [{"address": "x"}]}, "transfer_hook"),
    ],
)
def test_sol_each_disqualifier_names_itself(over, flag):
    f = goplus_features("solana", sol(**over))
    assert f["security_ok"] is False and f["security_flags"] == [flag]


def test_sol_trusted_token_overrides():
    f = goplus_features("solana", sol(mintable={"status": "1"}, trusted_token=1))
    assert f["security_ok"] is True


def test_unknown_and_partial_results_are_none():
    # core flag missing -> unknown, never safe
    assert goplus_features("base", evm(is_honeypot=None))["security_ok"] is None
    assert goplus_features("base", evm(is_honeypot=""))["security_ok"] is None
    assert goplus_features("solana", sol(freezable={}))["security_ok"] is None
    # a known failure still wins over a missing flag
    assert (
        goplus_features("base", evm(is_honeypot=None, is_blacklisted="1"))[
            "security_ok"
        ]
        is False
    )
    # malformed holders -> None, not a crash
    f = goplus_features("base", evm(holders="x", holder_count="n/a"))
    assert f["top10_pct"] is None and f["holders"] is None


def test_top10_none_when_no_holders_listed():
    assert goplus_features("base", evm(holders=[]))["top10_pct"] is None


def test_pick_result_matches_address_and_reads_unknown_as_none():
    body = {"code": 1, "result": {BRETT: {"is_honeypot": "0"}}}
    assert pick_result("base", BRETT.upper().replace("0X", "0x"), body) == {
        "is_honeypot": "0"
    }
    assert pick_result("base", BRETT, {"code": 1, "result": {}}) is None
    assert pick_result("solana", WIF, {"code": 2, "result": {WIF: {"a": 1}}}) == {
        "a": 1
    }
    # a different token's entry is never taken
    assert pick_result("base", BRETT, {"code": 1, "result": {POOL: {}}}) is None


def test_pick_result_raises_on_error_code():
    with pytest.raises(GoPlusError) as ei:
        pick_result("base", BRETT, {"code": 4029, "message": "too many requests"})
    assert ei.value.code == 4029
    with pytest.raises(GoPlusError):
        pick_result("base", BRETT, "not a dict")
