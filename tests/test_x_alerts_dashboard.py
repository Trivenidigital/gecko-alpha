"""Dashboard API tests for Hermes/xurl narrative alerts."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.api import create_app
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    d = Database(db_path)
    await d.initialize()
    yield d, str(db_path)
    await d.close()


@pytest.fixture
async def client(db):
    import dashboard.api as api_mod

    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None
    _d, db_path = db
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


async def _insert_x_alert(
    conn,
    *,
    event_id: str,
    tweet_author: str,
    tweet_id: str,
    received_at: str,
    extracted_cashtag: str | None = None,
    extracted_ca: str | None = None,
    extracted_chain: str | None = None,
    resolved_coin_id: str | None = None,
    classifier_confidence: float | None = None,
):
    await conn.execute(
        """INSERT INTO narrative_alerts_inbound (
               event_id, tweet_id, tweet_author, tweet_ts, tweet_text,
               tweet_text_hash, extracted_cashtag, extracted_ca, extracted_chain,
               resolved_coin_id, narrative_theme, urgency_signal,
               classifier_confidence, classifier_version, received_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            tweet_id,
            tweet_author,
            received_at,
            f"{tweet_author} called {extracted_cashtag or extracted_ca}",
            f"hash-{event_id}",
            extracted_cashtag,
            extracted_ca,
            extracted_chain,
            resolved_coin_id,
            "meme rotation",
            "high",
            classifier_confidence,
            "narrative_classifier-v1.1",
            received_at,
        ),
    )
    await conn.commit()


async def _insert_price_cache(
    conn,
    *,
    coin_id: str,
    current_price: float,
    updated_at: str,
):
    await conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d,
            market_cap, updated_at)
           VALUES (?, ?, NULL, NULL, NULL, ?)""",
        (coin_id, current_price, updated_at),
    )
    await conn.commit()


async def _insert_gainers_snapshot(
    conn,
    *,
    coin_id: str,
    symbol: str,
    price_at_snapshot: float,
    snapshot_at: str,
):
    await conn.execute(
        """INSERT INTO gainers_snapshots (
               coin_id, symbol, name, price_change_24h, market_cap,
               volume_24h, price_at_snapshot, snapshot_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            coin_id,
            symbol,
            f"{symbol.title()} Token",
            10.0,
            1_000_000.0,
            250_000.0,
            price_at_snapshot,
            snapshot_at,
        ),
    )
    await conn.commit()


async def test_x_alerts_endpoint_returns_latest_rows_and_rollup(client, db):
    c = client
    d, _db_path = db
    now = datetime.now(timezone.utc)
    newest = now.isoformat()
    older = (now - timedelta(hours=2)).isoformat()
    outside_24h = (now - timedelta(days=2)).isoformat()

    await _insert_x_alert(
        d._conn,
        event_id="evt-old",
        tweet_author="trade",
        tweet_id="111",
        received_at=older,
        extracted_cashtag="$GOBLIN",
        classifier_confidence=0.74,
    )
    await _insert_x_alert(
        d._conn,
        event_id="evt-new",
        tweet_author="_Shadow36",
        tweet_id="222",
        received_at=newest,
        extracted_ca="0xABCDEFabcdefABCDEFabcdefABCDEFabcdefABCD",
        extracted_chain="base",
        resolved_coin_id="goblin-base",
        classifier_confidence=0.91,
    )
    await _insert_x_alert(
        d._conn,
        event_id="evt-outside",
        tweet_author="trade",
        tweet_id="333",
        received_at=outside_24h,
        extracted_cashtag="$OLD",
        classifier_confidence=0.40,
    )

    resp = await c.get("/api/x_alerts?limit=2")

    assert resp.status_code == 200
    body = resp.json()
    assert body["stats_24h"] == {
        "alerts": 2,
        "unique_authors": 2,
        "with_ca": 1,
        "with_cashtag": 1,
        "resolved": 1,
        "avg_confidence": 0.825,
    }
    assert [row["event_id"] for row in body["alerts"]] == ["evt-new", "evt-old"]
    assert body["alerts"][0]["tweet_url"] == "https://x.com/_Shadow36/status/222"
    assert (
        body["alerts"][0]["asset_url"]
        == "https://dexscreener.com/base/0xABCDEFabcdefABCDEFabcdefABCDEFabcdefABCD"
    )
    assert body["alerts"][0]["asset_url_source"] == "dexscreener_contract"
    assert body["alerts"][0]["text_preview"].startswith("_Shadow36 called")
    assert body["alerts"][0]["classifier_version"] == "narrative_classifier-v1.1"


