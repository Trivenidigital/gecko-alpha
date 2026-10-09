# Aggregate-observation tests seed the "enrich" writer, which feeds only the
# aggregate check, so per-source SLOs do not add breaches here.
import pytest
from aioresponses import aioresponses
from structlog.testing import capture_logs
from gecko.freshness import check_freshness, SLO_MINUTES, alert_breaches

TG_URL = "https://api.telegram.org/bottok/sendMessage"


async def test_fresh_table_no_breach(store):
    await store.record_run(
        "enrich",
        "2026-10-09T00:00:00Z",
        "2026-10-09T00:00:10Z",
        True,
        rows_written=1,
    )
    await store.upsert_token(
        "solana:a", "solana", "A", "A", "A", "2026-10-09T00:00:00Z", "dexscreener"
    )
    await store.add_observation(
        "solana:a", "enrich", "2026-10-09T00:00:00Z", {"price": 1}
    )
    assert await check_freshness(store, "2026-10-09T00:20:00Z") == []


async def test_stale_table_breaches(store):
    await store.record_run(
        "enrich",
        "2026-10-09T00:00:00Z",
        "2026-10-09T00:00:10Z",
        True,
        rows_written=1,
    )
    await store.upsert_token(
        "solana:a", "solana", "A", "A", "A", "2026-10-09T00:00:00Z", "dexscreener"
    )
    await store.add_observation(
        "solana:a", "enrich", "2026-10-09T00:00:00Z", {"price": 1}
    )
    b = await check_freshness(store, "2026-10-09T01:00:00Z")
    assert [x.table for x in b] == ["observations"]
    assert b[0].slo_minutes == SLO_MINUTES["observations"]


async def test_never_written_table_is_not_breach_while_bootstrapping(store):
    assert await check_freshness(store, "2026-10-09T01:00:00Z") == []


async def test_alert_dedup_6h(store, settings):
    from pydantic import SecretStr

    settings.telegram_bot_token = SecretStr("tok")
    settings.telegram_health_chat_id = "1"
    await store.record_run(
        "enrich",
        "2026-10-09T00:00:00Z",
        "2026-10-09T00:00:10Z",
        True,
        rows_written=1,
    )
    await store.upsert_token(
        "solana:a", "solana", "A", "A", "A", "2026-10-09T00:00:00Z", "dexscreener"
    )
    await store.add_observation(
        "solana:a", "enrich", "2026-10-09T00:00:00Z", {"price": 1}
    )
    import aiohttp

    with aioresponses() as m:
        m.post("https://api.telegram.org/bottok/sendMessage", payload={"ok": True})
        m.post("https://api.telegram.org/bottok/sendMessage", payload={"ok": True})
        async with aiohttp.ClientSession() as session:
            results = [
                await alert_breaches(store, settings, session, t)
                for t in (
                    "2026-10-09T01:00:00Z",
                    "2026-10-09T02:00:00Z",
                    "2026-10-09T07:30:00Z",
                )
            ]
    assert [r.sent for r in results] == [1, 0, 1]
    sent = [c for c in m.requests if c[0] == "POST"]
    assert len(sent) == 1  # one URL key; check call count below
    assert len(m.requests[sent[0]]) == 2
    body = m.requests[sent[0]][0].kwargs["json"]
    assert "parse_mode" not in body and "observations" in body["text"]


async def _seed_stale_observations(store):
    await store.record_run(
        "enrich",
        "2026-10-09T00:00:00Z",
        "2026-10-09T00:00:10Z",
        True,
        rows_written=1,
    )
    await store.upsert_token(
        "solana:a", "solana", "A", "A", "A", "2026-10-09T00:00:00Z", "dexscreener"
    )
    await store.add_observation(
        "solana:a", "enrich", "2026-10-09T00:00:00Z", {"price": 1}
    )


async def _dedup_rows(store):
    rows = await (
        await store.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE job LIKE 'freshness-alert:%'"
        )
    ).fetchone()
    return rows[0]


def _with_channel(settings):
    from pydantic import SecretStr

    settings.telegram_bot_token = SecretStr("tok")
    settings.telegram_health_chat_id = "1"
    return settings


