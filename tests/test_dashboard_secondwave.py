"""Dashboard API endpoints for second-wave detection."""
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.api import create_app
from scout.db import Database


@pytest.fixture
async def seeded_db(tmp_path):
    db_path = tmp_path / "dash.db"
    d = Database(db_path)
    await d.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await d.insert_secondwave_candidate({
        "contract_address": "0xd", "chain": "eth",
        "token_name": "Dash", "ticker": "DSH", "coingecko_id": None,
        "peak_quant_score": 80, "peak_signals_fired": ["x"],
        "first_seen_at": now, "original_alert_at": now,
        "original_market_cap": 1e6, "alert_market_cap": 2e6,
        "days_since_first_seen": 5.0, "price_drop_from_peak_pct": -40.0,
        "current_price": 0.8, "current_market_cap": 1.2e6,
        "current_volume_24h": 5e5, "price_vs_alert_pct": 80.0,
        "volume_vs_cooldown_avg": 3.0, "price_is_stale": False,
        "reaccumulation_score": 85,
        "reaccumulation_signals": ["sufficient_drawdown", "price_recovery"],
        "detected_at": now, "alerted_at": now,
    })
    await d.close()
    yield str(db_path)


@pytest.fixture
async def client(seeded_db):
    import dashboard.api as api_mod
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None
    app = create_app(seeded_db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


async def test_secondwave_candidates_endpoint(client):
    r = await client.get("/api/secondwave/candidates")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["ticker"] == "DSH"


async def test_secondwave_stats_endpoint(client):
    r = await client.get("/api/secondwave/stats")
    assert r.status_code == 200
    data = r.json()
    assert "count" in data
    assert "avg_score" in data
