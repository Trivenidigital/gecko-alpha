from aioresponses import aioresponses
from gecko.freshness import check_freshness, SLO_MINUTES, alert_breaches

TG_URL = "https://api.telegram.org/bottok/sendMessage"


async def test_fresh_table_no_breach(store):
    await store.record_run(
        "dexscreener",
        "2026-10-09T00:00:00Z",
        "2026-10-09T00:00:10Z",
        True,
        rows_written=1,
    )
    await store.upsert_token(
        "solana:a", "solana", "A", "A", "A", "2026-10-09T00:00:00Z", "dexscreener"
    )
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-09T00:00:00Z", {"price": 1}
    )
    assert await check_freshness(store, "2026-10-09T00:20:00Z") == []


async def test_stale_table_breaches(store):
    await store.record_run(
        "dexscreener",
        "2026-10-09T00:00:00Z",
        "2026-10-09T00:00:10Z",
        True,
        rows_written=1,
    )
    await store.upsert_token(
        "solana:a", "solana", "A", "A", "A", "2026-10-09T00:00:00Z", "dexscreener"
    )
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-09T00:00:00Z", {"price": 1}
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
        "dexscreener",
        "2026-10-09T00:00:00Z",
        "2026-10-09T00:00:10Z",
        True,
        rows_written=1,
    )
    await store.upsert_token(
        "solana:a", "solana", "A", "A", "A", "2026-10-09T00:00:00Z", "dexscreener"
    )
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-09T00:00:00Z", {"price": 1}
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
        "dexscreener",
        "2026-10-09T00:00:00Z",
        "2026-10-09T00:00:10Z",
        True,
        rows_written=1,
    )
    await store.upsert_token(
        "solana:a", "solana", "A", "A", "A", "2026-10-09T00:00:00Z", "dexscreener"
    )
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-09T00:00:00Z", {"price": 1}
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
            r1 = await alert_breaches(store, settings, session, "2026-10-09T01:00:00Z")
            assert (r1.sent, r1.failed, r1.skipped) == (0, 1, 0)
            assert await _dedup_rows(store) == 0
            r2 = await alert_breaches(store, settings, session, "2026-10-09T01:05:00Z")
    assert (r2.sent, r2.failed, r2.skipped) == (1, 0, 0)
    assert await _dedup_rows(store) == 1


async def test_failed_send_does_not_stop_other_tables(store, settings):
    import aiohttp

    _with_channel(settings)
    await _seed_stale_observations(store)
    await store.record_run(
        "rank", "2026-10-09T00:00:00Z", "2026-10-09T00:00:10Z", True
    )  # watchlist writer ran, table never written -> breach
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
            r = await alert_breaches(store, settings, session, "2026-10-09T01:00:00Z")
    assert (r.sent, r.failed, r.skipped) == (0, 0, 1)
    assert await _dedup_rows(store) == 0
    assert len(m.requests) == 0


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
    for as_of in ("2026-10-09T01:00:00", "2026-10-09T03:00:00+02:00"):
        rc = await freshness_job(settings, argparse.Namespace(as_of=as_of))
        assert rc == 0
