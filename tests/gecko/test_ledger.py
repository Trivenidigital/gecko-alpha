import argparse

import aiohttp
import pytest
from aioresponses import aioresponses
from structlog.testing import capture_logs

import gecko.ledger
from gecko.http import UpstreamError

from gecko.ledger import (
    MAX_ATTEMPTS,
    btc_price_at,
    entry_price,
    fetch_price,
    measure_due,
)

E = "2026-10-01T00:00:00Z"
DS = "https://api.dexscreener.com/tokens/v1/solana/abc"
NOW = "2026-10-02T02:00:00Z"


async def seed(store, *, btc=True, token_obs=True):
    await store.upsert_token(
        "solana:abc",
        "solana",
        "abc",
        "ABC",
        "Abc",
        "2026-09-20T00:00:00Z",
        "dexscreener",
    )
    await store.set_token_status("solana:abc", "active")
    if token_obs:
        await store.add_observation(
            "solana:abc", "dexscreener", "2026-09-30T23:00:00Z", {"price_usd": 0.5}
        )
        await store.add_observation(
            "solana:abc", "dexscreener", "2026-10-01T00:30:00Z", {"price_usd": 1.0}
        )
        await store.add_observation(
            "solana:abc", "dexscreener", "2026-10-01T05:00:00Z", {"price_usd": 4.0}
        )
    await store.upsert_token(
        "benchmark:btc", "benchmark", "btc", "BTC", "Bitcoin", E, "coingecko"
    )
    if btc:
        await store.add_observation(
            "benchmark:btc", "coingecko", "2026-10-01T01:00:00Z", {"price_usd": 60000.0}
        )
        await store.add_observation(
            "benchmark:btc", "coingecko", "2026-10-02T01:00:00Z", {"price_usd": 66000.0}
        )
    await store.watchlist_upsert("solana:abc", E, "F4", 1, 1.0, {})
    await store.schedule_returns("solana:abc", E)


async def one(store, sql):
    return await (await store.conn.execute(sql)).fetchone()


async def test_entry_price_is_first_post_entry(store):
    await seed(store)
    assert await entry_price(store, "solana:abc", E) == 1.0


async def test_entry_price_first_across_sources_not_insert_order(store):
    await seed(store)
    # inserted later, but observed earlier than the 00:30 dexscreener row
    await store.add_observation(
        "solana:abc", "geckoterminal", "2026-10-01T00:10:00Z", {"price_usd": 0.9}
    )
    assert await entry_price(store, "solana:abc", E) == 0.9


async def test_entry_price_ignores_non_market_sources(store):
    await seed(store)
    await store.add_observation(
        "solana:abc", "enrich", "2026-10-01T00:05:00Z", {"price_usd": 7.0}
    )
    assert await entry_price(store, "solana:abc", E) == 1.0


async def test_entry_price_bounded_by_as_of(store):
    await seed(store)
    assert (
        await entry_price(store, "solana:abc", E, as_of="2026-10-01T00:20:00Z") is None
    )
    assert (
        await entry_price(store, "solana:abc", E, as_of="2026-10-01T00:30:00Z") == 1.0
    )


async def test_btc_nearest_within_window(store):
    await seed(store)
    assert await btc_price_at(store, "2026-10-01T00:00:00Z") == 60000.0
    assert await btc_price_at(store, "2026-10-05T00:00:00Z") is None


async def test_btc_nearest_either_side(store):
    await seed(store)
    with capture_logs() as logs:
        assert await btc_price_at(store, "2026-10-01T20:00:00Z") == 66000.0
        assert await btc_price_at(store, "2026-10-01T06:00:00+00:00") == 60000.0
    # inside the primary window: no fallback logged
    assert not [e for e in logs if e["event"] == "ledger_btc_fallback_window"]


