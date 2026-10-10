import re

import aiohttp
import pytest
from aioresponses import aioresponses
from pydantic import SecretStr

from gecko.collectors.coingecko import BASE, calls_this_month, hourly, markets_sweep
from gecko.http import UpstreamError
from gecko.runs import PartialFailure

NOW = "2026-10-09T00:00:00Z"
MARKETS = re.compile(re.escape(BASE + "/coins/markets") + r"\?.*")
COIN = re.compile(re.escape(BASE + "/coins/") + r"(?!markets)[a-z0-9-]+\?.*")
TRENDING = re.compile(re.escape(BASE + "/search/trending"))
PRICE = re.compile(re.escape(BASE + "/simple/price") + r"\?.*")


def row(cid, mcap, price=1.0):
    return {
        "id": cid,
        "symbol": cid[:3],
        "name": cid.title(),
        "current_price": price,
        "market_cap": mcap,
        "market_cap_rank": 500,
        "total_volume": 1e5,
        "price_change_percentage_24h_in_currency": 1.5,
        "price_change_percentage_7d_in_currency": 10.0,
    }


class RecordingSleep:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


@pytest.fixture
def cg_settings(settings):
    settings.coingecko_demo_api_key = SecretStr("demo")
    settings.cg_first_page = 1
    settings.cg_max_pages = 3
    return settings


async def test_sweep_maps_platforms_and_counts_calls(store, cg_settings):
    sleep = RecordingSleep()
    with aioresponses() as m:
        m.get(
            MARKETS,
            payload=[row("abc-coin", 5e6), row("mega", 9e9), row("nochain", 2e6)],
        )
        m.get(MARKETS, payload=[row("tail", 100.0)])  # last mcap below floor -> stop
        m.get(
            COIN,
            payload={"platforms": {"solana": "So1Addr111111111111111111111111111111"}},
        )
        m.get(COIN, payload={"platforms": {"tron": "T1"}})
        async with aiohttp.ClientSession() as s:
            r = await markets_sweep(store, cg_settings, s, NOW, sleep=sleep)
    assert r.credits_used == 4 and r.rows_written == 1
    # Pacer spaces 4 requests with 3 waits; no retries happened.
    assert len(sleep.calls) == 3
    assert all(0 < d <= cg_settings.cg_min_interval_sec for d in sleep.calls)
    tok = await (
        await store.conn.execute(
            "SELECT token_id, address, cg_id FROM tokens WHERE chain='solana'"
        )
    ).fetchall()
    assert tok == [
        (
            "solana:So1Addr111111111111111111111111111111",
            "So1Addr111111111111111111111111111111",
            "abc-coin",
        )
    ]
    obs = await store.observations(
        "solana:So1Addr111111111111111111111111111111", as_of=NOW, source="coingecko"
    )
    assert obs[0].features["mcap_usd"] == 5e6
    assert obs[0].features["price_change_7d_pct"] == 10.0
    assert "liquidity_usd" not in obs[0].features
    unmapped = await store.observations("cg:nochain", as_of=NOW, source="cg_unmapped")
    assert len(unmapped) == 1
    st = await (
        await store.conn.execute(
            "SELECT chain, status, disqualify_reason FROM tokens "
            "WHERE token_id='cg:nochain'"
        )
    ).fetchone()
    assert st == ("cg", "disqualified", "no_supported_platform")
    calls = [c for reqs in m.requests.values() for c in reqs]
    assert len(calls) == 4
    assert all(c.kwargs["headers"]["x-cg-demo-api-key"] == "demo" for c in calls)


async def test_sweep_lowercases_evm_platform_address(store, cg_settings):
    with aioresponses() as m:
        m.get(MARKETS, payload=[row("evm-coin", 5e6)])
        m.get(
            COIN,
            payload={
                "platforms": {"base": " 0xAbCdEf0000000000000000000000000000000000 "}
            },
        )
        m.get(MARKETS, payload=[])
        async with aiohttp.ClientSession() as s:
            await markets_sweep(store, cg_settings, s, NOW, sleep=RecordingSleep())
    tok = await (
        await store.conn.execute(
            "SELECT token_id, address FROM tokens WHERE cg_id='evm-coin'"
        )
    ).fetchall()
    assert tok == [
        (
            "base:0xabcdef0000000000000000000000000000000000",
            "0xabcdef0000000000000000000000000000000000",
        )
    ]


