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
        m.get(COIN, payload={"platforms": {"solana": "So1Addr"}})
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
    assert tok == [("solana:So1Addr", "So1Addr", "abc-coin")]
    obs = await store.observations("solana:So1Addr", as_of=NOW, source="coingecko")
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
        m.get(COIN, payload={"platforms": {"base": " 0xAbCdEf "}})
        m.get(MARKETS, payload=[])
        async with aiohttp.ClientSession() as s:
            await markets_sweep(store, cg_settings, s, NOW, sleep=RecordingSleep())
    tok = await (
        await store.conn.execute(
            "SELECT token_id, address FROM tokens WHERE cg_id='evm-coin'"
        )
    ).fetchall()
    assert tok == [("base:0xabcdef", "0xabcdef")]


async def test_sweep_does_not_refetch_known_platforms(store, cg_settings):
    await store.upsert_token(
        "solana:So1Addr",
        "solana",
        "So1Addr",
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
            r = await markets_sweep(store, cg_settings, s, NOW)
            h = await hourly(store, cg_settings, s, NOW)
    assert r.credits_used == 0 and r.rows_written == 0
    assert h.credits_used == 0 and h.rows_written == 0
    assert m.requests == {}


async def test_platform_lookups_never_overshoot_monthly_cap(store, cg_settings):
    # 3 market pages planned; the cap leaves room for exactly 1 platform lookup.
    cg_settings.coingecko_monthly_cap = 4
    with aioresponses() as m:
        m.get(
            MARKETS,
            payload=[row("a-coin", 5e6), row("b-coin", 5e6), row("c-coin", 5e6)],
        )
        m.get(COIN, payload={"platforms": {"solana": "AaaMint"}})
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
        "solana:So1Addr",
        "solana",
        "So1Addr",
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
    tr = await store.observations("solana:So1Addr", as_of=NOW, source="coingecko")
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