async def test_btc_fallback_window_36h(store):
    await seed(store)
    # nearest obs (10-01 01:00) is 20h away; the 10-02 obs is 44h away
    with capture_logs() as logs:
        assert await btc_price_at(store, "2026-09-30T05:00:00Z") == 60000.0
    fb = [e for e in logs if e["event"] == "ledger_btc_fallback_window"]
    assert len(fb) == 1
    assert fb[0]["ts"] == "2026-09-30T05:00:00Z" and fb[0]["distance_hours"] == 20.0
    assert fb[0]["log_level"] == "warning"
    # fallback still picks the nearest: 10-01 14:00 is 13h / 11h away
    assert await btc_price_at(store, "2026-10-01T14:00:00Z") == 66000.0
    # beyond 36h on both sides -> None
    assert await btc_price_at(store, "2026-09-29T12:00:00Z") is None


async def test_fetch_price_picks_deepest_pair(settings):
    with aioresponses() as m:
        m.get(
            DS,
            payload=[
                {"priceUsd": "1.5", "liquidity": {"usd": 1000}},
                {"priceUsd": "1.9", "liquidity": {"usd": 90000}},
                {"priceUsd": None, "liquidity": {"usd": 999999}},
                {"priceUsd": "abc", "liquidity": {"usd": 999998}},
                {"priceUsd": "0", "liquidity": {"usd": 999997}},
                {"priceUsd": "-3", "liquidity": {"usd": 999996}},
                {"priceUsd": "inf", "liquidity": {"usd": 999995}},
            ],
        )
        async with aiohttp.ClientSession() as s:
            assert await fetch_price(s, settings, "solana", "abc") == 1.9


async def test_fetch_price_none_on_404(settings):
    with aioresponses() as m:
        m.get(DS, status=404)
        async with aiohttp.ClientSession() as s:
            assert await fetch_price(s, settings, "solana", "abc") is None


async def test_fetch_price_none_on_empty_list(settings):
    with aioresponses() as m:
        m.get(DS, payload=[])
        async with aiohttp.ClientSession() as s:
            assert await fetch_price(s, settings, "solana", "abc") is None


async def test_measure_due_writes_return_vs_btc(store, settings):
    await seed(store)
    with aioresponses() as m:
        m.get(DS, payload=[{"priceUsd": "2.0", "liquidity": {"usd": 5}}])
        async with aiohttp.ClientSession() as s:
            r = await measure_due(store, settings, s, NOW)
    assert (r.measured, r.failed, r.total_loss, r.skipped) == (1, 0, 0, 0)
    row = await one(
        store,
        "SELECT entry_price, price, btc_entry, btc_price, ret, ret_vs_btc, measured_at "
        "FROM forward_returns WHERE horizon_days=1",
    )
    assert row[:4] == (1.0, 2.0, 60000.0, 66000.0)
    assert row[4] == pytest.approx(1.0) and row[5] == pytest.approx(0.9)
    assert row[6] == NOW
    # only the due horizon was touched
    assert (
        await one(
            store, "SELECT COUNT(*) FROM forward_returns WHERE measured_at IS NOT NULL"
        )
    ) == (1,)


async def test_missing_price_counts_as_total_loss(store, settings):
    await seed(store)
    with aioresponses() as m:
        for _ in range(MAX_ATTEMPTS):
            m.get(DS, status=404)
        async with aiohttp.ClientSession() as s:
            reports = [
                await measure_due(store, settings, s, NOW) for _ in range(MAX_ATTEMPTS)
            ]
    assert [r.failed for r in reports] == [1, 1, 0] and reports[-1].total_loss == 1
    row = await one(
        store,
        "SELECT ret, price, entry_price, btc_entry, btc_price, attempts "
        "FROM forward_returns WHERE horizon_days=1",
    )
    assert row == (-1.0, 0.0, 1.0, 60000.0, 66000.0, MAX_ATTEMPTS)
    st = await one(
        store,
        "SELECT status, disqualify_reason FROM tokens WHERE token_id='solana:abc'",
    )
    assert st == ("dead", "no_price")


async def test_btc_gap_skips_row_without_bumping(store, settings):
    await seed(store, btc=False)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(DS, payload=[{"priceUsd": "2.0", "liquidity": {"usd": 5}}])
            r = await measure_due(store, settings, s, NOW)
            assert not m.requests  # no fetch is spent on a BTC gap
    assert (r.measured, r.failed, r.total_loss, r.skipped) == (0, 0, 0, 1)
    assert [d.horizon_days for d in await store.due_returns(NOW)] == [1]
    assert (
        await one(store, "SELECT attempts FROM forward_returns WHERE horizon_days=1")
    ) == (0,)
    st = await one(store, "SELECT status FROM tokens WHERE token_id='solana:abc'")
    assert st == ("active",)


