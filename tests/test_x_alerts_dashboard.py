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
    assert alert["asset_url"] is None
    assert alert["asset_url_source"] == "ambiguous_symbol"
    assert alert["outcome_coin_id"] is None
    assert alert["entry_price_usd"] is None
    assert alert["current_price_usd"] is None
    assert alert["gain_pct_since_alert"] is None
    assert alert["profit_usd_at_300"] is None


async def test_x_alerts_endpoint_clamps_limit_and_handles_empty(client):
    resp = await client.get("/api/x_alerts?limit=500")

    assert resp.status_code == 200
    body = resp.json()
    assert body["alerts"] == []
    assert body["stats_24h"]["alerts"] == 0
    assert body["stats_24h"]["avg_confidence"] is None