async def test_delivery_failure_records_no_dedup_and_retries(store, settings):
    import aiohttp

    _with_channel(settings)
    await _seed_stale_observations(store)
    with aioresponses() as m:
        m.post(TG_URL, payload={"ok": False})
        m.post(TG_URL, payload={"ok": True})
        async with aiohttp.ClientSession() as session:
            with capture_logs() as logs:
                r1 = await alert_breaches(
                    store, settings, session, "2026-10-09T01:00:00Z"
                )
            assert (r1.sent, r1.failed, r1.skipped) == (0, 1, 0)
            dispatched = [e for e in logs if e["event"] == "freshness_alert_dispatched"]
            delivered = [e for e in logs if e["event"] == "freshness_alert_delivered"]
            assert len(dispatched) == 1 and dispatched[0]["table"] == "observations"
            assert len(delivered) == 1
            assert delivered[0]["table"] == "observations"
            assert delivered[0]["delivered"] is False
            assert await _dedup_rows(store) == 0
            r2 = await alert_breaches(store, settings, session, "2026-10-09T01:05:00Z")
    assert (r2.sent, r2.failed, r2.skipped) == (1, 0, 0)
    assert await _dedup_rows(store) == 1


async def test_failed_send_does_not_stop_other_tables(store, settings):
    import aiohttp

    _with_channel(settings)
    await _seed_stale_observations(store)
    await store.record_run(
        "rank", "2026-10-08T22:00:00Z", "2026-10-08T22:00:10Z", True
    )  # watchlist writer first ran 3h ago, table never written -> breach
    with aioresponses() as m:
        m.post(TG_URL, exception=aiohttp.ClientConnectionError())
        m.post(TG_URL, payload={"ok": True})
        async with aiohttp.ClientSession() as session:
            r = await alert_breaches(store, settings, session, "2026-10-09T01:00:00Z")
    assert (r.sent, r.failed, r.skipped) == (1, 1, 0)
    assert await _dedup_rows(store) == 1


async def test_no_channel_skips_without_dedup_or_post(store, settings):
    import aiohttp

    await _seed_stale_observations(store)
    with aioresponses() as m:
        async with aiohttp.ClientSession() as session:
            with capture_logs() as logs:
                r = await alert_breaches(
                    store, settings, session, "2026-10-09T01:00:00Z"
                )
    assert (r.sent, r.failed, r.skipped) == (0, 0, 1)
    assert await _dedup_rows(store) == 0
    assert len(m.requests) == 0
    skipped = [e for e in logs if e["event"] == "freshness_alert_skipped_no_channel"]
    assert len(skipped) == 1 and skipped[0]["table"] == "observations"


async def test_freshness_job_no_channel_with_breach_exits_1(store, settings):
    import argparse

    from gecko.freshness import freshness_job

    await _seed_stale_observations(store)
    rc = await freshness_job(settings, argparse.Namespace(as_of="2026-10-09T01:00:00Z"))
    assert rc == 1
    row = await (
        await store.conn.execute("SELECT ok, error FROM runs WHERE job='freshness'")
    ).fetchone()
    assert row[0] == 0 and "no alert channel" in row[1]
    assert await _dedup_rows(store) == 0


async def test_freshness_job_delivery_failure_exits_1_and_records_failed_run(
    store, settings
):
    import argparse

    from gecko.freshness import freshness_job

    _with_channel(settings)
    await _seed_stale_observations(store)
    with aioresponses() as m:
        m.post(TG_URL, payload={"ok": False})
        rc = await freshness_job(
            settings, argparse.Namespace(as_of="2026-10-09T01:00:00Z")
        )
    assert rc == 1
    row = await (
        await store.conn.execute("SELECT ok, error FROM runs WHERE job='freshness'")
    ).fetchone()
    assert row[0] == 0 and "delivery failed for 1 table(s)" in row[1]
    assert await _dedup_rows(store) == 0


async def test_freshness_job_success_exits_0(store, settings):
    import argparse

    from gecko.freshness import freshness_job

    _with_channel(settings)
    await _seed_stale_observations(store)
    with aioresponses() as m:
        m.post(TG_URL, payload={"ok": True})
        rc = await freshness_job(
            settings, argparse.Namespace(as_of="2026-10-09T01:00:00Z")
        )
    assert rc == 0
    row = await (
        await store.conn.execute(
            "SELECT ok, rows_written FROM runs WHERE job='freshness'"
        )
    ).fetchone()
    assert row == (1, 1)


async def test_freshness_job_accepts_naive_and_offset_as_of(store, settings):
    import argparse

    from gecko.freshness import freshness_job

    await _seed_stale_observations(store)
    # naive is UTC; 09:00+02:00 is 07:00Z. Both breach observations and must deliver.
    for as_of in ("2026-10-09T01:00:00", "2026-10-09T09:00:00+02:00"):
        with aioresponses() as m:
            m.post(TG_URL, payload={"ok": True})
            _with_channel(settings)
            rc = await freshness_job(settings, argparse.Namespace(as_of=as_of))
        assert rc == 0
        assert len(m.requests) == 1


async def test_check_freshness_naive_now_is_utc(store):
    await _seed_stale_observations(store)
    b = await check_freshness(store, "2026-10-09T01:00:00")
    assert [x.table for x in b] == ["observations"]
    assert await check_freshness(store, "2026-10-09T00:20:00") == []