LATER = "2026-10-08T01:00:00Z"  # h=1 and h=7 both due


async def test_btc_gap_at_entry_only_skips_without_bumping(store, settings):
    await seed(store, btc=False)
    # BTC at now present; nothing within 36h of entry
    await store.add_observation(
        "benchmark:btc", "coingecko", "2026-10-08T00:30:00Z", {"price_usd": 70000.0}
    )
    assert await btc_price_at(store, LATER) == 70000.0
    assert await btc_price_at(store, E) is None
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(
                DS, payload=[{"priceUsd": "2.0", "liquidity": {"usd": 5}}], repeat=True
            )
            r = await measure_due(store, settings, s, LATER)
            assert not m.requests
    assert (r.measured, r.failed, r.total_loss, r.skipped) == (0, 0, 0, 2)
    rows = await (
        await store.conn.execute(
            "SELECT horizon_days, attempts, measured_at FROM forward_returns "
            "WHERE horizon_days IN (1, 7) ORDER BY horizon_days"
        )
    ).fetchall()
    assert rows == [(1, 0, None), (7, 0, None)]


async def test_btc_gap_at_now_only_skips_without_bumping(store, settings):
    await seed(store, btc=False)
    await store.add_observation(
        "benchmark:btc", "coingecko", "2026-10-01T01:00:00Z", {"price_usd": 60000.0}
    )
    assert await btc_price_at(store, E) == 60000.0
    assert await btc_price_at(store, LATER) is None
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(
                DS, payload=[{"priceUsd": "2.0", "liquidity": {"usd": 5}}], repeat=True
            )
            r = await measure_due(store, settings, s, LATER)
            assert not m.requests
    assert (r.measured, r.failed, r.skipped) == (0, 0, 2)
    assert (await one(store, "SELECT SUM(attempts) FROM forward_returns")) == (0,)


async def test_set_return_input_error_is_not_swallowed(store, settings, monkeypatch):
    await seed(store)

    async def bad(*a, **k):
        raise ValueError("price/btc_entry/btc_price required")

    monkeypatch.setattr(store, "set_return", bad)
    with aioresponses() as m:
        m.get(DS, payload=[{"priceUsd": "2.0", "liquidity": {"usd": 5}}])
        async with aiohttp.ClientSession() as s:
            with pytest.raises(ValueError, match="required"):
                await measure_due(store, settings, s, NOW)


async def test_missing_entry_price_waits_then_finalizes_all_horizons(store, settings):
    await seed(store, token_obs=False)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            r = await measure_due(store, settings, s, NOW)  # E+26h: still pending
            assert not m.requests
        assert (r.skipped, r.failed, r.unmeasurable) == (1, 0, 0)
        late = "2026-10-03T01:00:00Z"  # E+49h: the 48h window has closed
        with aioresponses() as m:
            r = await measure_due(store, settings, s, late)
            assert not m.requests
    assert (r.unmeasurable, r.total_loss, r.measured) == (5, 0, 0)
    rows = await (
        await store.conn.execute(
            "SELECT entry_price, price, ret, ret_vs_btc, measured_at, attempts "
            "FROM forward_returns ORDER BY horizon_days"
        )
    ).fetchall()
    assert rows == [(None, 0.0, None, None, late, 0)] * 5
    assert await one(
        store,
        "SELECT exit_reason, entry_price FROM watchlist WHERE token_id='solana:abc'",
    ) == ("no_entry_price", None)
    # an absent entry price is no evidence the token is dead
    st = await one(store, "SELECT status FROM tokens WHERE token_id='solana:abc'")
    assert st == ("active",)


