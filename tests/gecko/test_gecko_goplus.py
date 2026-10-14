import aiohttp
import pytest
from aioresponses import aioresponses

from gecko.collectors.goplus import (
    GoPlusError,
    calls_today,
    enrich,
    enrich_queue,
    goplus_features,
    pick_result,
)
from gecko.features import compute_features
from gecko.runs import PartialFailure

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


@pytest.mark.parametrize("result", [[{"is_honeypot": "0"}], "oops"])
def test_pick_result_non_dict_result_raises_goplus_error(result):
    with pytest.raises(GoPlusError) as ei:
        pick_result("base", BRETT, {"code": 1, "result": result})
    assert ei.value.code == 1


def test_pick_result_empty_list_result_is_unknown():
    assert pick_result("base", BRETT, {"code": 1, "result": []}) is None


@pytest.mark.parametrize("over", [{"dex": 5}, {"lp_holders": "x"}])
def test_top10_tolerates_malformed_dex_and_lp_holders(over):
    # the malformed exclusion list is ignored; POOL then counts as a holder
    assert goplus_features("base", evm(**over))["top10_pct"] is not None


@pytest.mark.parametrize("key", ["is_honeypot", "cannot_sell_all", "is_blacklisted"])
def test_evm_missing_core_flag_is_unknown(key):
    r = evm()
    del r[key]
    assert goplus_features("base", r)["security_ok"] is None


def test_evm_missing_taxes_are_not_required():
    r = evm()
    del r["buy_tax"], r["sell_tax"]
    assert goplus_features("base", r)["security_ok"] is True


@pytest.mark.parametrize("over", [{"is_mintable": "1"}, {"is_proxy": "1"}])
def test_evm_mintable_or_proxy_without_owner_key_is_unknown(over):
    r = evm(**over)
    del r["owner_address"]
    f = goplus_features("base", r)
    assert f["security_ok"] is None and f["security_flags"] == []


def test_evm_absent_owner_still_fails_on_another_flag():
    r = evm(is_mintable="1", sell_tax="0.5")
    del r["owner_address"]
    f = goplus_features("base", r)
    assert f["security_ok"] is False and f["security_flags"] == ["sell_tax"]


@pytest.mark.parametrize(
    "key", ["mintable", "freezable", "balance_mutable_authority", "non_transferable"]
)
def test_sol_missing_core_flag_is_unknown(key):
    r = sol()
    del r[key]
    assert goplus_features("solana", r)["security_ok"] is None


def test_code_2_partial_body_reads_unknown():
    partial = {k: v for k, v in evm().items() if k != "cannot_sell_all"}
    r = pick_result("base", BRETT, {"code": 2, "result": {BRETT: partial}})
    assert goplus_features("base", r)["security_ok"] is None


NOW = "2026-10-14T12:00:00Z"
SOL_URL = "https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses="
BASE_URL = "https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses="
A = "0x" + "a" * 40
B = "0x" + "b" * 40


async def tok(store, chain, addr, status="active", first="2026-10-10T00:00:00Z"):
    tid = f"{chain}:{addr}"
    await store.upsert_token(tid, chain, addr, "T", "T", first, "dexscreener")
    await store.set_token_status(tid, status)
    return tid


async def enrich_rows(store, tid):
    return await store.observations(tid, as_of=NOW, source="enrich")


async def test_enrich_writes_features_and_deployer(store, settings):
    tid = await tok(store, "base", A)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 1, "result": {A: evm()}})
            r = await enrich(store, settings, s, NOW, sleep=_nosleep)
    assert r.rows_written == 1 and r.credits_used == 1
    (o,) = await enrich_rows(store, tid)
    assert o.features["security_ok"] is True and "deployer" not in o.features
    row = await (
        await store.conn.execute("SELECT deployer FROM tokens WHERE token_id=?", (tid,))
    ).fetchone()
    assert row[0] == "0x3c12b77ae8b7dd1feb63d1d6a2a819acda0a41d2"


async def test_empty_result_writes_unknown_marker(store, settings):
    tid = await tok(store, "base", A)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 1, "result": {}})
            await enrich(store, settings, s, NOW, sleep=_nosleep)
    (o,) = await enrich_rows(store, tid)
    assert o.features == {"goplus_status": "unknown"}