async def test_empty_table_within_slo_of_first_writer_run_no_breach(store):
    await store.record_run(
        "enrich", "2026-10-09T00:55:00Z", "2026-10-09T00:55:10Z", True
    )
    assert await check_freshness(store, "2026-10-09T01:00:00Z") == []


async def test_empty_table_past_slo_of_first_writer_run_breaches(store):
    await store.record_run(
        "enrich", "2026-10-08T23:00:00Z", "2026-10-08T23:00:10Z", True
    )
    await store.record_run(
        "enrich", "2026-10-09T00:58:00Z", "2026-10-09T00:58:10Z", True
    )  # a recent run must not reset the clock: MIN(finished_at) is the anchor
    b = await check_freshness(store, "2026-10-09T01:00:00Z")
    assert [x.table for x in b] == ["observations"]
    assert b[0].last_write is None


async def test_failed_writer_run_does_not_start_the_clock(store):
    await store.record_run(
        "enrich", "2026-10-08T23:00:00Z", "2026-10-08T23:00:10Z", False
    )
    assert await check_freshness(store, "2026-10-09T01:00:00Z") == []


async def test_rank_ran_recently_with_empty_watchlist_no_breach(store):
    await store.record_run("rank", "2026-10-09T00:00:00Z", "2026-10-09T00:00:05Z", True)
    await store.record_run("rank", "2026-10-09T02:50:00Z", "2026-10-09T02:50:05Z", True)
    # watchlist has never had a row; the writer is alive
    assert await check_freshness(store, "2026-10-09T03:00:00Z") == []


async def test_rank_last_ran_3h_ago_breaches_despite_fresh_rows(store):
    await store.record_run("rank", "2026-10-09T00:00:00Z", "2026-10-09T00:00:05Z", True)
    await store.watchlist_upsert("solana:a", "2026-10-09T02:59:00Z", "F4", 1, 1.0, {})
    # a later failed run does not count as activity
    await store.record_run(
        "rank", "2026-10-09T02:00:00Z", "2026-10-09T02:00:05Z", False
    )
    b = await check_freshness(store, "2026-10-09T03:00:05Z")
    assert [x.table for x in b] == ["watchlist"]
    assert b[0].last_write == "2026-10-09T00:00:05Z"


async def test_ledger_with_nothing_due_does_not_page(store):
    await store.record_run(
        "ledger", "2026-10-08T00:00:00Z", "2026-10-08T00:00:05Z", True
    )
    await store.record_run(
        "ledger", "2026-10-09T02:00:00Z", "2026-10-09T02:00:05Z", True
    )
    assert await check_freshness(store, "2026-10-09T03:00:00Z") == []


PER_SOURCE = [
    # key, writer job, token, source, features
    ("observations:dexscreener", "dexscreener", "solana:a", "dexscreener", {"p": 1}),
    ("observations:geckoterminal", "geckoterminal", "solana:a", "geckoterminal", {}),
    ("observations:coingecko", "coingecko-markets", "solana:a", "coingecko", {}),
    (
        "observations:geckoterminal_ohlcv",
        "geckoterminal-ohlcv",
        "solana:a",
        "geckoterminal_ohlcv",
        {"candles": [[1, 1, 1, 1, 1, 1]]},
    ),
    (
        "benchmark:btc",
        "coingecko-hourly",
        "benchmark:btc",
        "coingecko",
        {"price_usd": 1},
    ),
]
T_OBS = "2026-10-09T00:00:00Z"


def _plus(minutes: float) -> str:
    import datetime as dt

    t = dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("key,job,tid,source,features", PER_SOURCE)
async def test_per_source_slo(store, key, job, tid, source, features):
    slo = SLO_MINUTES[key]
    # stale before the writer ever succeeded: bootstrapping, no breach
    await store.add_observation(tid, source, T_OBS, features)
    assert key not in [b.table for b in await check_freshness(store, _plus(slo + 5))]
    await store.record_run(job, T_OBS, T_OBS, True)
    assert key not in [b.table for b in await check_freshness(store, _plus(slo - 1))]
    breached = [
        b for b in await check_freshness(store, _plus(slo + 1)) if b.table == key
    ]
    assert len(breached) == 1 and breached[0].last_write == T_OBS


async def test_ohlcv_status_markers_do_not_count_as_fresh(store):
    await store.record_run("geckoterminal-ohlcv", T_OBS, T_OBS, True)
    await store.add_observation(
        "solana:a", "geckoterminal_ohlcv", T_OBS, {"candles": [[1, 1, 1, 1, 1, 1]]}
    )
    await store.add_observation(
        "solana:a",
        "geckoterminal_ohlcv",
        _plus(1500),
        {"candles": [], "status": "error"},
    )
    b = [x for x in await check_freshness(store, _plus(1600)) if "ohlcv" in x.table]
    assert [(x.table, x.last_write) for x in b] == [
        ("observations:geckoterminal_ohlcv", T_OBS)
    ]


