from aioresponses import aioresponses
from gecko.freshness import check_freshness, SLO_MINUTES, alert_breaches


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
            n1 = await alert_breaches(store, settings, session, "2026-10-09T01:00:00Z")
            n2 = await alert_breaches(store, settings, session, "2026-10-09T02:00:00Z")
            n3 = await alert_breaches(store, settings, session, "2026-10-09T07:30:00Z")
    assert (n1, n2, n3) == (1, 0, 1)
    sent = [c for c in m.requests if c[0] == "POST"]
    assert len(sent) == 1  # one URL key; check call count below
    assert len(m.requests[sent[0]]) == 2
    body = m.requests[sent[0]][0].kwargs["json"]
    assert "parse_mode" not in body and "observations" in body["text"]