async def test_sweep_does_not_refetch_known_platforms(store, cg_settings):
    await store.upsert_token(
        "solana:So1Addr111111111111111111111111111111",
        "solana",
        "So1Addr111111111111111111111111111111",
        "ABC",
        "Abc",
        NOW,
        "coingecko",
        cg_id="abc-coin",
    )
    with aioresponses() as m:
        m.get(MARKETS, payload=[row("abc-coin", 5e6)])
        m.get(MARKETS, payload=[])
        async with aiohttp.ClientSession() as s:
            r = await markets_sweep(store, cg_settings, s, NOW, sleep=RecordingSleep())
    assert r.credits_used == 2 and r.rows_written == 1


async def test_sweep_does_not_refetch_unmapped(store, cg_settings):
    with aioresponses() as m:
        m.get(MARKETS, payload=[row("nochain", 2e6)])
        m.get(COIN, payload={"platforms": {"tron": "T1"}})
        m.get(MARKETS, payload=[])
        m.get(MARKETS, payload=[row("nochain", 2e6)])
        m.get(MARKETS, payload=[])
        async with aiohttp.ClientSession() as s:
            first = await markets_sweep(
                store, cg_settings, s, NOW, sleep=RecordingSleep()
            )
            second = await markets_sweep(
                store, cg_settings, s, NOW, sleep=RecordingSleep()
            )
    assert first.credits_used == 3 and second.credits_used == 2


async def test_markets_429_then_200(store, cg_settings):
    with aioresponses() as m:
        m.get(MARKETS, status=429)
        m.get(MARKETS, payload=[row("tail", 100.0)])
        async with aiohttp.ClientSession() as s:
            r = await markets_sweep(store, cg_settings, s, NOW, sleep=lambda s: _noop())
    assert r.credits_used == 2


async def _noop():
    return None


async def test_markets_401_fails_run(store, cg_settings):
    with aioresponses() as m:
        m.get(MARKETS, status=401, payload={"status": {"error_code": 10004}})
        async with aiohttp.ClientSession() as s:
            with pytest.raises(PartialFailure) as ei:
                await markets_sweep(store, cg_settings, s, NOW)
    assert isinstance(ei.value.cause, UpstreamError)
    assert ei.value.cause.status == 401
    assert ei.value.credits_used == 1 and ei.value.rows_written == 0


async def test_hourly_failure_reports_spent_credits(store, cg_settings):
    with aioresponses() as m:
        m.get(re.compile(re.escape(BASE + "/search/trending")), payload={"coins": []})
        m.get(re.compile(re.escape(BASE + "/simple/price") + r"\?.*"), status=401)
        async with aiohttp.ClientSession() as s:
            with pytest.raises(PartialFailure) as ei:
                await hourly(store, cg_settings, s, NOW, sleep=RecordingSleep())
    assert ei.value.cause.status == 401 and ei.value.credits_used == 2


async def test_calls_this_month_counts_failed_runs(store):
    await store.record_run(
        "coingecko-markets",
        "2026-10-02T00:00:00Z",
        "2026-10-02T00:01:00Z",
        False,
        credits_used=5,
        error="PartialFailure",
    )
    assert await calls_this_month(store, NOW) == 5


async def test_monthly_cap_stops_before_calling(store, cg_settings):
    cg_settings.coingecko_monthly_cap = 10
    await store.record_run(
        "coingecko-markets",
        "2026-10-01T00:00:00Z",
        "2026-10-01T00:01:00Z",
        True,
        credits_used=9,
    )
    # Last month's spend does not count.
    await store.record_run(
        "coingecko-hourly",
        "2026-09-30T23:00:00Z",
        "2026-09-30T23:01:00Z",
        True,
        credits_used=500,
    )
    assert await calls_this_month(store, NOW) == 9
    with aioresponses() as m:
        async with aiohttp.ClientSession() as s:
            r = await markets_sweep(store, cg_settings, s, NOW, budget_now=NOW)
            with pytest.raises(RuntimeError, match="cg budget exhausted"):
                await hourly(store, cg_settings, s, NOW, budget_now=NOW)
    assert r.credits_used == 0 and r.rows_written == 0
    assert m.requests == {}