async def test_one_live_source_does_not_hide_a_dead_one(store):
    await store.record_run("dexscreener", T_OBS, T_OBS, True)
    await store.record_run("geckoterminal", T_OBS, T_OBS, True)
    await store.add_observation("solana:a", "geckoterminal", T_OBS, {})
    await store.add_observation("solana:a", "dexscreener", _plus(375), {})
    tables = [b.table for b in await check_freshness(store, _plus(380))]
    assert "observations" not in tables
    assert "observations:geckoterminal" in tables


async def test_forward_returns_backlog(store):
    await store.schedule_returns("solana:a", "2026-10-01T00:00:00Z", horizons=(1,))
    due = "2026-10-02T00:00:00Z"
    ok = [b.table for b in await check_freshness(store, "2026-10-04T23:00:00Z")]
    assert "forward_returns_backlog" not in ok  # 71 h overdue
    b = [
        x
        for x in await check_freshness(store, "2026-10-05T01:00:00Z")
        if x.table == "forward_returns_backlog"
    ]
    assert len(b) == 1 and b[0].last_write == due  # 73 h overdue
    await store.set_return("solana:a", 1, 1.0, 1.0, 1.0, 1.0, "2026-10-05T02:00:00Z")
    tables = [x.table for x in await check_freshness(store, "2026-10-05T03:00:00Z")]
    assert "forward_returns_backlog" not in tables


async def test_per_source_check_bootstraps_on_its_own_writer_only(store):
    # dexscreener has run; geckoterminal never has, so its stale row is not a breach
    await store.record_run("dexscreener", T_OBS, T_OBS, True)
    await store.add_observation("solana:a", "dexscreener", _plus(100), {})
    await store.add_observation("solana:a", "geckoterminal", T_OBS, {})
    tables = [b.table for b in await check_freshness(store, _plus(110))]
    assert "observations:geckoterminal" not in tables


async def test_benchmark_check_reads_only_the_benchmark(store):
    await store.record_run("coingecko-hourly", T_OBS, T_OBS, True)
    await store.add_observation("benchmark:btc", "coingecko", T_OBS, {"price_usd": 1})
    await store.add_observation("solana:a", "coingecko", _plus(200), {"mcap_usd": 1})
    b = [
        x
        for x in await check_freshness(store, _plus(200))
        if x.table == "benchmark:btc"
    ]
    assert len(b) == 1 and b[0].last_write == T_OBS


@pytest.mark.parametrize("with_btc", [True, False])
async def test_coingecko_slo_counts_only_markets_rows(store, with_btc):
    await store.record_run("coingecko-markets", T_OBS, T_OBS, True)
    await store.add_observation("solana:a", "coingecko", T_OBS, {"mcap_usd": 1e6})
    later = _plus(int(3.5 * 1440))
    await store.add_observation("solana:a", "coingecko", later, {"trending_rank": 1})
    if with_btc:  # the hourly job keeps writing: it must not mask a dead sweep
        await store.add_observation(
            "benchmark:btc", "coingecko", later, {"price_usd": 60000.0}
        )
    b = [
        x
        for x in await check_freshness(store, later)
        if x.table == "observations:coingecko"
    ]
    assert len(b) == 1 and b[0].last_write == T_OBS


async def test_hourly_run_does_not_bootstrap_the_markets_slo(store):
    await store.record_run("coingecko-hourly", T_OBS, T_OBS, True)
    await store.add_observation("solana:a", "coingecko", T_OBS, {"mcap_usd": 1e6})
    tables = [b.table for b in await check_freshness(store, _plus(3000))]
    assert "observations:coingecko" not in tables


@pytest.mark.parametrize("row_age_h,breach", [(7, True), (5, False)])
async def test_geckoterminal_ok_runs_writing_nothing_still_page(
    store, row_age_h, breach
):
    # GT runs every 15 min and succeeds, but (say) a renamed reserve field puts
    # every pool out of band: no rows. Only the newest row's age can tell.
    now = _plus(8 * 60)
    for m in range(0, 8 * 60 + 1, 15):
        await store.record_run("geckoterminal", _plus(m), _plus(m), True)
    await store.add_observation(
        "solana:a", "geckoterminal", _plus(8 * 60 - row_age_h * 60), {}
    )
    tables = [x.table for x in await check_freshness(store, now)]
    assert ("observations:geckoterminal" in tables) is breach
