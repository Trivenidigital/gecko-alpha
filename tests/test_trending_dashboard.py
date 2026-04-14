"""Tests for trending tracker dashboard API endpoints."""

from datetime import datetime, timezone

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from scout.db import Database


@pytest.fixture
async def seeded_db(tmp_path):
    """Create DB with trending tables and seed data."""
    db_path = str(tmp_path / "test_trending_dash.db")
    # Use scout.db.Database to create all tables
    sdb = Database(db_path)
    await sdb.initialize()

    now = datetime.now(timezone.utc).isoformat()
    # Seed trending snapshots
    for i in range(3):
        await sdb._conn.execute(
            """INSERT INTO trending_snapshots
               (coin_id, symbol, name, market_cap_rank, trending_score, snapshot_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (f"coin-{i}", f"C{i}", f"Coin {i}", 100 + i, float(i + 1), now),
        )

    # Seed trending comparisons
    await sdb._conn.execute(
        """INSERT INTO trending_comparisons
           (coin_id, symbol, name, appeared_on_trending_at, is_gap,
            detected_by_narrative, narrative_lead_minutes,
            detected_by_pipeline, detected_by_chains)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("coin-0", "C0", "Coin 0", now, 0, 1, 45.0, 0, 0),
    )
    await sdb._conn.execute(
        """INSERT INTO trending_comparisons
           (coin_id, symbol, name, appeared_on_trending_at, is_gap,
            detected_by_narrative, detected_by_pipeline, detected_by_chains)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("coin-1", "C1", "Coin 1", now, 1, 0, 0, 0),
    )
    await sdb._conn.commit()
    await sdb.close()
    return db_path


@pytest.fixture
async def client(seeded_db):
    from dashboard.api import create_app
    app = create_app(db_path=seeded_db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_trending_snapshots_endpoint(client):
    resp = await client.get("/api/trending/snapshots")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    assert data[0]["coin_id"] in {"coin-0", "coin-1", "coin-2"}


@pytest.mark.asyncio
async def test_trending_stats_endpoint(client):
    resp = await client.get("/api/trending/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_tracked"] == 2
    assert data["caught_before_trending"] == 1
    assert data["missed"] == 1
    assert data["hit_rate_pct"] == 50.0
    assert data["by_narrative"] == 1


@pytest.mark.asyncio
async def test_trending_comparisons_endpoint(client):
    resp = await client.get("/api/trending/comparisons")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    # Check coin-0 was detected by narrative
    c0 = next(d for d in data if d["coin_id"] == "coin-0")
    assert c0["detected_by_narrative"] == 1
    assert c0["is_gap"] == 0


@pytest.mark.asyncio
async def test_trending_snapshots_query_params(client):
    resp = await client.get("/api/trending/snapshots?hours=1&limit=2")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) <= 2
