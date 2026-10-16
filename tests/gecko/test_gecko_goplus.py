from types import SimpleNamespace

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


def test_evm_top10_counts_an_eoa_that_also_holds_lp():
    # lp_holders are LP-TOKEN holders (often the deployer EOA), not pools: a dev
    # holding 65% of supply who also added liquidity must still be counted
    dev = "0x" + "d" * 40
    r = evm(
        lp_holders=[{"address": dev, "percent": "1.0", "is_locked": 0}],
        holders=[
            {"address": dev, "percent": "0.65", "is_contract": 0, "is_locked": 0},
            {"address": "0x" + "e" * 40, "percent": "0.02", "is_contract": 0},
        ],
    )
    f = goplus_features("base", r)
    assert f["top10_pct"] == pytest.approx(67.0)
    assert f["top10_pct"] > 60  # concentration disqualifier threshold


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


@pytest.mark.parametrize("owner", ["absent", None])
@pytest.mark.parametrize("over", [{"is_mintable": "1"}, {"is_proxy": "1"}])
def test_evm_mintable_or_proxy_without_owner_key_is_unknown(over, owner):
    # GoPlus: no value = owner unknown; "" = no owner (renounced)
    r = evm(**over)
    if owner == "absent":
        del r["owner_address"]
    else:
        r["owner_address"] = owner
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
    cand_partial = await tok(store, "base", "0x" + "7" * 40, status="candidate")
    await store.add_observation(
        cand_ok, "enrich", old, {"goplus_status": "ok", "security_ok": True}
    )
    await store.add_observation(
        cand_unknown, "enrich", old, {"goplus_status": "unknown"}
    )
    # an ok read with no verdict (code 2 / missing core flag) still gets the daily retry
    await store.add_observation(
        cand_partial, "enrich", old, {"goplus_status": "ok", "security_ok": None}
    )
    await store.add_observation(act_stale, "enrich", old, {"goplus_status": "ok"})
    await store.add_observation(act_fresh, "enrich", recent, {"goplus_status": "ok"})
    q = [t for t, _, _ in await enrich_queue(store, settings, NOW)]
    assert q == [wl, act_stale, cand_new, cand_unknown, cand_partial]


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


async def test_queue_never_enriched_is_fifo_by_first_seen(store, settings):
    late = await tok(store, "base", "0x" + "1" * 40, first="2026-10-12T00:00:00Z")
    early = await tok(store, "base", "0x" + "9" * 40, first="2026-10-11T00:00:00Z")
    assert late < early  # token_id order is the reverse of arrival order
    q = [t for t, _, _ in await enrich_queue(store, settings, NOW)]
    assert q == [early, late]


async def test_enrich_slo_needs_a_known_verdict_and_done_counts_unknowns(
    store, settings
):
    # GoPlus drops a core flag: every row is goplus_status=ok with no verdict.
    # Freshness must page, and enrich_done must say how many verdicts were unknown.
    from datetime import timedelta

    from structlog.testing import capture_logs

    from gecko.freshness import check_freshness
    from gecko.store import parse_utc

    def at(h):
        return (parse_utc(NOW) + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")

    await store.record_run("enrich", at(-25), at(-25), True)
    a, b, c = A, "0x" + "b" * 40, "0x" + "c" * 40
    for addr in (a, b, c):
        await tok(store, "base", addr)
    partial = evm()
    del partial["is_honeypot"]
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + a, payload={"code": 1, "result": {a: partial}})
            m.get(BASE_URL + b, payload={"code": 1, "result": {b: dict(partial)}})
            m.get(BASE_URL + c, payload={"code": 1, "result": {}})
            with capture_logs() as logs:
                await enrich(store, settings, s, NOW, sleep=_nosleep)
    (done,) = [e for e in logs if e["event"] == "enrich_done"]
    assert done["written"] == 3 and done["unknown_security"] == 3
    tables = {x.table for x in await check_freshness(store, at(2))}
    assert "observations:enrich" in tables
    tid = await tok(store, "base", "0x" + "d" * 40)
    await store.add_observation(
        tid, "enrich", NOW, {"goplus_status": "ok", "security_ok": False}
    )
    assert "observations:enrich" not in {
        x.table for x in await check_freshness(store, at(2))
    }


async def test_security_ok_is_the_newest_read_not_the_newest_known(store, settings):
    # day 1 reads True, day 2 loses a core flag: the token is unknown again
    from datetime import timedelta

    from gecko.store import parse_utc

    tid = await tok(store, "base", A)
    yday = (parse_utc(NOW) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    partial = evm()
    del partial["cannot_sell_all"]
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 1, "result": {A: evm()}})
            m.get(BASE_URL + A, payload={"code": 1, "result": {A: partial}})
            await enrich(store, settings, s, yday, sleep=_nosleep, budget_now=yday)
            assert (await compute_features(store, settings, tid, yday)).security_ok
            await enrich(store, settings, s, NOW, sleep=_nosleep, budget_now=NOW)
    assert (await compute_features(store, settings, tid, NOW)).security_ok is None