async def test_entry_price_appearing_later_is_used(store, settings):
    await seed(store, token_obs=False)
    with aioresponses() as m:
        m.get(DS, payload=[{"priceUsd": "3.0", "liquidity": {"usd": 5}}])
        async with aiohttp.ClientSession() as s:
            r1 = await measure_due(store, settings, s, NOW)
            await store.add_observation(
                "solana:abc", "dexscreener", "2026-10-01T00:30:00Z", {"price_usd": 1.5}
            )
            r2 = await measure_due(store, settings, s, NOW)
    assert (r1.skipped, r2.measured) == (1, 1)
    row = await one(
        store, "SELECT entry_price, price FROM forward_returns WHERE horizon_days=1"
    )
    assert row == (1.5, 3.0)


async def test_all_horizons_share_one_frozen_entry_price(store, settings):
    await seed(store)
    await store.add_observation(
        "benchmark:btc", "coingecko", "2026-10-08T00:30:00Z", {"price_usd": 70000.0}
    )
    with aioresponses() as m:
        m.get(DS, payload=[{"priceUsd": "2.0", "liquidity": {"usd": 5}}], repeat=True)
        async with aiohttp.ClientSession() as s:
            await measure_due(store, settings, s, NOW)
            # an earlier-observed row arrives late; the frozen price must hold
            await store.add_observation(
                "solana:abc",
                "geckoterminal",
                "2026-10-01T00:10:00Z",
                {"price_usd": 0.9},
            )
            r = await measure_due(store, settings, s, LATER)
    assert r.measured == 1
    rows = await (
        await store.conn.execute(
            "SELECT horizon_days, entry_price FROM forward_returns "
            "WHERE measured_at IS NOT NULL ORDER BY horizon_days"
        )
    ).fetchall()
    assert rows == [(1, 1.0), (7, 1.0)]
    assert await one(
        store, "SELECT entry_price FROM watchlist WHERE token_id='solana:abc'"
    ) == (1.0,)


async def test_entry_price_after_the_48h_window_is_not_used(store, settings):
    await seed(store, token_obs=False)
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-10-03T02:00:00Z", {"price_usd": 1.0}
    )  # E+50h
    await store.add_observation(
        "benchmark:btc", "coingecko", "2026-10-03T12:00:00Z", {"price_usd": 61000.0}
    )
    async with aiohttp.ClientSession() as s:
        with aioresponses():
            r = await measure_due(store, settings, s, "2026-10-03T12:00:00Z")
    assert r.unmeasurable == 5 and r.measured == 0


async def test_already_measured_row_is_logged_and_skipped(store, settings, monkeypatch):
    await seed(store)
    real = store.set_return

    async def racing(*a, **k):
        await real(*a, **k)  # a concurrent run finalized it first
        await real(*a, **k)

    monkeypatch.setattr(store, "set_return", racing)
    with aioresponses() as m:
        m.get(DS, payload=[{"priceUsd": "2.0", "liquidity": {"usd": 5}}])
        async with aiohttp.ClientSession() as s:
            r = await measure_due(store, settings, s, NOW)
    assert (r.measured, r.failed, r.total_loss) == (0, 0, 0)
    assert r.skipped == 1


async def test_definite_4xx_counts_as_missing_price(store, settings):
    await seed(store)
    with aioresponses() as m:
        for _ in range(MAX_ATTEMPTS):
            m.get(DS, status=410)
        async with aiohttp.ClientSession() as s:
            r = await measure_due(store, settings, s, NOW)
            assert (r.measured, r.failed, r.skipped) == (0, 1, 0)
            assert (
                await one(
                    store, "SELECT attempts FROM forward_returns WHERE horizon_days=1"
                )
            ) == (1,)
            for _ in range(MAX_ATTEMPTS - 1):
                r = await measure_due(store, settings, s, NOW)
    assert r.total_loss == 1
    row = await one(
        store, "SELECT ret, price FROM forward_returns WHERE horizon_days=1"
    )
    assert row == (-1.0, 0.0)


@pytest.mark.parametrize("status", [0, 403, 429, 500, 503])
async def test_transient_upstream_error_skips_without_bumping(
    store, settings, monkeypatch, status
):
    await seed(store)

    async def boom(session, url, **kw):
        raise UpstreamError(status, url)

    monkeypatch.setattr(gecko.ledger, "get_json", boom)
    async with aiohttp.ClientSession() as s:
        with capture_logs() as logs:
            r = await measure_due(store, settings, s, NOW)
    assert (r.measured, r.failed, r.total_loss, r.skipped) == (0, 0, 0, 1)
    assert (
        await one(store, "SELECT attempts FROM forward_returns WHERE horizon_days=1")
    ) == (0,)
    assert [e["status"] for e in logs if e["event"] == "ledger_fetch_transient"] == [
        status
    ]