async def test_x_alerts_endpoint_adds_outcome_for_resolved_coin(client, db):
    c = client
    d, _db_path = db
    alert_time = datetime.now(timezone.utc) - timedelta(hours=1)

    await _insert_x_alert(
        d._conn,
        event_id="evt-valued",
        tweet_author="trade",
        tweet_id="444",
        received_at=alert_time.isoformat(),
        extracted_cashtag="$GOBLIN",
        resolved_coin_id="goblin",
        classifier_confidence=0.88,
    )
    await _insert_gainers_snapshot(
        d._conn,
        coin_id="goblin",
        symbol="GOBLIN",
        price_at_snapshot=1.00,
        snapshot_at=(alert_time - timedelta(minutes=5)).isoformat(),
    )
    await _insert_price_cache(
        d._conn,
        coin_id="goblin",
        current_price=1.50,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )

    resp = await c.get("/api/x_alerts?limit=1")

    assert resp.status_code == 200
    alert = resp.json()["alerts"][0]
    assert alert["outcome_investment_usd"] == 300.0
    assert alert["outcome_coin_id"] == "goblin"
    assert alert["asset_url"] == "https://www.coingecko.com/en/coins/goblin"
    assert alert["asset_url_source"] == "coingecko_resolved"
    assert alert["entry_price_usd"] == 1.0
    assert alert["current_price_usd"] == 1.5
    assert alert["gain_pct_since_alert"] == 50.0
    assert alert["profit_usd_at_300"] == 150.0
    assert alert["outcome_status"] == "priced"


async def test_x_alerts_endpoint_values_unique_cashtag_match(client, db):
    c = client
    d, _db_path = db
    alert_time = datetime.now(timezone.utc) - timedelta(hours=1)

    await _insert_x_alert(
        d._conn,
        event_id="evt-cashtag",
        tweet_author="_Shadow36",
        tweet_id="555",
        received_at=alert_time.isoformat(),
        extracted_cashtag="$TROLL",
        classifier_confidence=0.90,
    )
    await _insert_gainers_snapshot(
        d._conn,
        coin_id="troll",
        symbol="TROLL",
        price_at_snapshot=0.10,
        snapshot_at=(alert_time + timedelta(minutes=4)).isoformat(),
    )
    await _insert_price_cache(
        d._conn,
        coin_id="troll",
        current_price=0.08,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )

    resp = await c.get("/api/x_alerts?limit=1")

    assert resp.status_code == 200
    alert = resp.json()["alerts"][0]
    assert alert["outcome_coin_id"] == "troll"
    assert alert["asset_url"] == "https://www.coingecko.com/en/coins/troll"
    assert alert["asset_url_source"] == "coingecko_resolved"
    assert alert["entry_price_usd"] == 0.10
    assert alert["current_price_usd"] == 0.08
    assert alert["gain_pct_since_alert"] == -20.0
    assert alert["profit_usd_at_300"] == -60.0
    assert alert["outcome_status"] == "priced"