async def test_crash_mid_run_records_the_goplus_calls_spent(
    store, settings, monkeypatch
):
    # a lock timeout after N calls must still charge N against the daily cap
    import sqlite3

    from gecko.collectors.goplus import enrich_job
    from gecko.store import Store

    b, c = "0x" + "b" * 40, "0x" + "c" * 40
    for addr in (A, b, c):
        await tok(store, "base", addr)
    real, seen = Store.add_observation, []

    async def flaky(self, *a, **kw):
        seen.append(a)
        if len(seen) == 2:
            raise sqlite3.OperationalError("database is locked")
        return await real(self, *a, **kw)

    monkeypatch.setattr(Store, "add_observation", flaky)
    settings.gp_min_interval_sec = 0
    with aioresponses() as m:
        for addr in (A, b, c):
            m.get(BASE_URL + addr, payload={"code": 1, "result": {addr: evm()}})
        rc = await enrich_job(settings, SimpleNamespace(as_of=None))
    row = await (
        await store.conn.execute(
            "SELECT ok, credits_used, rows_written FROM runs WHERE job='enrich'"
        )
    ).fetchone()
    assert rc == 1 and row == (0, 2, 1)


async def _status_of(store, tid):
    return await (
        await store.conn.execute(
            "SELECT status, disqualify_reason FROM tokens WHERE token_id=?", (tid,)
        )
    ).fetchone()


async def test_tax_only_failure_keeps_candidate(store, settings):
    from structlog.testing import capture_logs

    tid = await tok(store, "base", A, status="candidate")
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 1, "result": {A: evm(buy_tax="0.2")}})
            with capture_logs() as logs:
                await enrich(store, settings, s, NOW, sleep=_nosleep)
    (o,) = await enrich_rows(store, tid)
    assert o.features["security_ok"] is False
    assert o.features["security_structural"] is True
    assert o.features["security_flags"] == ["buy_tax"]
    assert (await _status_of(store, tid))[0] == "candidate"
    (ev,) = [e for e in logs if e["event"] == "token_security_tax"]
    assert ev["token_id"] == tid and ev["flags"] == ["buy_tax"]
    assert not [e for e in logs if e["event"] == "token_security_fail"]


async def test_structural_flag_disqualifies(store, settings):
    from structlog.testing import capture_logs

    tid = await tok(store, "base", A, status="candidate")
    bad = evm(is_honeypot="1", sell_tax="0.5")
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 1, "result": {A: bad}})
            with capture_logs() as logs:
                await enrich(store, settings, s, NOW, sleep=_nosleep)
    (o,) = await enrich_rows(store, tid)
    assert o.features["security_structural"] is False
    assert await _status_of(store, tid) == ("disqualified", "security_fail")
    (ev,) = [e for e in logs if e["event"] == "token_security_fail"]
    assert ev["token_id"] == tid and ev["flags"] == ["honeypot", "sell_tax"]
    assert not [e for e in logs if e["event"] == "token_security_tax"]


@pytest.mark.parametrize("key", ["is_mintable", "is_proxy"])
def test_mintable_owned_is_structural(key):
    owned = evm(**{key: "1", "owner_address": "0x" + "1" * 40})
    f = goplus_features("base", owned)
    assert f["security_ok"] is False and f["security_structural"] is False
    # renounced: no flag, known -> True; owner unknown -> None (never safe)
    assert goplus_features("base", evm(**{key: "1"}))["security_structural"] is True
    r = evm(**{key: "1"})
    del r["owner_address"]
    assert goplus_features("base", r)["security_structural"] is None
    # every Solana flag is structural; a tax-only EVM failure is not
    frozen = sol(freezable={"status": "1"})
    assert goplus_features("solana", frozen)["security_structural"] is False
    assert goplus_features("solana", sol(freezable={}))["security_structural"] is None
    assert goplus_features("base", evm(sell_tax="0.5"))["security_structural"] is True


@pytest.mark.parametrize(
    "mock,code",
    [
        ({"payload": {"code": 4012, "message": "bad"}}, 4012),
        ({"status": 404}, 404),
        ({"status": 400}, 400),
    ],
)
async def test_error_marker_written_on_goplus_code_error_and_4xx(
    store, settings, mock, code
):
    tid = await tok(store, "base", A, status="candidate")
    tb = await tok(store, "base", B, status="candidate")
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, **mock)
            m.get(BASE_URL + B, payload={"code": 1, "result": {B: evm()}})
            await enrich(store, settings, s, NOW, sleep=_nosleep)
    (o,) = await enrich_rows(store, tid)
    assert o.features == {"goplus_status": "error", "code": code}
    # the marked token left the never-enriched head
    assert tid not in [t for t, _, _ in await enrich_queue(store, settings, NOW)]


