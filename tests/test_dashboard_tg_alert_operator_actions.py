"""Dashboard API tests for TG alert operator-action telemetry."""

from __future__ import annotations

import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from scout.db import Database

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason="Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp/httpx tests",
)


@pytest.fixture(autouse=True)
async def _reset_dashboard_module_state():
    import dashboard.api as dash_api

    dash_api._scout_db = None
    dash_api._db_path = "scout.db"
    yield
    if dash_api._scout_db is not None:
        await dash_api._scout_db.close()
    dash_api._scout_db = None
    dash_api._db_path = "scout.db"


async def _seed_alerts(db_path: str) -> tuple[int, int]:
    db = Database(db_path)
    await db.initialize()
    cur = await db._conn.execute(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
        "VALUES (NULL, 'narrative_prediction', 'bonk', "
        "'2026-05-31T11:00:00+00:00', 'sent', 'delivered')"
    )
    sent_id = cur.lastrowid
    cur = await db._conn.execute(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
        "VALUES (NULL, 'chain_completed', 'pepe', "
        "'2026-05-31T11:05:00+00:00', 'blocked_eligibility', 'blocked')"
    )
    blocked_id = cur.lastrowid
    await db._conn.commit()
    await db.close()
    return sent_id, blocked_id


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_recent_tg_alerts_returns_sent_alerts_with_operator_action(tmp_path):
    db_path = str(tmp_path / "test.db")
    sent_id, _blocked_id = await _seed_alerts(db_path)

    from dashboard.api import create_app

    app = create_app(db_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        post = await client.post(
            f"/api/tg_alerts/{sent_id}/operator-action",
            json={"action": "useful", "note": "worth reviewing"},
        )
        assert post.status_code == 200
        assert post.json()["action"] == "useful"

        resp = await client.get("/api/tg_alerts/recent?limit=20")
        assert resp.status_code == 200
        payload = resp.json()

    assert payload["meta"]["read_only"] is True
    assert payload["meta"]["not_for_alerting"] is True
    assert [row["id"] for row in payload["alerts"]] == [sent_id]
    assert payload["alerts"][0]["operator_action"]["action"] == "useful"
    assert payload["alerts"][0]["operator_action"]["note"] == "worth reviewing"


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_operator_action_endpoint_rejects_unknown_alert_and_bad_action(tmp_path):
    db_path = str(tmp_path / "test.db")
    sent_id, _blocked_id = await _seed_alerts(db_path)

    from dashboard.api import create_app

    app = create_app(db_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        bad_action = await client.post(
            f"/api/tg_alerts/{sent_id}/operator-action",
            json={"action": "trade_now"},
        )
        missing = await client.post(
            "/api/tg_alerts/999999/operator-action",
            json={"action": "ignored"},
        )

    assert bad_action.status_code == 422
    assert missing.status_code == 404
