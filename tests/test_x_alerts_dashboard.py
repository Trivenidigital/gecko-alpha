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
    assert body["alerts"][0]["text_preview"].startswith("_Shadow36 called")
    assert body["alerts"][0]["classifier_version"] == "narrative_classifier-v1.1"


async def test_x_alerts_endpoint_clamps_limit_and_handles_empty(client):
    resp = await client.get("/api/x_alerts?limit=500")

    assert resp.status_code == 200
    body = resp.json()
    assert body["alerts"] == []
    assert body["stats_24h"]["alerts"] == 0
    assert body["stats_24h"]["avg_confidence"] is None
