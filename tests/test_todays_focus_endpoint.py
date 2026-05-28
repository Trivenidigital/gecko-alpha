"""Tests for the read-only Today's Focus dashboard queue."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.api import create_app
from scout.db import Database

_CHECK_SPEC = importlib.util.spec_from_file_location(
    "check_todays_focus_contract",
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "check_todays_focus_contract.py",
)
_CHECKER = importlib.util.module_from_spec(_CHECK_SPEC)
sys.modules["check_todays_focus_contract"] = _CHECKER
_CHECK_SPEC.loader.exec_module(_CHECKER)


def _assert_todays_focus_contract(payload: dict, *, window_hours: int = 36) -> None:
    result = _CHECKER.validate_payload(payload, requested_window=window_hours)
    assert result.is_clean, result.criticals


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
    _, db_path = db
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, db[0]
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


async def _insert_open_trade(
    conn,
    *,
    token_id: str,
    symbol: str | None = None,
    entry_price: float = 100.0,
    current_price: float | None = 103.0,
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
        await conn.execute(
            """INSERT OR REPLACE INTO price_cache
               (coin_id, current_price, price_change_24h, market_cap, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                token_id,
                current_price,
                price_change_24h,
                50_000_000.0,
                updated_at or now.isoformat(),
            ),
        )
    await conn.commit()


