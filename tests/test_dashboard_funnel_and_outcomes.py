"""DASH-01 (dispatch funnel) + DASH-06 (alert->outcome linkage) API tests.

DASH-01: /api/dispatch_funnel aggregates trade_decision_events by
(decision, reason) over a rolling ?days window (default 1), surfacing the
"why nothing fired" block-reason split plus the opened count.

DASH-06: /api/tg_alerts/recent joins tg_alert_log.paper_trade_id ->
paper_trades to render each sent alert's realized outcome inline; unlinked
rows carry an explicit 'unlinked' tag (never blank).

Both surfaces are read-only visibility only — they classify/rank/dispatch
nothing.

NOTE (Windows CI): these DB + httpx.ASGITransport tests crash the native
sqlite/loop stack on Windows (INF-08, same class as the existing
test_dashboard_cockpit_slice1.py suite) — run under Linux CI. The SQL/logic
was additionally verified locally against a real Database via a standalone
asyncio harness.
"""

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


async def _insert_decision(conn, decision, reason, *, hours_ago=1, token="tok"):
    created = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    await conn.execute(
        """INSERT INTO trade_decision_events
           (token_id, signal_type, decision, reason, source_module,
            signal_combo, paper_trade_id, event_data, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token,
            "volume_spike",
            decision,
            reason,
            "scout.trading.signals",
            None,
            None,
            "{}",
            created,
        ),
    )
    await conn.commit()


async def _insert_paper_trade(
    conn,
    token_id,
    *,
    status="closed_tp",
    pnl_usd=45.0,
    pnl_pct=15.0,
    exit_reason="tp",
    peak_pct=30.0,
):
    now = datetime.now(timezone.utc)
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, pnl_usd, pnl_pct, exit_reason, peak_pct, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            token_id.upper(),
            token_id.title(),
            "coingecko",
            "volume_spike",
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
            pnl_pct,
            exit_reason,
            peak_pct,
            (now - timedelta(hours=6)).isoformat(),
        ),
    )
    await conn.commit()
    cur = await conn.execute(
        "SELECT id FROM paper_trades WHERE token_id = ?", (token_id,)
    )
    return (await cur.fetchone())["id"]


async def _insert_tg_alert(
    conn, paper_trade_id, token_id, *, outcome="sent", hours_ago=1
):
    alerted = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    await conn.execute(
        """INSERT INTO tg_alert_log
           (paper_trade_id, signal_type, token_id, alerted_at, outcome, detail)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (paper_trade_id, "volume_spike", token_id, alerted, outcome, None),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# DASH-01 — dispatch funnel "why nothing fired"
# ---------------------------------------------------------------------------


async def test_dispatch_funnel_reason_breakdown(client):
    c, d = client
    await _insert_decision(d._conn, "opened", "paper_trade_opened", hours_ago=1)
    await _insert_decision(d._conn, "opened", "paper_trade_opened", hours_ago=2)
    await _insert_decision(d._conn, "blocked", "suppressed", hours_ago=1)
    await _insert_decision(d._conn, "blocked", "suppressed", hours_ago=3)
    await _insert_decision(d._conn, "blocked", "suppressed", hours_ago=5)
    await _insert_decision(d._conn, "blocked", "signal_disabled", hours_ago=2)
    await _insert_decision(d._conn, "blocked", "below_min_market_cap", hours_ago=4)
    # outside the default 1-day window — must be excluded
    await _insert_decision(d._conn, "blocked", "late_pump", hours_ago=72)

    resp = await c.get("/api/dispatch_funnel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["window_days"] == 1
    assert data["opened"] == 2
    assert data["blocked"] == 5
    assert data["total_events"] == 7
    assert data["meta"]["read_only"] is True

    by_reason = {(r["decision"], r["reason"]): r["count"] for r in data["reasons"]}
    assert by_reason[("blocked", "suppressed")] == 3
    assert by_reason[("blocked", "signal_disabled")] == 1
    assert by_reason[("blocked", "below_min_market_cap")] == 1
    assert by_reason[("opened", "paper_trade_opened")] == 2
    assert ("blocked", "late_pump") not in by_reason
    # ordered by count desc
    counts = [r["count"] for r in data["reasons"]]
    assert counts == sorted(counts, reverse=True)


async def test_dispatch_funnel_window_widens_with_days(client):
    c, d = client
    await _insert_decision(d._conn, "blocked", "suppressed", hours_ago=1)
    await _insert_decision(d._conn, "blocked", "late_pump", hours_ago=72)

    day1 = (await c.get("/api/dispatch_funnel?days=1")).json()
    assert day1["total_events"] == 1

    day7 = (await c.get("/api/dispatch_funnel?days=7")).json()
    assert day7["window_days"] == 7
    assert day7["total_events"] == 2


async def test_dispatch_funnel_empty_window(client):
    c, _ = client
    resp = await c.get("/api/dispatch_funnel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["opened"] == 0
    assert data["blocked"] == 0
    assert data["total_events"] == 0
    assert data["reasons"] == []


# ---------------------------------------------------------------------------
# DASH-06 — sent alert -> paper trade outcome linkage
# ---------------------------------------------------------------------------


async def test_tg_alerts_recent_joins_paper_trade_outcome(client):
    c, d = client
    win_id = await _insert_paper_trade(
        d._conn,
        "winner",
        status="closed_tp",
        pnl_usd=45.0,
        pnl_pct=15.0,
        exit_reason="tp",
        peak_pct=30.0,
    )
    open_id = await _insert_paper_trade(
        d._conn,
        "openpos",
        status="open",
        pnl_usd=None,
        pnl_pct=None,
        exit_reason=None,
        peak_pct=8.0,
    )
    await _insert_tg_alert(d._conn, win_id, "winner", hours_ago=5)
    await _insert_tg_alert(d._conn, open_id, "openpos", hours_ago=4)
    await _insert_tg_alert(d._conn, None, "unlinked_tok", hours_ago=3)
    # a non-sent row must never appear
    await _insert_tg_alert(
        d._conn, None, "blocked_tok", outcome="blocked_eligibility", hours_ago=2
    )

    resp = await c.get("/api/tg_alerts/recent")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["outcome_linkage_available"] is True
    assert body["meta"]["rows_linked"] == 2

    by_token = {a["token_id"]: a for a in body["alerts"]}
    assert "blocked_tok" not in by_token

    win = by_token["winner"]["outcome"]
    assert win["linked"] is True
    assert win["state"] == "closed"
    assert win["pnl_usd"] == pytest.approx(45.0)
    assert win["pnl_pct"] == pytest.approx(15.0)
    assert win["exit_reason"] == "tp"
    assert win["peak_pct"] == pytest.approx(30.0)

    opened = by_token["openpos"]["outcome"]
    assert opened["linked"] is True
    assert opened["state"] == "open"
    assert opened["pnl_usd"] is None
    assert opened["peak_pct"] == pytest.approx(8.0)

    unlinked = by_token["unlinked_tok"]["outcome"]
    assert unlinked == {"linked": False, "state": "unlinked"}


# ---------------------------------------------------------------------------
# Frontend copy-firewall — same text-level pattern as the cockpit-slice tests
# ---------------------------------------------------------------------------


def _read_component(name):
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    return (root / "dashboard" / "frontend" / "components" / name).read_text(
        encoding="utf-8"
    )


def _read_frontend(name):
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    return (root / "dashboard" / "frontend" / name).read_text(encoding="utf-8")


def test_dispatch_funnel_panel_renders_reasons():
    jsx = _read_component("DispatchFunnelPanel.jsx")
    assert "Why nothing fired" in jsx
    assert "/api/dispatch_funnel" in jsx
    assert "opened" in jsx
    assert "Block reason" in jsx
    # plain-words map for the raw engine reason keys
    assert "below_min_market_cap" in jsx
    assert "suppressed" in jsx


def test_pipeline_tab_wires_dispatch_funnel_panel():
    app = _read_frontend("App.jsx")
    assert "DispatchFunnelPanel" in app


def test_tg_alerts_tab_renders_outcome_column():
    jsx = _read_component("TGAlertsTab.jsx")
    assert "OutcomeCell" in jsx
    assert "a.outcome" in jsx
    assert "<th>Outcome</th>" in jsx
    # unlinked rows carry an explicit tag, never blank
    assert "unlinked" in jsx