async def test_fetch_price_none_on_non_json_body(settings):
    with aioresponses() as m:
        m.get(DS, status=200, body="<html>")
        async with aiohttp.ClientSession() as s:
            with capture_logs() as logs:
                assert await fetch_price(s, settings, "solana", "abc") is None
    assert [e["event"] for e in logs if e["event"] == "ledger_price_body_invalid"] == [
        "ledger_price_body_invalid"
    ]


async def test_non_json_body_spends_attempt_and_later_rows_proceed(store, settings):
    await seed(store)
    await store.upsert_token(
        "solana:def",
        "solana",
        "def",
        "DEF",
        "Def",
        "2026-09-20T00:00:00Z",
        "dexscreener",
    )
    await store.add_observation(
        "solana:def", "dexscreener", "2026-10-01T00:30:00Z", {"price_usd": 1.0}
    )
    await store.watchlist_upsert("solana:def", E, "F4", 2, 0.5, {})
    await store.schedule_returns("solana:def", E)
    with aioresponses() as m:
        m.get(DS, status=200, body="<html>")
        m.get(
            "https://api.dexscreener.com/tokens/v1/solana/def",
            payload=[{"priceUsd": "2.0", "liquidity": {"usd": 5}}],
        )
        async with aiohttp.ClientSession() as s:
            r = await measure_due(store, settings, s, NOW)
    assert (r.measured, r.failed, r.skipped) == (1, 1, 0)
    rows = await (
        await store.conn.execute(
            "SELECT token_id, attempts, measured_at FROM forward_returns "
            "WHERE horizon_days=1 ORDER BY token_id"
        )
    ).fetchall()
    assert rows == [("solana:abc", 1, None), ("solana:def", 0, NOW)]


async def test_ledger_job_registered_and_records(settings):
    from gecko.__main__ import _load_jobs
    from gecko.jobs import JOBS
    from gecko.store import Store

    _load_jobs()
    assert "ledger" in JOBS
    s = await Store.open(settings.db_path)
    try:
        await seed(s)
    finally:
        await s.close()
    with aioresponses() as m:
        m.get(DS, payload=[{"priceUsd": "2.0", "liquidity": {"usd": 5}}])
        rc = await JOBS["ledger"](
            settings, argparse.Namespace(as_of="2026-10-02T02:00:00+00:00")
        )
    assert rc == 0
    s = await Store.open(settings.db_path)
    try:
        assert await one(s, "SELECT job, ok, rows_written FROM runs") == (
            "ledger",
            1,
            1,
        )
        assert await one(
            s, "SELECT measured_at FROM forward_returns WHERE horizon_days=1"
        ) == (NOW,)
    finally:
        await s.close()


async def test_btc_picks_nearest_of_several(store):
    await seed(store)
    await store.add_observation(
        "benchmark:btc", "coingecko", "2026-10-01T04:00:00Z", {"price_usd": 61000.0}
    )
    assert await btc_price_at(store, "2026-10-01T03:00:00Z") == 61000.0
    assert await btc_price_at(store, "2026-10-01T02:00:00Z") == 60000.0


async def test_entry_price_skips_non_positive_prices(store):
    await seed(store)
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-10-01T00:10:00Z", {"price_usd": 0}
    )
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-10-01T00:20:00Z", {"price_usd": -2.0}
    )
    assert await entry_price(store, "solana:abc", E) == 1.0


async def test_measure_due_ignores_entry_observations_after_now(store, settings):
    await seed(store, token_obs=False)
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-10-02T03:00:00Z", {"price_usd": 1.0}
    )
    with aioresponses() as m:
        async with aiohttp.ClientSession() as s:
            r = await measure_due(store, settings, s, NOW)
        assert not m.requests
    assert (r.measured, r.failed, r.skipped) == (0, 0, 1)
