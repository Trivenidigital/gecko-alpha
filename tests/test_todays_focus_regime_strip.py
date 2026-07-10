"""DASH-07/SIG-09 (trailing-7d per-trade PnL + hostile display cue) and
SIG-08 (detection-earliness truth) display surfaces on /api/todays_focus meta.

DB-layer tests: they call ``dashboard.db.get_todays_focus`` directly
(aiosqlite only, no httpx/ASGI) so they run on Windows where the
endpoint-level httpx tests crash on the OpenSSL uplink. Endpoint-level
wiring (api.py -> db.py threshold plumbing) is covered in
``test_todays_focus_endpoint.py`` (CI/Linux).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dashboard import db as ddb
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


def _assert_contract(payload: dict, *, window_hours: int = 36) -> None:
    result = _CHECKER.validate_payload(payload, requested_window=window_hours)
    assert result.is_clean, result.criticals


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    d = Database(db_path)
    await d.initialize()
    yield d, str(db_path)
    await d.close()


async def _insert_closed_trade(
    conn,
    *,
    token_id: str,
    pnl_usd: float,
    closed_days_ago: float = 1.0,
    status: str = "closed_sl",
    signal_type: str = "volume_spike",
):
    now = datetime.now(timezone.utc)
    opened = (now - timedelta(days=closed_days_ago, hours=6)).isoformat()
    closed = (now - timedelta(days=closed_days_ago)).isoformat()
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, pnl_usd, opened_at, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            token_id.upper()[:8],
            token_id.title(),
            "coingecko",
            signal_type,
            json.dumps({}),
            100.0,
            300.0,
            3.0,
            20.0,
            10.0,
            120.0,
            90.0,
            status,
            pnl_usd,
            opened,
            closed,
        ),
    )
    await conn.commit()


async def _insert_opened_trade_with_leadtime(
    conn,
    *,
    token_id: str,
    lead_min: float | None,
    lead_status: str,
    opened_days_ago: float = 1.0,
    signal_type: str = "volume_spike",
):
    now = datetime.now(timezone.utc)
    opened = (now - timedelta(days=opened_days_ago)).isoformat()
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at,
            lead_time_vs_trending_min, lead_time_vs_trending_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            token_id.upper()[:8],
            token_id.title(),
            "coingecko",
            signal_type,
            json.dumps({}),
            100.0,
            300.0,
            3.0,
            20.0,
            10.0,
            120.0,
            90.0,
            "open",
            opened,
            lead_min,
            lead_status,
        ),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# DASH-07 / SIG-09: trailing-7d per-trade paper PnL + hostile display cue
# ---------------------------------------------------------------------------


async def test_trailing_pnl_absent_when_no_closed_trades(db):
    _, path = db
    payload = await ddb.get_todays_focus(path)
    _assert_contract(payload)
    assert "trailing_7d_paper_pnl" not in payload["meta"]
    assert "trailing_7d_paper_pnl_is_visual_context_only" not in payload["meta"]


async def test_trailing_pnl_present_below_gate_never_hostile(db):
    d, path = db
    for i in range(3):  # below n_gate=5
        await _insert_closed_trade(d._conn, token_id=f"t{i}", pnl_usd=-50.0)
    payload = await ddb.get_todays_focus(path, hostile_per_trade_threshold_usd=-10.0)
    _assert_contract(payload)
    block = payload["meta"]["trailing_7d_paper_pnl"]
    assert block["closed_trades"] == 3
    assert block["per_trade_usd"] == -50.0
    assert block["total_pnl_usd"] == -150.0
    assert block["n_gate"] == 5
    assert block["window_days"] == 7
    # Below the gate the figure renders '-' client-side; hostile MUST be
    # False regardless of the per-trade value.
    assert block["hostile"] is False
    assert payload["meta"]["trailing_7d_paper_pnl_is_visual_context_only"] is True


async def test_trailing_pnl_hostile_at_gate_below_threshold(db):
    d, path = db
    for i in range(6):
        await _insert_closed_trade(d._conn, token_id=f"h{i}", pnl_usd=-32.5)
    payload = await ddb.get_todays_focus(path, hostile_per_trade_threshold_usd=-10.0)
    _assert_contract(payload)
    block = payload["meta"]["trailing_7d_paper_pnl"]
    assert block["closed_trades"] == 6
    assert block["per_trade_usd"] == -32.5
    assert block["display_threshold_usd"] == -10.0
    assert block["hostile"] is True


async def test_trailing_pnl_not_hostile_when_above_threshold(db):
    d, path = db
    for i in range(6):
        await _insert_closed_trade(d._conn, token_id=f"p{i}", pnl_usd=25.0)
    payload = await ddb.get_todays_focus(path, hostile_per_trade_threshold_usd=-10.0)
    block = payload["meta"]["trailing_7d_paper_pnl"]
    assert block["per_trade_usd"] == 25.0
    assert block["hostile"] is False


async def test_trailing_pnl_excludes_trades_outside_7d_window(db):
    d, path = db
    for i in range(6):
        await _insert_closed_trade(
            d._conn, token_id=f"old{i}", pnl_usd=-100.0, closed_days_ago=10.0
        )
    payload = await ddb.get_todays_focus(path)
    assert "trailing_7d_paper_pnl" not in payload["meta"]


async def test_trailing_pnl_excludes_open_trades(db):
    d, path = db
    await _insert_opened_trade_with_leadtime(
        d._conn, token_id="open1", lead_min=100.0, lead_status="ok"
    )
    payload = await ddb.get_todays_focus(path)
    assert "trailing_7d_paper_pnl" not in payload["meta"]


async def test_trailing_pnl_threshold_defaults_to_minus_ten(db):
    d, path = db
    for i in range(5):
        await _insert_closed_trade(d._conn, token_id=f"d{i}", pnl_usd=-11.0)
    payload = await ddb.get_todays_focus(path)  # threshold None -> server default
    block = payload["meta"]["trailing_7d_paper_pnl"]
    assert block["display_threshold_usd"] == -10.0
    assert block["hostile"] is True  # -11.0 < -10.0 at n=5 gate


# ---------------------------------------------------------------------------
# SIG-08: detection-earliness truth surface
# ---------------------------------------------------------------------------


async def test_earliness_absent_when_no_opened_trades(db):
    _, path = db
    payload = await ddb.get_todays_focus(path)
    _assert_contract(payload)
    assert "earliness_vs_trending" not in payload["meta"]
    assert "earliness_vs_trending_is_visual_context_only" not in payload["meta"]


async def test_earliness_median_late_and_no_reference_pct(db):
    d, path = db
    # 3 ok (100, 200, 300 min -> median 200 = late) + 2 no_reference; total 5.
    await _insert_opened_trade_with_leadtime(
        d._conn, token_id="ok1", lead_min=100.0, lead_status="ok"
    )
    await _insert_opened_trade_with_leadtime(
        d._conn, token_id="ok2", lead_min=300.0, lead_status="ok"
    )
    await _insert_opened_trade_with_leadtime(
        d._conn, token_id="ok3", lead_min=200.0, lead_status="ok"
    )
    await _insert_opened_trade_with_leadtime(
        d._conn, token_id="nr1", lead_min=None, lead_status="no_reference"
    )
    await _insert_opened_trade_with_leadtime(
        d._conn, token_id="nr2", lead_min=None, lead_status="no_reference"
    )
    payload = await ddb.get_todays_focus(path)
    _assert_contract(payload)
    block = payload["meta"]["earliness_vs_trending"]
    assert block["median_lead_time_min"] == 200.0
    assert block["count_ok"] == 3
    assert block["count_no_reference"] == 2
    assert block["count_total"] == 5
    assert block["no_reference_pct"] == 40.0
    assert block["window_days"] == 30
    assert payload["meta"]["earliness_vs_trending_is_visual_context_only"] is True


async def test_earliness_median_null_when_all_no_reference(db):
    d, path = db
    await _insert_opened_trade_with_leadtime(
        d._conn, token_id="nr1", lead_min=None, lead_status="no_reference"
    )
    await _insert_opened_trade_with_leadtime(
        d._conn, token_id="nr2", lead_min=None, lead_status="no_reference"
    )
    payload = await ddb.get_todays_focus(path)
    _assert_contract(payload)
    block = payload["meta"]["earliness_vs_trending"]
    assert block["median_lead_time_min"] is None
    assert block["count_ok"] == 0
    assert block["no_reference_pct"] == 100.0


async def test_earliness_negative_median_is_early(db):
    d, path = db
    await _insert_opened_trade_with_leadtime(
        d._conn, token_id="early1", lead_min=-500.0, lead_status="ok"
    )
    payload = await ddb.get_todays_focus(path)
    _assert_contract(payload)
    block = payload["meta"]["earliness_vs_trending"]
    assert block["median_lead_time_min"] == -500.0
    assert block["count_ok"] == 1
    assert block["no_reference_pct"] == 0.0


async def test_earliness_excludes_trades_outside_30d_window(db):
    d, path = db
    await _insert_opened_trade_with_leadtime(
        d._conn,
        token_id="stale",
        lead_min=100.0,
        lead_status="ok",
        opened_days_ago=45.0,
    )
    payload = await ddb.get_todays_focus(path)
    assert "earliness_vs_trending" not in payload["meta"]