async def test_platform_lookups_never_overshoot_monthly_cap(store, cg_settings):
    # 3 market pages planned; the cap leaves room for exactly 1 platform lookup.
    cg_settings.coingecko_monthly_cap = 4
    cg_settings.cg_hourly_reserve = 0
    with aioresponses() as m:
        m.get(
            MARKETS,
            payload=[row("a-coin", 5e6), row("b-coin", 5e6), row("c-coin", 5e6)],
        )
        m.get(
            COIN,
            payload={"platforms": {"solana": "AaaMint111111111111111111111111111111"}},
        )
        m.get(MARKETS, payload=[])
        async with aiohttp.ClientSession() as s:
            r = await markets_sweep(store, cg_settings, s, NOW, sleep=RecordingSleep())
    assert r.credits_used <= cg_settings.coingecko_monthly_cap
    assert r.credits_used == 3 and r.rows_written == 1
    # Coins skipped for budget are not marked unmapped; they retry next sweep.
    skipped = await (
        await store.conn.execute("SELECT COUNT(*) FROM tokens WHERE chain='cg'")
    ).fetchone()
    assert skipped == (0,)


async def test_hourly_btc_and_trending(store, cg_settings):
    await store.upsert_token(
        "solana:So1Addr111111111111111111111111111111",
        "solana",
        "So1Addr111111111111111111111111111111",
        "ABC",
        "Abc",
        NOW,
        "coingecko",
        cg_id="abc-coin",
    )
    sleep = RecordingSleep()
    trending = {
        "coins": [
            {
                "item": {
                    "id": "abc-coin",
                    "symbol": "abc",
                    "name": "Abc",
                    "market_cap_rank": 700,
                }
            },
            {
                "item": {
                    "id": "unknown",
                    "symbol": "u",
                    "name": "U",
                    "market_cap_rank": 9,
                }
            },
        ]
    }
    with aioresponses() as m:
        m.get(re.compile(re.escape(BASE + "/search/trending")), payload=trending)
        m.get(
            re.compile(re.escape(BASE + "/simple/price") + r"\?.*"),
            payload={"bitcoin": {"usd": 61234.5}},
        )
        async with aiohttp.ClientSession() as s:
            r = await hourly(store, cg_settings, s, NOW, sleep=sleep)
    assert r.credits_used == 2 and r.rows_written == 2
    assert len(sleep.calls) == 1
    btc = await store.observations("benchmark:btc", as_of=NOW, source="coingecko")
    assert btc[0].features["price_usd"] == 61234.5
    tr = await store.observations(
        "solana:So1Addr111111111111111111111111111111", as_of=NOW, source="coingecko"
    )
    assert tr[0].features["trending_rank"] == 1
    assert await store.observations("cg:unknown", as_of=NOW) == []


def test_jobs_registered():
    from gecko.__main__ import _load_jobs
    from gecko.jobs import JOBS

    _load_jobs()
    assert {"coingecko-markets", "coingecko-hourly"} <= set(JOBS)


async def test_client_counts_every_attempt_and_raises_after_retries(cg_settings):
    from gecko.collectors.coingecko import CGClient

    sleep = RecordingSleep()
    with aioresponses() as m:
        for _ in range(4):
            m.get(MARKETS, status=503)
        async with aiohttp.ClientSession() as s:
            cg = CGClient(s, cg_settings, sleep)
            with pytest.raises(UpstreamError) as ei:
                await cg.get("/coins/markets", {"page": 1})
    assert ei.value.status == 503
    assert cg.calls == 4
    assert [d for d in sleep.calls if d in (2.0, 4.0, 8.0)] == [2.0, 4.0, 8.0]
    assert len(sleep.calls) == 6  # 3 backoffs + 3 pacer waits between 4 attempts


async def test_budget_uses_real_clock_not_as_of(store, cg_settings):
    cg_settings.coingecko_monthly_cap = 10
    await store.record_run(
        "coingecko-markets",
        "2026-10-02T00:00:00Z",
        "2026-10-02T00:01:00Z",
        False,
        credits_used=10,
    )
    with aioresponses() as m:
        async with aiohttp.ClientSession() as s:
            r = await markets_sweep(
                store,
                cg_settings,
                s,
                "2026-09-15T00:00:00Z",
                budget_now="2026-10-09T12:00:00Z",
            )
            with pytest.raises(RuntimeError, match="cg budget exhausted"):
                await hourly(
                    store,
                    cg_settings,
                    s,
                    "2026-09-15T00:00:00Z",
                    budget_now="2026-10-09T12:00:00Z",
                )
    assert r.credits_used == 0
    assert m.requests == {}


