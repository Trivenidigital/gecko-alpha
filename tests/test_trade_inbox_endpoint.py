"""Tests for the read-only trade opportunity inbox."""

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


async def _insert_open_trade(
    conn,
    *,
    token_id: str,
    symbol: str | None = None,
    entry_price: float = 100.0,
    current_price: float | None = 102.0,
    actionable: int | None = 1,
    would_be_live: int | None = 1,
    opened_at: str | None = None,
    updated_at: str | None = None,
    signal_type: str = "volume_spike",
    price_change_24h: float = 12.0,
):
    now = datetime.now(timezone.utc)
    opened = opened_at or (now - timedelta(hours=2)).isoformat()
    sym = symbol or token_id.upper()[:8]
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity,
            tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at,
            would_be_live, actionable)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            sym,
            token_id.title(),
            "coingecko",
            signal_type,
            json.dumps({}),
            entry_price,
            1000.0,
            10.0,
            20.0,
            10.0,
            entry_price * 1.2,
            entry_price * 0.9,
            "open",
            opened,
            would_be_live,
            actionable,
        ),
    )
    if current_price is not None:
        updated = updated_at or now.isoformat()
        await conn.execute(
            """INSERT OR REPLACE INTO price_cache
               (coin_id, current_price, price_change_24h, market_cap, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (token_id, current_price, price_change_24h, 50_000_000.0, updated),
        )
    await conn.commit()


async def test_trade_inbox_groups_shape_and_read_only(client):
    c, db = client
    now = datetime.now(timezone.utc)
    await _insert_open_trade(db._conn, token_id="review", current_price=103.0)
    await _insert_open_trade(
        db._conn,
        token_id="moved",
        current_price=140.0,
        opened_at=(now - timedelta(hours=1)).isoformat(),
    )
    await _insert_open_trade(db._conn, token_id="blocked", actionable=0)
    await _insert_open_trade(
        db._conn,
        token_id="stale-warning",
        current_price=102.0,
        updated_at=(now - timedelta(minutes=90)).isoformat(),
    )

    before = await _paper_trade_count(db._conn)
    resp = await c.get("/api/trade_inbox?limit_per_group=10&window_hours=36")
    after = await _paper_trade_count(db._conn)

    assert resp.status_code == 200, resp.text
    assert after == before
    payload = resp.json()
    assert set(payload["groups"]) == {"act_now", "watch", "already_ran", "blocked"}
    assert payload["meta"]["read_only"] is True
    assert payload["meta"]["not_trade_advice"] is True
    assert payload["meta"]["experimental"] is True
    assert payload["meta"]["rows_returned"] == sum(
        len(v) for v in payload["groups"].values()
    )
    assert payload["groups"]["act_now"][0]["action_label"] == "REVIEW_NOW"
    assert payload["groups"]["watch"][0]["token_id"] == "stale-warning"
    assert payload["groups"]["already_ran"][0]["token_id"] == "moved"
    assert payload["groups"]["blocked"][0]["block_reason_primary"] == "NOT_ACTIONABLE"


async def test_trade_inbox_broad_cohort_surfaces_toes_beyond_raw_limit(client):
    c, db = client
    now = datetime.now(timezone.utc)
    for i in range(14):
        await _insert_open_trade(
            db._conn,
            token_id=f"newer-blocked-{i:02d}",
            actionable=0,
            opened_at=(now - timedelta(minutes=i)).isoformat(),
        )
    await _insert_open_trade(
        db._conn,
        token_id="toes",
        symbol="TOES",
        current_price=103.0,
        opened_at=(now - timedelta(hours=6)).isoformat(),
        price_change_24h=18.0,
    )

    resp = await c.get("/api/trade_inbox?limit_per_group=5&window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert [r["token_id"] for r in payload["groups"]["act_now"]] == ["toes"]
    assert payload["meta"]["source_rows_considered"] >= 15


async def test_trade_inbox_score_sort_key_and_why_now_are_deterministic(client):
    c, db = client
    opened = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await _insert_open_trade(
        db._conn,
        token_id="alpha",
        entry_price=100.0,
        current_price=102.0,
        opened_at=opened,
        price_change_24h=10.0,
    )
    await _insert_open_trade(
        db._conn,
        token_id="zeta",
        entry_price=100.0,
        current_price=102.0,
        opened_at=opened,
        price_change_24h=10.0,
    )

    resp = await c.get("/api/trade_inbox?limit_per_group=10&window_hours=36")

    assert resp.status_code == 200, resp.text
    rows = resp.json()["groups"]["act_now"]
    assert [r["token_id"] for r in rows] == ["alpha", "zeta"]
    assert rows[0]["trade_score"] == 100.0
    assert rows[0]["sort_key"] == rows[1]["sort_key"][:-1] + ["alpha"]
    assert rows[0]["why_now"][:3] == ["open_window", "window=open", "fresh_entry"]


async def test_trade_inbox_stale_boundaries_and_bad_data(client):
    c, db = client
    now = datetime.now(timezone.utc)
    await _insert_open_trade(db._conn, token_id="no-price", current_price=None)
    await _insert_open_trade(
        db._conn,
        token_id="bad-time",
        opened_at="not-iso",
    )
    await _insert_open_trade(
        db._conn,
        token_id="stale-60",
        updated_at=(now - timedelta(minutes=60)).isoformat(),
    )
    await _insert_open_trade(
        db._conn,
        token_id="stale-120",
        updated_at=(now - timedelta(minutes=120)).isoformat(),
    )
    await _insert_open_trade(
        db._conn,
        token_id="bad-price-time",
        updated_at="not-iso",
    )

    resp = await c.get("/api/trade_inbox?limit_per_group=10&window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    blocked = {r["token_id"]: r for r in payload["groups"]["blocked"]}
    watch = {r["token_id"]: r for r in payload["groups"]["watch"]}
    assert blocked["no-price"]["block_reason_primary"] == "NO_PRICE"
    assert blocked["bad-time"]["block_reason_primary"] == "BAD_TIMESTAMP"
    assert blocked["bad-price-time"]["block_reason_primary"] == "DATA_INSUFFICIENT"
    assert "price_timestamp_unparseable" in blocked["bad-price-time"]["risk_reasons"]
    assert blocked["stale-120"]["block_reason_primary"] == "STALE_PRICE"
    assert watch["stale-60"]["price_is_stale"] is True
    assert payload["meta"]["stale_warning_count"] >= 1
    assert payload["meta"]["hard_stale_count"] >= 1


async def test_trade_inbox_overflow_meta_exposes_hidden_rows(client):
    c, db = client
    now = datetime.now(timezone.utc)
    for i in range(7):
        await _insert_open_trade(
            db._conn,
            token_id=f"review-{i}",
            opened_at=(now - timedelta(minutes=i)).isoformat(),
        )

    resp = await c.get("/api/trade_inbox?limit_per_group=3&window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert len(payload["groups"]["act_now"]) == 3
    assert payload["meta"]["group_counts"]["act_now"] == 7
    assert payload["meta"]["group_hidden_counts"]["act_now"] == 4


async def _paper_trade_count(conn) -> int:
    cursor = await conn.execute("SELECT COUNT(*) AS c FROM paper_trades")
    row = await cursor.fetchone()
    return int(row["c"])