async def test_x_alerts_endpoint_leaves_ambiguous_cashtag_unpriced(client, db):
    c = client
    d, _db_path = db
    alert_time = datetime.now(timezone.utc) - timedelta(hours=1)

    await _insert_x_alert(
        d._conn,
        event_id="evt-ambiguous",
        tweet_author="trade",
        tweet_id="666",
        received_at=alert_time.isoformat(),
        extracted_cashtag="$CAT",
        classifier_confidence=0.77,
    )
    for coin_id in ("cat-token", "cat-in-a-dogs-world"):
        await _insert_gainers_snapshot(
            d._conn,
            coin_id=coin_id,
            symbol="CAT",
            price_at_snapshot=1.0,
            snapshot_at=alert_time.isoformat(),
        )
        await _insert_price_cache(
            d._conn,
            coin_id=coin_id,
            current_price=2.0,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    resp = await c.get("/api/x_alerts?limit=1")

    assert resp.status_code == 200
    alert = resp.json()["alerts"][0]
    assert alert["outcome_status"] == "ambiguous_symbol"
    assert alert["asset_url"] == "https://www.coingecko.com/en/search?query=CAT"
    assert alert["asset_url_source"] == "ambiguous_symbol"
    assert alert["outcome_coin_id"] is None
    assert alert["entry_price_usd"] is None
    assert alert["current_price_usd"] is None
    assert alert["gain_pct_since_alert"] is None
    assert alert["profit_usd_at_300"] is None


async def test_x_alerts_endpoint_links_unresolved_cashtag_to_coingecko_search(client, db):
    c = client
    d, _db_path = db
    alert_time = datetime.now(timezone.utc) - timedelta(minutes=15)

    await _insert_x_alert(
        d._conn,
        event_id="evt-unresolved-search",
        tweet_author="CrashiusClay69",
        tweet_id="777",
        received_at=alert_time.isoformat(),
        extracted_cashtag="$GIGA",
        classifier_confidence=0.80,
    )

    resp = await c.get("/api/x_alerts?limit=1")

    assert resp.status_code == 200
    alert = resp.json()["alerts"][0]
    assert alert["outcome_status"] == "unresolved_symbol"
    assert alert["asset_url"] == "https://www.coingecko.com/en/search?query=GIGA"
    assert alert["asset_url_source"] == "unresolved_symbol"


async def test_x_alerts_endpoint_clamps_limit_and_handles_empty(client):
    resp = await client.get("/api/x_alerts?limit=500")

    assert resp.status_code == 200
    body = resp.json()
    assert body["alerts"] == []
    assert body["stats_24h"]["alerts"] == 0
    assert body["stats_24h"]["avg_confidence"] is None


async def test_x_alerts_resolver_does_not_query_candidates_coingecko_id(
    client, db, monkeypatch
):
    """Regression test for BL-NEW-DASHBOARD-X-ALERTS-RESOLVER-SCHEMA-ALIGN.

    The `candidates` table on prod has no `coingecko_id` column; the old
    resolver issued a SELECT against it, which `_safe_fetchall` caught
    silently as `OperationalError`. Verified prod failure mode by the
    repeated `dashboard_x_alerts_outcome_source_unavailable err=no such
    column: coingecko_id` log line on srilu (post-PR #190 deploy).

    This test (a) confirms the request succeeds against a fresh schema
    (which is what prod looks like), and (b) asserts no SQL containing
    `candidates` AND `coingecko_id` is executed during the request. If a
    future change reintroduces the dead branch, this test fails.
    """
    import dashboard.db as ddb

    c = client
    d, _db_path = db

    captured_sql: list[str] = []
    original_execute = ddb.aiosqlite.Connection.execute

    async def _spy_execute(self, sql, *args, **kwargs):
        captured_sql.append(sql)
        return await original_execute(self, sql, *args, **kwargs)

    monkeypatch.setattr(ddb.aiosqlite.Connection, "execute", _spy_execute)

    # Insert one alert that WOULD have hit the contract-match path:
    # extracted_ca + extracted_chain are present, resolved_coin_id is NULL,
    # extracted_cashtag is NULL. The legacy code would have queried
    # candidates.coingecko_id; the new code skips that step entirely.
    received_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _insert_x_alert(
        d._conn,
        event_id="schema-align-1",
        tweet_author="testuser",
        tweet_id="999000111",
        received_at=received_at,
        extracted_ca="0xdeadbeef",
        extracted_chain="ethereum",
        extracted_cashtag=None,
        resolved_coin_id=None,
    )

    resp = await c.get("/api/x_alerts?limit=10")
    assert resp.status_code == 200

    offending = [
        s
        for s in captured_sql
        if ("candidates" in s.lower() and "coingecko_id" in s.lower())
    ]
    assert not offending, (
        f"resolver issued SQL referencing nonexistent candidates.coingecko_id: "
        f"{offending}"
    )

    # Also assert the request returned the unresolved-symbol outcome for
    # the CA-only alert (no cashtag → falls through to 'unresolved').
    alert = next(
        a for a in resp.json()["alerts"] if a["event_id"] == "schema-align-1"
    )
    assert alert["outcome_status"] in {"unresolved", "no_entry_price", "no_current_price"}


async def test_x_alerts_entry_price_uses_batched_query_per_source(client, db, monkeypatch):
    """Regression guard for BL-NEW-DASHBOARD-X-ALERTS-TIMEOUT-FIX.

    The entry-price preload must issue EXACTLY ONE query per source
    table for all resolved coin_ids — not one per row. Previously the
    per-row 5-source-table BETWEEN sweep scaled at O(N rows x 5
    queries) and tripped the 5s frontend timeout at limit=80 (prod
    9.3s 2026-05-21). The batched form collapses to a constant 5
    queries regardless of row count.

    This test inserts multiple alerts referencing the same coin_id,
    runs the endpoint, and asserts that each entry-price source
    table appears in exactly one SELECT.
    """
    import dashboard.db as ddb

    c = client
    d, _db_path = db

    # Insert 5 alerts with the same resolved_coin_id so the batched
    # preload's win is exercised (5 rows -> would be 5x5=25 source
    # queries pre-fix, but 5 total post-fix).
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(5):
        await _insert_x_alert(
            d._conn,
            event_id=f"batch-{i}",
            tweet_author="alice",
            tweet_id=str(900 + i),
            received_at=(base - timedelta(minutes=i * 10)).isoformat(),
            extracted_cashtag="$BATCH",
            resolved_coin_id="batch-coin",
            classifier_confidence=0.80,
        )

    captured_sql: list[str] = []
    original_execute = ddb.aiosqlite.Connection.execute

    async def _spy_execute(self, sql, *args, **kwargs):
        captured_sql.append(sql)
        return await original_execute(self, sql, *args, **kwargs)

    monkeypatch.setattr(ddb.aiosqlite.Connection, "execute", _spy_execute)

    resp = await c.get("/api/x_alerts?limit=10")
    assert resp.status_code == 200

    # Contract: total entry-price SELECTs must be constant w.r.t. row
    # count (PR-review fold — relaxed from "exactly 1 per source" so a
    # future legitimate chunking refactor (split IN-list at SQLite
    # variable cap) doesn't trip a brittle assertion. The intent is
    # O(1) in N rows, NOT "literally 1 SELECT per table".)
    #
    # Upper bound: ≤5 source-table SELECTs total (one per source) when
    # all coin_ids fit in a single IN-list (<500 — guaranteed by the
    # 500-cap slice in get_x_alerts). Pre-fix code would have issued
    # 25 (5 rows × 5 sources) for this setup.
    entry_price_selects = [
        s for s in captured_sql
        if "BETWEEN ?" in s
        and any(
            f"FROM {t}" in s
            for t in (
                "gainers_snapshots",
                "losers_snapshots",
                "volume_history_cg",
                "volume_spikes",
                "momentum_7d",
            )
        )
    ]
    assert 1 <= len(entry_price_selects) <= 5, (
        f"expected ≤5 batched entry-price SELECTs (one per source) for "
        f"a single coin_id batch, got {len(entry_price_selects)}: "
        f"{entry_price_selects}"
    )


async def test_x_alerts_entry_price_skips_preload_when_no_resolved_coins(client, db, monkeypatch):
    """Empty `resolved_coin_ids` set must NOT issue entry-price preload
    queries (would be `coin_id IN ()` syntax error)."""
    import dashboard.db as ddb

    c = client
    d, _db_path = db

    # Insert one alert with no resolved coin (cashtag-only, unresolved).
    await _insert_x_alert(
        d._conn,
        event_id="no-resolved",
        tweet_author="bob",
        tweet_id="800",
        received_at=datetime.now(timezone.utc).isoformat(),
        extracted_cashtag="$UNKNOWN",
        resolved_coin_id=None,
        classifier_confidence=0.50,
    )

    captured_sql: list[str] = []
    original_execute = ddb.aiosqlite.Connection.execute

    async def _spy_execute(self, sql, *args, **kwargs):
        captured_sql.append(sql)
        return await original_execute(self, sql, *args, **kwargs)

    monkeypatch.setattr(ddb.aiosqlite.Connection, "execute", _spy_execute)

    resp = await c.get("/api/x_alerts?limit=10")
    assert resp.status_code == 200

    # No preload queries should have fired — the cashtag/symbol resolver
    # still runs but the entry-price BETWEEN ? sweep against the 5 source
    # tables must NOT appear when no coin_ids resolved.
    entry_price_selects = [
        s for s in captured_sql
        if "BETWEEN ?" in s
        and any(t in s for t in (
            "FROM gainers_snapshots",
            "FROM losers_snapshots",
            "FROM volume_history_cg",
            "FROM volume_spikes",
            "FROM momentum_7d",
        ))
    ]
    assert entry_price_selects == [], (
        f"entry-price preload should be skipped when no coin_ids resolve, "
        f"got: {entry_price_selects}"
    )