async def test_failed_security_disqualifies_candidate(store, settings):
    tid = await tok(store, "base", A, status="candidate")
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(
                BASE_URL + A, payload={"code": 1, "result": {A: evm(is_honeypot="1")}}
            )
            await enrich(store, settings, s, NOW, sleep=_nosleep)
    row = await (
        await store.conn.execute(
            "SELECT status, disqualify_reason FROM tokens WHERE token_id=?", (tid,)
        )
    ).fetchone()
    assert row == ("disqualified", "security_fail")


async def test_queue_priority_and_refresh_rules(store, settings):
    cand_new = await tok(store, "base", "0x" + "1" * 40, status="candidate")
    cand_ok = await tok(store, "base", "0x" + "2" * 40, status="candidate")
    cand_unknown = await tok(store, "base", "0x" + "3" * 40, status="candidate")
    act_stale = await tok(store, "base", "0x" + "4" * 40)
    act_fresh = await tok(store, "base", "0x" + "5" * 40)
    wl = await tok(store, "base", "0x" + "6" * 40)
    await store.watchlist_upsert(wl, "2026-10-13T00:00:00Z", "F4", 1, 1.0, {})
    old, recent = "2026-10-13T10:00:00Z", "2026-10-14T06:00:00Z"
    await store.add_observation(cand_ok, "enrich", old, {"goplus_status": "ok"})
    await store.add_observation(
        cand_unknown, "enrich", old, {"goplus_status": "unknown"}
    )
    await store.add_observation(act_stale, "enrich", old, {"goplus_status": "ok"})
    await store.add_observation(act_fresh, "enrich", recent, {"goplus_status": "ok"})
    q = [t for t, _, _ in await enrich_queue(store, settings, NOW)]
    assert q == [wl, act_stale, cand_new, cand_unknown]


async def test_queue_skips_unconfigured_chain_and_benchmark(store, settings):
    settings.chains = ["solana"]
    await tok(store, "base", A)
    await store.upsert_token(
        "benchmark:btc", "benchmark", "btc", "BTC", "Bitcoin", NOW, "coingecko"
    )
    assert await enrich_queue(store, settings, NOW) == []


async def test_run_cap_and_daily_cap(store, settings):
    settings.gp_max_per_run = 1
    await tok(store, "base", A)
    await tok(store, "base", B)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 1, "result": {A: evm()}})
            r = await enrich(store, settings, s, NOW, sleep=_nosleep)
    assert r.credits_used == 1
    await store.record_run("enrich", NOW, NOW, True, credits_used=999)
    assert await calls_today(store, NOW) == 999
    settings.gp_daily_cap = 999
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            r = await enrich(store, settings, s, NOW, sleep=_nosleep, budget_now=NOW)
            assert not m.requests
    assert r.credits_used == 0


async def test_error_code_counts_failure_and_other_tokens_continue(store, settings):
    await tok(store, "base", A)
    tb = await tok(store, "base", B)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 4029, "message": "rate"})
            m.get(BASE_URL + B, payload={"code": 1, "result": {B: evm()}})
            r = await enrich(store, settings, s, NOW, sleep=_nosleep)
    assert r.rows_written == 1 and len(await enrich_rows(store, tb)) == 1


async def test_every_fetch_failing_raises_partial_failure(store, settings):
    await tok(store, "base", A)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, status=400)
            with pytest.raises(PartialFailure) as ei:
                await enrich(store, settings, s, NOW, sleep=_nosleep)
    assert ei.value.credits_used == 1


async def test_solana_uses_solana_endpoint(store, settings):
    tid = await tok(store, "solana", WIF)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(SOL_URL + WIF, payload={"code": 1, "result": {WIF: sol()}})
            await enrich(store, settings, s, NOW, sleep=_nosleep)
    (o,) = await enrich_rows(store, tid)
    assert o.features["top10_pct"] == pytest.approx(23.39)


async def test_enrich_feeds_features(store, settings):
    tid = await tok(store, "base", A)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 1, "result": {A: evm()}})
            await enrich(store, settings, s, NOW, sleep=_nosleep)
    f = await compute_features(store, settings, tid, NOW)
    assert f.security_ok is True and f.holders == 1182384
    assert f.top10_pct == pytest.approx(10.92) and f.bundle_flag is False


async def _nosleep(_):
    return None
