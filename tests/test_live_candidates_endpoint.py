"""Tests for BL-NEW-LIVE-DECISION-COCKPIT V1 live candidates endpoint."""

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
    symbol: str,
    entry_price: float,
    actionable: int | None,
    would_be_live: int | None,
    opened_at: str | None = None,
):
    now = datetime.now(timezone.utc)
    opened = opened_at or (now - timedelta(hours=2)).isoformat()
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
            symbol,
            token_id.title(),
            "coingecko",
            "volume_spike",
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
    await conn.commit()


async def _upsert_price_cache(
    conn,
    *,
    coin_id: str,
    current_price: float,
    market_cap: float = 50_000_000,
    updated_at: str | None = None,
):
    now = datetime.now(timezone.utc)
    updated = updated_at or now.isoformat()
    await conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, market_cap, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (coin_id, current_price, 1.23, market_cap, updated),
    )
    await conn.commit()


async def _insert_prediction(
    conn,
    *,
    coin_id: str,
    counter_flags: list,
    narrative_fit_score: int = 50,
    counter_risk_score: int = 30,
):
    """Minimal predictions insert covering only the columns the cockpit reads."""
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """INSERT INTO predictions
           (category_id, category_name, coin_id, symbol, name,
            market_cap_at_prediction, price_at_prediction,
            narrative_fit_score, staying_power, confidence, reasoning,
            strategy_snapshot, predicted_at,
            counter_risk_score, counter_flags)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "cat",
            "test_category",
            coin_id,
            coin_id.upper(),
            coin_id.title(),
            1_000_000.0,
            1.0,
            narrative_fit_score,
            "medium",
            "medium",
            "test",
            "{}",
            now,
            counter_risk_score,
            json.dumps(counter_flags),
        ),
    )
    await conn.commit()


def _assert_envelope(payload: dict, *, expected_open_trades: int | None = None):
    assert "meta" in payload
    assert "rows" in payload
    meta = payload["meta"]
    assert meta["read_only"] is True
    assert meta["not_trade_advice"] is True
    assert meta["experimental"] is True
    assert meta["rows_returned"] == len(payload["rows"])
    assert meta["generated_at"]
    if expected_open_trades is not None:
        assert meta["open_trades_scanned"] == expected_open_trades


async def test_live_candidates_candidate(client):
    c, db = client
    await _insert_open_trade(
        db._conn,
        token_id="bitcoin",
        symbol="BTC",
        entry_price=100.0,
        actionable=1,
        would_be_live=1,
    )
    await _upsert_price_cache(db._conn, coin_id="bitcoin", current_price=105.0)

    resp = await c.get("/api/live_candidates")
    assert resp.status_code == 200
    payload = resp.json()
    _assert_envelope(payload, expected_open_trades=1)
    rows = payload["rows"]
    assert rows
    row = rows[0]
    assert row["token_id"] == "bitcoin"
    assert row["verdict"] == "candidate_review"
    assert row["entry_quality"] in ("fresh_entry", "acceptable_pullback")
    assert row["disclaimer"]


async def test_live_candidates_missing_price_is_data_insufficient(client):
    c, db = client
    await _insert_open_trade(
        db._conn,
        token_id="ethereum",
        symbol="ETH",
        entry_price=100.0,
        actionable=1,
        would_be_live=1,
    )
    resp = await c.get("/api/live_candidates")
    assert resp.status_code == 200
    payload = resp.json()
    _assert_envelope(payload, expected_open_trades=1)
    rows = payload["rows"]
    assert rows
    row = next(r for r in rows if r["token_id"] == "ethereum")
    assert row["verdict"] == "data_insufficient"
    assert "no_price_snapshot_for_token_id" in row["risk_reasons"]


async def test_live_candidates_extreme_stale_price_is_data_insufficient(client):
    c, db = client
    await _insert_open_trade(
        db._conn,
        token_id="solana",
        symbol="SOL",
        entry_price=100.0,
        actionable=1,
        would_be_live=1,
    )
    stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    await _upsert_price_cache(
        db._conn, coin_id="solana", current_price=101.0, updated_at=stale
    )

    resp = await c.get("/api/live_candidates")
    assert resp.status_code == 200
    payload = resp.json()
    _assert_envelope(payload, expected_open_trades=1)
    row = next(r for r in payload["rows"] if r["token_id"] == "solana")
    assert row["verdict"] == "data_insufficient"
    assert row["entry_quality"] == "too_stale"


async def test_live_candidates_actionable_zero_is_blocked(client):
    c, db = client
    await _insert_open_trade(
        db._conn,
        token_id="dogecoin",
        symbol="DOGE",
        entry_price=100.0,
        actionable=0,
        would_be_live=1,
    )
    await _upsert_price_cache(db._conn, coin_id="dogecoin", current_price=100.0)

    resp = await c.get("/api/live_candidates")
    assert resp.status_code == 200
    payload = resp.json()
    _assert_envelope(payload, expected_open_trades=1)
    row = next(r for r in payload["rows"] if r["token_id"] == "dogecoin")
    assert row["verdict"] == "blocked"


async def test_live_candidates_actionable_null_is_data_insufficient(client):
    c, db = client
    # actionable=NULL covers older pre-cutover rows; must not silently slip
    # into "watch" or any positive verdict.
    await _insert_open_trade(
        db._conn,
        token_id="cardano",
        symbol="ADA",
        entry_price=100.0,
        actionable=None,
        would_be_live=1,
    )
    await _upsert_price_cache(db._conn, coin_id="cardano", current_price=105.0)

    resp = await c.get("/api/live_candidates")
    assert resp.status_code == 200
    payload = resp.json()
    _assert_envelope(payload, expected_open_trades=1)
    row = next(r for r in payload["rows"] if r["token_id"] == "cardano")
    assert row["verdict"] == "data_insufficient"
    assert "actionable_null_pre_cutover" in row["risk_reasons"]
    assert "actionable=null" in row["inclusion_reasons"]


async def test_live_candidates_empty_cohort_returns_envelope(client):
    c, _ = client
    resp = await c.get("/api/live_candidates")
    assert resp.status_code == 200
    payload = resp.json()
    _assert_envelope(payload, expected_open_trades=0)
    assert payload["rows"] == []


async def test_live_candidates_counter_flags_accepts_rich_dict_shape(client):
    # Regression: predictions.counter_flags in prod is a list of dicts
    # ({flag, severity, detail}) — model previously declared list[str] and
    # 500'd on rows whose token had counter_flags rows.
    c, db = client
    await _insert_open_trade(
        db._conn,
        token_id="polkadot",
        symbol="DOT",
        entry_price=10.0,
        actionable=1,
        would_be_live=1,
    )
    await _upsert_price_cache(db._conn, coin_id="polkadot", current_price=10.4)
    rich_flags = [
        {"flag": "dead_project", "severity": "high",
         "detail": "Zero commits in the last 4 weeks"},
        {"flag": "weak_community", "severity": "high",
         "detail": "Reddit subscribers (0) below 100"},
    ]
    await _insert_prediction(
        db._conn,
        coin_id="polkadot",
        counter_flags=rich_flags,
        counter_risk_score=72,
    )

    resp = await c.get("/api/live_candidates")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    row = next(r for r in payload["rows"] if r["token_id"] == "polkadot")
    assert row["counter_flags"] == rich_flags
    assert row["counter_risk_score"] == 72


async def test_live_candidates_query_caps(client):
    c, _ = client
    resp = await c.get("/api/live_candidates?limit=999")
    assert resp.status_code == 422