async def test_platform_choice_prefers_configured_chain(store, cg_settings):
    cg_settings.chains = ["solana", "base"]
    with aioresponses() as m:
        m.get(MARKETS, payload=[row("multi", 5e6), row("ethonly", 5e6)])
        m.get(
            COIN,
            payload={
                "platforms": {
                    "ethereum": "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "base": "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                }
            },
        )
        m.get(
            COIN,
            payload={
                "platforms": {"ethereum": "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"}
            },
        )
        m.get(MARKETS, payload=[])
        m.get(MARKETS, payload=[row("multi", 5e6), row("ethonly", 5e6)])
        m.get(MARKETS, payload=[])
        async with aiohttp.ClientSession() as s:
            first = await markets_sweep(
                store, cg_settings, s, NOW, sleep=RecordingSleep()
            )
            second = await markets_sweep(
                store, cg_settings, s, NOW, sleep=RecordingSleep()
            )
    toks = await (
        await store.conn.execute(
            "SELECT token_id, cg_id, status FROM tokens ORDER BY token_id"
        )
    ).fetchall()
    assert toks == [
        ("base:0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "multi", "candidate"),
        ("cg:ethonly", "ethonly", "disqualified"),
    ]
    assert first.credits_used == 4 and first.rows_written == 1
    # Second sweep: two market pages, zero /coins/{id} lookups.
    assert second.credits_used == 2 and second.rows_written == 1


async def test_sweep_leaves_hourly_reserve(store, cg_settings):
    cg_settings.coingecko_monthly_cap = 4
    cg_settings.cg_hourly_reserve = 2
    cg_settings.cg_max_pages = 1
    with aioresponses() as m:
        m.get(MARKETS, payload=[row("a-coin", 5e6), row("b-coin", 5e6)])
        m.get(
            COIN,
            payload={"platforms": {"solana": "AaaMint111111111111111111111111111111"}},
        )
        async with aiohttp.ClientSession() as s:
            r = await markets_sweep(
                store, cg_settings, s, NOW, sleep=RecordingSleep(), budget_now=NOW
            )
    assert r.credits_used == 2 and r.rows_written == 1


async def test_hourly_logs_missing_btc_price(store, cg_settings):
    from structlog.testing import capture_logs

    with aioresponses() as m:
        m.get(TRENDING, payload={"coins": []})
        m.get(PRICE, payload={})
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                r = await hourly(store, cg_settings, s, NOW, sleep=RecordingSleep())
    assert r.credits_used == 2 and r.rows_written == 0
    missing = [e for e in logs if e["event"] == "cg_btc_price_missing"]
    assert len(missing) == 1 and missing[0]["log_level"] == "warning"
    assert await store.observations("benchmark:btc", as_of=NOW) == []


async def test_calls_this_month_is_bounded_to_one_month(store):
    for started, credits in (
        ("2026-09-30T23:59:59Z", 7),
        ("2026-10-31T23:59:59Z", 5),
        ("2026-11-01T00:00:00Z", 11),
        ("2026-12-31T23:00:00Z", 13),
        ("2027-01-01T00:00:00Z", 17),
    ):
        await store.record_run(
            "coingecko-hourly", started, started, True, credits_used=credits
        )
    assert await calls_this_month(store, "2026-10-09T00:00:00Z") == 5
    assert await calls_this_month(store, "2026-11-20T00:00:00Z") == 11
    assert await calls_this_month(store, "2026-12-01T00:00:00Z") == 13


async def test_quota_exhausted_429_is_not_retried(store, cg_settings):
    from structlog.testing import capture_logs

    sleep = RecordingSleep()
    with aioresponses() as m:
        m.get(MARKETS, status=429, payload={"status": {"error_code": 10006}})
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                with pytest.raises(PartialFailure) as ei:
                    await markets_sweep(store, cg_settings, s, NOW, sleep=sleep)
    assert ei.value.cause.status == 429 and ei.value.credits_used == 1
    assert sleep.calls == []
    assert [e["event"] for e in logs].count("cg_quota_exhausted") == 1
    assert "cg_retry" not in [e["event"] for e in logs]


async def test_quota_code_at_top_level_is_not_retried(cg_settings):
    from gecko.collectors.coingecko import CGClient

    sleep = RecordingSleep()
    with aioresponses() as m:
        m.get(MARKETS, status=429, payload={"error_code": 10006})
        async with aiohttp.ClientSession() as s:
            cg = CGClient(s, cg_settings, sleep)
            with pytest.raises(UpstreamError):
                await cg.get("/coins/markets", {"page": 1})
    assert cg.calls == 1 and sleep.calls == []


@pytest.mark.parametrize(
    "body",
    [
        "",
        "<html>Too Many Requests</html>",
        '{"status": {"error_code": 429}}',
    ],
)
async def test_plain_429_is_still_retried(cg_settings, body):
    from gecko.collectors.coingecko import CGClient

    sleep = RecordingSleep()
    with aioresponses() as m:
        m.get(MARKETS, status=429, body=body)
        m.get(MARKETS, payload=[])
        async with aiohttp.ClientSession() as s:
            cg = CGClient(s, cg_settings, sleep)
            assert await cg.get("/coins/markets", {"page": 1}) == []
    assert cg.calls == 2 and 2.0 in sleep.calls


async def test_quota_code_on_non_429_is_still_retried(cg_settings):
    from gecko.collectors.coingecko import CGClient

    sleep = RecordingSleep()
    with aioresponses() as m:
        m.get(MARKETS, status=503, payload={"status": {"error_code": 10006}})
        m.get(MARKETS, payload=[])
        async with aiohttp.ClientSession() as s:
            cg = CGClient(s, cg_settings, sleep)
            assert await cg.get("/coins/markets", {"page": 1}) == []
    assert cg.calls == 2


async def test_hourly_job_budget_stop_exits_1_markets_job_exits_0(cg_settings):
    import argparse

    from structlog.testing import capture_logs

    from gecko.collectors.coingecko import hourly_job, markets_job
    from gecko.store import Store, utcnow

    cg_settings.coingecko_monthly_cap = 1
    st = await Store.open(cg_settings.db_path)
    await st.record_run("prior", utcnow(), utcnow(), True)
    await st.conn.execute(  # the month is the real clock's: spend it all now
        "UPDATE runs SET job='coingecko-markets', credits_used=1"
    )
    await st.conn.commit()
    await st.close()
    ns = argparse.Namespace(as_of=None)
    with aioresponses() as m:
        with capture_logs() as logs:
            assert await markets_job(cg_settings, ns) == 0
            assert await hourly_job(cg_settings, ns) == 1
        assert m.requests == {}
    stop = [e for e in logs if e["event"] == "cg_budget_exhausted"]
    assert len(stop) == 2 and all(e["log_level"] == "warning" for e in stop)
    st = await Store.open(cg_settings.db_path)
    rows = await (
        await st.conn.execute(
            "SELECT job, ok, error LIKE '%cg budget exhausted%' FROM runs WHERE id>1 "
            "ORDER BY id"
        )
    ).fetchall()
    await st.close()
    assert rows == [("coingecko-markets", 1, None), ("coingecko-hourly", 0, 1)]


async def test_platform_lookups_capped_per_run(store, cg_settings):
    cg_settings.cg_max_lookups_per_run = 1
    cg_settings.cg_max_pages = 1
    with aioresponses() as m:
        m.get(MARKETS, payload=[row("a-coin", 5e6), row("b-coin", 5e6)])
        m.get(COIN, payload={"platforms": {"solana": "AaaMint" + "1" * 30}})
        async with aiohttp.ClientSession() as s:
            r = await markets_sweep(store, cg_settings, s, NOW, sleep=RecordingSleep())
    assert r.credits_used == 2 and r.rows_written == 1
    # the deferred coin is not marked unmapped: it retries next sweep
    n = await (
        await store.conn.execute("SELECT COUNT(*) FROM tokens WHERE chain='cg'")
    ).fetchone()
    assert n == (0,)


async def test_invalid_platform_address_is_skipped_and_counted(store, cg_settings):
    from structlog.testing import capture_logs

    cg_settings.cg_max_pages = 1
    with aioresponses() as m:
        m.get(MARKETS, payload=[row("bad-coin", 5e6)])
        m.get(
            COIN,
            payload={"platforms": {"solana": "not/a/mint", "base": "0x" + "c" * 40}},
        )
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                r = await markets_sweep(
                    store, cg_settings, s, NOW, sleep=RecordingSleep()
                )
    assert r.rows_written == 1
    toks = await (await store.conn.execute("SELECT token_id FROM tokens")).fetchall()
    assert toks == [("base:0x" + "c" * 40,)]
    done = [e for e in logs if e["event"] == "coingecko_markets_done"]
    assert done[0]["invalid_addresses"] == 1


async def test_benchmark_btc_is_never_a_candidate(store, cg_settings):
    with aioresponses() as m:
        m.get(TRENDING, payload={"coins": []})
        m.get(PRICE, payload={"bitcoin": {"usd": 60000}})
        async with aiohttp.ClientSession() as s:
            await hourly(store, cg_settings, s, NOW, sleep=RecordingSleep())
    st = await (
        await store.conn.execute(
            "SELECT status, disqualify_reason FROM tokens WHERE token_id='benchmark:btc'"
        )
    ).fetchone()
    assert st == ("disqualified", "benchmark")