async def _insert_gainer(
    conn,
    *,
    coin_id: str,
    symbol: str | None = None,
    appeared_at: str | None = None,
    detected_price: float | None = 100.0,
    current_price: float | None = 104.0,
    price_change_24h: float = 24.0,
):
    now = datetime.now(timezone.utc)
    appeared = appeared_at or (now - timedelta(hours=1)).isoformat()
    sym = symbol or coin_id.upper()[:8]
    await conn.execute(
        """INSERT INTO gainers_comparisons
           (coin_id, symbol, name, price_change_24h, appeared_on_gainers_at,
            detected_by_narrative, detected_by_pipeline, detected_by_chains,
            detected_by_spikes, is_gap, detected_price)
           VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 1, ?)""",
        (
            coin_id,
            sym,
            coin_id.title(),
            price_change_24h,
            appeared,
            detected_price,
        ),
    )
    if current_price is not None:
        await conn.execute(
            """INSERT OR REPLACE INTO price_cache
               (coin_id, current_price, price_change_24h, market_cap, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (coin_id, current_price, price_change_24h, 75_000_000.0, now.isoformat()),
        )
    await conn.commit()


async def _insert_prediction(conn, *, coin_id: str, counter_flags: list | str):
    flags_text = (
        counter_flags if isinstance(counter_flags, str) else json.dumps(counter_flags)
    )
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
            "test",
            "Test",
            coin_id,
            coin_id.upper()[:8],
            coin_id.title(),
            1_000_000.0,
            1.0,
            50,
            "medium",
            "medium",
            "test",
            "{}",
            now,
            88,
            flags_text,
        ),
    )
    await conn.commit()


async def test_todays_focus_shape_read_only_and_closed_schema(client):
    c, db = client
    await _insert_open_trade(db._conn, token_id="paper-a")
    await _insert_gainer(db._conn, coin_id="tracker-a")

    resp = await c.get("/api/todays_focus?window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    _assert_todays_focus_contract(payload)
    meta = payload["meta"]
    assert meta["read_only"] is True
    assert meta["not_trade_advice"] is True
    assert meta["visibility_only"] is True
    assert meta["not_for_alerting"] is True
    assert meta["not_for_execution"] is True
    assert meta["not_for_sizing"] is True
    assert meta["not_for_source_ranking"] is True
    assert meta["max_rows"] == 5
    assert len(payload["rows"]) <= 5
    for row in payload["rows"]:
        assert "action_label" not in row
        assert "trade_score" not in row
        assert "sort_key" not in row
        assert "why_now" not in row


async def test_todays_focus_fixed_recipe_selects_three_paper_and_two_tracker(client):
    c, db = client
    now = datetime.now(timezone.utc)
    for i in range(4):
        await _insert_open_trade(
            db._conn,
            token_id=f"paper-{i}",
            opened_at=(now - timedelta(minutes=i)).isoformat(),
        )
    for i in range(3):
        await _insert_gainer(
            db._conn,
            coin_id=f"tracker-{i}",
            appeared_at=(now - timedelta(minutes=i)).isoformat(),
        )

    resp = await c.get("/api/todays_focus?window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    _assert_todays_focus_contract(payload)
    assert len(payload["rows"]) == 5
    assert [r["source_corpus"] for r in payload["rows"]].count("paper") == 3
    assert [r["source_corpus"] for r in payload["rows"]].count("tracker") == 2
    assert [r["token_id"] for r in payload["rows"][:3]] == [
        "paper-0",
        "paper-1",
        "paper-2",
    ]


async def test_todays_focus_fills_underfilled_quota_from_unused_rows(client):
    c, db = client
    now = datetime.now(timezone.utc)
    await _insert_open_trade(db._conn, token_id="paper-only")
    for i in range(4):
        await _insert_gainer(
            db._conn,
            coin_id=f"tracker-fill-{i}",
            appeared_at=(now - timedelta(minutes=i)).isoformat(),
        )

    resp = await c.get("/api/todays_focus?window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    _assert_todays_focus_contract(payload)
    assert len(payload["rows"]) == 5
    assert [r["source_corpus"] for r in payload["rows"]].count("paper") == 1
    assert [r["source_corpus"] for r in payload["rows"]].count("tracker") == 4


async def test_todays_focus_move_basis_is_explicit_for_paper_and_tracker(client):
    c, db = client
    await _insert_open_trade(
        db._conn,
        token_id="paper-move",
        entry_price=100.0,
        current_price=110.0,
    )
    await _insert_gainer(
        db._conn,
        coin_id="tracker-move",
        detected_price=200.0,
        current_price=250.0,
    )

    resp = await c.get("/api/todays_focus?window_hours=36")

    assert resp.status_code == 200, resp.text
    rows = {row["token_id"]: row for row in resp.json()["rows"]}
    assert rows["paper-move"]["current_move_pct"] == 10.0
    assert rows["paper-move"]["move_basis"] == "paper_entry"
    assert rows["tracker-move"]["current_move_pct"] == 25.0
    assert rows["tracker-move"]["move_basis"] == "tracker_detection"


async def test_todays_focus_block_cause_reports_immediate_blocker(client):
    c, db = client
    now = datetime.now(timezone.utc)
    await _insert_open_trade(
        db._conn,
        token_id="policy-stale",
        actionable=0,
        opened_at=(now - timedelta(hours=1)).isoformat(),
        updated_at=(now - timedelta(hours=3)).isoformat(),
    )
    await _insert_gainer(
        db._conn,
        coin_id="structural-tracker",
        detected_price=None,
        current_price=103.0,
        appeared_at=(now - timedelta(hours=1)).isoformat(),
    )
    await _insert_open_trade(
        db._conn,
        token_id="normal-paper",
        opened_at=(now - timedelta(minutes=15)).isoformat(),
    )

    resp = await c.get("/api/todays_focus?window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    _assert_todays_focus_contract(payload)
    rows = {row["token_id"]: row for row in payload["rows"]}
    assert rows["policy-stale"]["trade_inbox_group"] == "blocked"
    assert rows["policy-stale"]["block_cause"] == "data_quality"
    assert rows["structural-tracker"]["trade_inbox_group"] == "blocked"
    assert rows["structural-tracker"]["block_cause"] == "data_quality"
    assert "tracker_only_no_paper_trade" in rows["structural-tracker"]["risk_reasons"]
    assert rows["normal-paper"]["block_cause"] is None


async def test_todays_focus_sanitizes_counter_flag_copy(client):
    c, db = client
    await _insert_open_trade(db._conn, token_id="flagged")
    await _insert_prediction(
        db._conn,
        coin_id="flagged",
        counter_flags=[
            {"type": "holder_concentration", "severity": "high", "detail": "buy now"},
            {"type": "thin_liquidity", "severity": "warning", "detail": "thin pool"},
        ],
    )

    resp = await c.get("/api/todays_focus?window_hours=36")

    assert resp.status_code == 200, resp.text
    row = resp.json()["rows"][0]
    assert row["token_id"] == "flagged"
    rendered = " ".join(row["counter_flag_facts"]).lower()
    assert "buy now" not in rendered
    assert "high" not in rendered
    assert "thin_liquidity" in rendered or "thin pool" in rendered


async def test_todays_focus_empty_state_is_factual(client):
    c, _ = client

    resp = await c.get("/api/todays_focus?window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    _assert_todays_focus_contract(payload)
    assert payload["rows"] == []
    assert payload["meta"]["empty_state"].startswith("No eligible Trade Inbox rows")
