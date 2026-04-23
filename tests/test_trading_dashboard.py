"""Tests for paper trading dashboard API endpoints."""

import json
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
    d, db_path = db
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, d
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


async def _insert_trade(
    conn, token_id, symbol, signal_type, status, pnl_usd=None, pnl_pct=None
):
    now = datetime.now(timezone.utc)
    opened = (now - timedelta(hours=2)).isoformat()
    closed = now.isoformat() if status != "open" else None
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, pnl_usd, pnl_pct, opened_at, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            symbol,
            token_id.title(),
            "coingecko",
            signal_type,
            json.dumps({}),
            100.0,
            1000.0,
            10.0,
            20.0,
            10.0,
            120.0,
            90.0,
            status,
            pnl_usd,
            pnl_pct,
            opened,
            closed,
        ),
    )
    await conn.commit()


async def test_get_positions(client):
    c, db = client
    await _insert_trade(db._conn, "bitcoin", "BTC", "volume_spike", "open")
    resp = await c.get("/api/trading/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["symbol"] == "BTC"


async def test_get_history(client):
    c, db = client
    await _insert_trade(
        db._conn, "bitcoin", "BTC", "volume_spike", "closed_tp", 200.0, 20.0
    )
    resp = await c.get("/api/trading/history")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


async def test_get_stats(client):
    c, db = client
    await _insert_trade(
        db._conn, "bitcoin", "BTC", "volume_spike", "closed_tp", 200.0, 20.0
    )
    await _insert_trade(
        db._conn, "ethereum", "ETH", "narrative_prediction", "closed_sl", -50.0, -5.0
    )
    resp = await c.get("/api/trading/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_pnl_usd" in data
    assert "win_rate_pct" in data


async def test_get_stats_by_signal(client):
    c, db = client
    await _insert_trade(
        db._conn, "bitcoin", "BTC", "volume_spike", "closed_tp", 200.0, 20.0
    )
    await _insert_trade(
        db._conn, "ethereum", "ETH", "volume_spike", "closed_sl", -50.0, -5.0
    )
    resp = await c.get("/api/trading/stats/by-signal")
    assert resp.status_code == 200
    data = resp.json()
    assert "volume_spike" in data


async def test_positions_empty(client):
    c, _ = client
    resp = await c.get("/api/trading/positions")
    assert resp.status_code == 200
    assert resp.json() == []


async def _seed_price(conn, token_id, current_price):
    await conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (token_id, current_price, datetime.now(timezone.utc).isoformat()),
    )
    await conn.commit()


async def test_unrealized_pnl_uses_remaining_qty_post_leg_1(client):
    """Post-leg-1 unrealized P&L must be computed on remaining_qty, not initial quantity."""
    c, db = client
    await _insert_trade(db._conn, "ladder-coin", "LDR", "first_signal", "open")
    await db._conn.execute(
        "UPDATE paper_trades SET remaining_qty = 7.0, leg_1_filled_at = ? "
        "WHERE token_id = 'ladder-coin'",
        (datetime.now(timezone.utc).isoformat(),),
    )
    await db._conn.commit()
    await _seed_price(db._conn, "ladder-coin", 110.0)

    resp = await c.get("/api/trading/positions")
    assert resp.status_code == 200
    pos = [p for p in resp.json() if p["token_id"] == "ladder-coin"][0]
    # entry=100, cp=110, remaining_qty=7 → (110-100)*7 = 70.00
    assert pos["unrealized_pnl_usd"] == 70.00
    assert pos["remaining_qty"] == 7.0


async def test_unrealized_pnl_falls_back_to_quantity_pre_cutover(client):
    """Pre-cutover trades have remaining_qty=NULL and must use initial quantity."""
    c, db = client
    await _insert_trade(db._conn, "legacy-coin", "LGC", "first_signal", "open")
    await _seed_price(db._conn, "legacy-coin", 110.0)

    resp = await c.get("/api/trading/positions")
    assert resp.status_code == 200
    pos = [p for p in resp.json() if p["token_id"] == "legacy-coin"][0]
    # remaining_qty is NULL, quantity=10 → (110-100)*10 = 100.00
    assert pos["unrealized_pnl_usd"] == 100.00
    assert pos["remaining_qty"] is None