@pytest.mark.parametrize("status", [429, 503])
async def test_429_writes_no_marker(store, settings, status, monkeypatch):
    import gecko.collectors.goplus as gp

    real = gp.get_json

    async def fast(*a, **kw):  # skip the 2/4/8 s retry backoff
        return await real(*a, sleep=_nosleep, **kw)

    monkeypatch.setattr(gp, "get_json", fast)
    tid = await tok(store, "base", A, status="candidate")
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, status=status, repeat=True)
            with pytest.raises(PartialFailure):
                await enrich(store, settings, s, NOW, sleep=_nosleep)
    assert await enrich_rows(store, tid) == []
    assert [t for t, _, _ in await enrich_queue(store, settings, NOW)] == [tid]


async def _tax_row(store, tid, at, structural=True):
    feats = {"goplus_status": "ok", "security_ok": False, "security_flags": ["x"]}
    if structural is not None:
        feats["security_structural"] = structural
    await store.add_observation(tid, "enrich", at, feats)


async def test_tax_failed_candidate_requeued_after_refresh_hours(store, settings):
    from structlog.testing import capture_logs

    settings.gp_refresh_hours = 24
    old = "2026-10-13T11:00:00Z"  # 25h before NOW
    tax = await tok(store, "base", "0x" + "1" * 40, status="candidate")
    await _tax_row(store, tax, old)
    legacy = await tok(store, "base", "0x" + "2" * 40, status="candidate")
    await _tax_row(store, legacy, old, structural=None)  # no key: not False
    struct = await tok(store, "base", "0x" + "3" * 40, status="candidate")
    await _tax_row(store, struct, old, structural=False)
    clean = await tok(store, "base", "0x" + "4" * 40, status="candidate")
    await store.add_observation(
        clean, "enrich", old, {"goplus_status": "ok", "security_ok": True}
    )
    unknown = await tok(store, "base", "0x" + "5" * 40, status="candidate")
    await store.add_observation(unknown, "enrich", old, {"goplus_status": "unknown"})
    new = await tok(store, "base", "0x" + "6" * 40, status="candidate")
    act = await tok(store, "base", "0x" + "7" * 40)
    await store.add_observation(act, "enrich", old, {"goplus_status": "ok"})
    q = [t for t, _, _ in await enrich_queue(store, settings, NOW)]
    # actives, never-enriched, never-known refresh, then tax-refresh
    assert q == [act, new, unknown, tax, legacy]
    # a newer non-verdict row does not hide the tax verdict
    await store.add_observation(tax, "enrich", old, {"goplus_status": "unknown"})
    assert tax in [t for t, _, _ in await enrich_queue(store, settings, NOW)]
    # enrich_done reports the whole due queue, not only the run slice
    settings.gp_max_per_run = 0
    async with aiohttp.ClientSession() as s:
        with capture_logs() as logs:
            await enrich(store, settings, s, NOW, sleep=_nosleep)
    (done,) = [e for e in logs if e["event"] == "enrich_done"]
    assert done["enrich_queue_len"] == 5


async def test_tax_failed_candidate_not_requeued_before_refresh_hours(store, settings):
    settings.gp_refresh_hours = 24
    ta = await tok(store, "base", A, status="candidate")
    await _tax_row(store, ta, "2026-10-13T13:00:00Z")  # 23h before NOW
    assert await enrich_queue(store, settings, NOW) == []
    # a newer known clean verdict ends the tax refresh
    tb = await tok(store, "base", B, status="candidate")
    await _tax_row(store, tb, "2026-10-12T00:00:00Z")
    clean = {"goplus_status": "ok", "security_ok": True}
    await store.add_observation(tb, "enrich", "2026-10-12T01:00:00Z", clean)
    assert await enrich_queue(store, settings, NOW) == []


def test_enrich_refuses_as_of(monkeypatch, settings, capsys):
    import gecko.__main__
    from gecko.jobs import JOBS

    monkeypatch.setattr(gecko.__main__, "GeckoSettings", lambda: settings)
    ran = []

    async def job(settings, ns):
        ran.append(ns.as_of)
        return 0

    monkeypatch.setitem(JOBS, "enrich", job)
    assert gecko.__main__.main(["enrich", "--as-of", "2026-10-01T00:00:00Z"]) == 2
    assert "enrich is live-only" in capsys.readouterr().err
    assert ran == []
    assert gecko.__main__.main(["enrich"]) == 0 and ran == [None]
