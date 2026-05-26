"""Tests for /api/signal_trust/scorecards (read-only signal scorecards)."""

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


async def test_scorecards_returns_200_with_invariants(client):
    c, _ = client
    resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    payload = resp.json()
    meta = payload["meta"]
    assert meta["ok"] is True
    assert meta["read_only"] is True
    assert meta["not_for_pruning"] is True
    assert meta["not_for_auto_disable"] is True
    assert meta["experimental"] is True
    assert meta["generated_at"]
    assert meta["windows_days"] == [7, 14, 30]
    assert "rows" in payload


async def test_scorecards_ordering_is_deterministic(client):
    c, d = client
    conn = d._conn
    assert conn is not None
    # Create two open trades with distinct signal types so union-of-keys is non-empty.
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity,
            tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at,
            would_be_live, actionable)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)""",
        (
            "t1",
            "T1",
            "t1",
            "coingecko",
            "volume_spike",
            "{}",
            1.0,
            100.0,
            10.0,
            20.0,
            10.0,
            1.2,
            0.9,
            "open",
            1,
            1,
        ),
    )
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity,
            tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at,
            would_be_live, actionable)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)""",
        (
            "t2",
            "T2",
            "t2",
            "coingecko",
            "chain_completed",
            "{}",
            1.0,
            100.0,
            10.0,
            20.0,
            10.0,
            1.2,
            0.9,
            "open",
            1,
            1,
        ),
    )
    await conn.commit()

    resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    signal_types = [r["signal_type"] for r in rows]
    assert signal_types == sorted(signal_types)
