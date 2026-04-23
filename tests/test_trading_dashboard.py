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


async def test_dashboard_returns_would_be_live(tmp_path):
    """Task 6: dashboard positions endpoint must surface would_be_live column."""
    from scout.trading.paper import PaperTrader
    from dashboard.db import get_trading_positions

    db_path = tmp_path / "gecko.db"
    db = Database(str(db_path))
    await db.initialize()
    trader = PaperTrader()

    async def open_one(tok: str, live_cap: int, min_quant: int):
        return await trader.execute_buy(
            db=db,
            token_id=tok,
            symbol=tok[:2].upper(),
            name=tok,
            chain="eth",
            signal_type="first_signal",
            signal_data={"quant_score": 50},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=40.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="first_signal",
            lead_time_vs_trending_min=None,
            lead_time_vs_trending_status=None,
            live_eligible_cap=live_cap,
            min_quant_score=min_quant,
        )

    await open_one("live_tok", 1, 1)  # First trade: cap=1 → would_be_live=1
    await open_one("cap_tok", 1, 1)  # Second trade: cap already hit → would_be_live=0
    await db.close()

    positions = await get_trading_positions(str(db_path))
    by_tok = {p["token_id"]: p for p in positions}
    assert (
        "would_be_live" in by_tok["live_tok"]
    ), "would_be_live key missing from response"
    assert (
        by_tok["live_tok"]["would_be_live"] == 1
    ), "first trade should be live-eligible"
    assert by_tok["cap_tok"]["would_be_live"] == 0, "second trade should be beyond-cap"
