"""Tests for BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE — internal HMAC-authed
operator-alert endpoint.

Covers (per assignment's required test surface):

- Auth-failure paths (missing headers / bad signature / replay / disabled)
- Delivery success (HMAC ok + alerter returns ok -> 200 + log triplet)
- Delivery failure (HMAC ok + alerter raises -> 502 + failed log)
- No secret leakage in any log output across all paths

Run: uv run pytest tests/test_internal_alert_api.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
import structlog
from httpx import ASGITransport, AsyncClient

from dashboard.api import create_app
from scout.config import Settings

_HMAC_SECRET = "x" * 64  # 32-byte hex; arbitrary fixed value for tests


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_replay_lru():
    """Replay-LRU is process-scoped; reset between tests."""
    from scout.api import narrative as _narrative_mod

    _narrative_mod._replay_seen.clear()
    yield
    _narrative_mod._replay_seen.clear()


def _make_settings(hmac_secret: str) -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test_bot_token_value",
        TELEGRAM_CHAT_ID="test_chat_id",
        ANTHROPIC_API_KEY="test_anthropic_key",
        NARRATIVE_SCANNER_HMAC_SECRET=hmac_secret,
    )


@pytest.fixture
async def client_with_secret(tmp_path):
    """Client with HMAC secret configured."""
    import dashboard.api as api_mod

    api_mod._DASHBOARD_SETTINGS = _make_settings(_HMAC_SECRET)
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None

    db_path = tmp_path / "test.db"
    app = create_app(db_path=str(db_path))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


@pytest.fixture
async def client_no_secret(tmp_path):
    """Client with empty HMAC secret (feature gated off)."""
    import dashboard.api as api_mod

    api_mod._DASHBOARD_SETTINGS = _make_settings("")
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None

    db_path = tmp_path / "test.db"
    app = create_app(db_path=str(db_path))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign(secret: str, method: str, path: str, ts: str, body: bytes) -> str:
    canonical = f"{method}\n{path}\n\n{ts}\n".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def _valid_payload() -> dict:
    return {
        "message": "narrative_dispatcher_misconfig: NARRATIVE_SCANNER_HMAC_SECRET unset",
        "source": "narrative_dispatcher",
    }


async def _post_signed(client: AsyncClient, payload: dict, secret: str = _HMAC_SECRET):
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign(secret, "POST", "/api/internal/operator-alert", ts, body)
    return await client.post(
        "/api/internal/operator-alert",
        content=body,
        headers={
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
    )


# ---------------------------------------------------------------------------
# Auth-failure paths
# ---------------------------------------------------------------------------


async def test_disabled_503_when_secret_empty(client_no_secret):
    """Empty NARRATIVE_SCANNER_HMAC_SECRET → 503 (feature gate)."""
    resp = await client_no_secret.post(
        "/api/internal/operator-alert", json=_valid_payload()
    )
    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"].lower()


async def test_missing_headers_401(client_with_secret):
    """No X-Timestamp / X-Signature → 401."""
    resp = await client_with_secret.post(
        "/api/internal/operator-alert", json=_valid_payload()
    )
    assert resp.status_code == 401
    assert "missing" in resp.json()["detail"].lower()


async def test_bad_signature_403(client_with_secret):
    """Wrong signature → 403 (constant-time HMAC mismatch)."""
    body = json.dumps(_valid_payload()).encode("utf-8")
    ts = str(int(time.time()))
    bad_sig = "0" * 64
    resp = await client_with_secret.post(
        "/api/internal/operator-alert",
        content=body,
        headers={
            "X-Timestamp": ts,
            "X-Signature": bad_sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 403
    assert "mismatch" in resp.json()["detail"].lower()


async def test_replay_409(client_with_secret, monkeypatch):
    """Same (timestamp, signature) replayed → 409.

    Reviewer 1 P2 fold: monkeypatch the alerter to a no-op so the first call
    is deterministically 200, and the assertion that the second identical
    call returns 409 is independent of any network / Telegram-token state.
    """

    async def _fake_send(text, session, settings, **kwargs):
        pass  # deterministic no-op delivery

    monkeypatch.setattr("scout.api.internal_alert.send_telegram_message", _fake_send)

    payload = _valid_payload()
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign(_HMAC_SECRET, "POST", "/api/internal/operator-alert", ts, body)
    headers = {
        "X-Timestamp": ts,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }

    # First call: 200 (HMAC ok + alerter mocked no-op).
    resp1 = await client_with_secret.post(
        "/api/internal/operator-alert", content=body, headers=headers
    )
    assert resp1.status_code == 200, resp1.text

    # Second identical call: replay-cache hit at the HMAC layer → 409
    # BEFORE the alerter is reached again.
    resp2 = await client_with_secret.post(
        "/api/internal/operator-alert", content=body, headers=headers
    )
    assert resp2.status_code == 409
    assert "replay" in resp2.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Delivery paths (alerter mocked)
# ---------------------------------------------------------------------------


async def test_delivery_success_200(client_with_secret, monkeypatch):
    """HMAC ok + alerter returns → 200, log triplet emits dispatched + delivered."""
    calls: list[dict] = []

    async def _fake_send(text, session, settings, **kwargs):
        calls.append({"text": text, "kwargs": kwargs})

    monkeypatch.setattr("scout.api.internal_alert.send_telegram_message", _fake_send)

    resp = await _post_signed(client_with_secret, _valid_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "delivered"
    assert body["source"] == "narrative_dispatcher"

    # Alerter was called exactly once with parse_mode=None (§2.9 hygiene)
    assert len(calls) == 1
    assert calls[0]["kwargs"]["parse_mode"] is None
    assert calls[0]["kwargs"]["raise_on_failure"] is True
    assert "narrative_dispatcher" in calls[0]["kwargs"]["source"]
    assert calls[0]["text"] == _valid_payload()["message"]


async def test_delivery_failure_502(client_with_secret, monkeypatch):
    """HMAC ok + alerter raises → 502, *_failed log emitted."""

    async def _fake_send_fails(text, session, settings, **kwargs):
        raise RuntimeError("telegram http 500")

    monkeypatch.setattr(
        "scout.api.internal_alert.send_telegram_message", _fake_send_fails
    )

    resp = await _post_signed(client_with_secret, _valid_payload())
    assert resp.status_code == 502, resp.text
    assert "delivery_failed" in resp.json()["detail"].lower() or "telegram" in resp.json()["detail"].lower()


async def test_invalid_payload_400(client_with_secret, monkeypatch):
    """HMAC ok + bad payload shape → 400 BEFORE alerter is called."""
    calls: list[dict] = []

    async def _fake_send(text, session, settings, **kwargs):
        calls.append({"text": text, "kwargs": kwargs})

    monkeypatch.setattr("scout.api.internal_alert.send_telegram_message", _fake_send)

    # Missing required `source` field.
    bad_payload = {"message": "hello"}
    resp = await _post_signed(client_with_secret, bad_payload)
    assert resp.status_code == 400
    # Alerter not called when payload shape fails.
    assert calls == []


# ---------------------------------------------------------------------------
# Log triplet (§12b)
# ---------------------------------------------------------------------------


async def test_log_triplet_on_success(client_with_secret, monkeypatch):
    """*_dispatched fires before delivery; *_delivered fires after."""

    async def _fake_send(text, session, settings, **kwargs):
        pass  # success

    monkeypatch.setattr("scout.api.internal_alert.send_telegram_message", _fake_send)

    with structlog.testing.capture_logs() as captured:
        resp = await _post_signed(client_with_secret, _valid_payload())
        assert resp.status_code == 200

    events = [r["event"] for r in captured]
    assert "operator_alert_dispatched" in events
    assert "operator_alert_delivered" in events
    # dispatched must precede delivered
    assert events.index("operator_alert_dispatched") < events.index(
        "operator_alert_delivered"
    )
    # No *_failed in success path
    assert "operator_alert_failed" not in events


async def test_log_triplet_on_failure(client_with_secret, monkeypatch):
    """*_dispatched fires; *_failed fires on alerter exception; no *_delivered."""

    async def _fake_send_fails(text, session, settings, **kwargs):
        raise RuntimeError("telegram http 500")

    monkeypatch.setattr(
        "scout.api.internal_alert.send_telegram_message", _fake_send_fails
    )

    with structlog.testing.capture_logs() as captured:
        resp = await _post_signed(client_with_secret, _valid_payload())
        assert resp.status_code == 502

    events = [r["event"] for r in captured]
    assert "operator_alert_dispatched" in events
    assert "operator_alert_failed" in events
    assert "operator_alert_delivered" not in events


# ---------------------------------------------------------------------------
# Secret-leakage tests (assignment-required)
# ---------------------------------------------------------------------------


def _assert_no_secret_in(records, secret: str = _HMAC_SECRET) -> None:
    """Scan all captured log records for any occurrence of the HMAC secret
    (or its first 16 chars — a partial leak is still a leak)."""
    secret_prefix = secret[:16]
    for r in records:
        text = json.dumps(r, default=str)
        assert secret not in text, (
            f"HMAC secret leaked into log record: event={r.get('event')}"
        )
        assert secret_prefix not in text, (
            f"HMAC secret prefix leaked into log record: event={r.get('event')}"
        )


async def test_no_secret_leak_on_success(client_with_secret, monkeypatch):
    async def _fake_send(text, session, settings, **kwargs):
        pass

    monkeypatch.setattr("scout.api.internal_alert.send_telegram_message", _fake_send)
    with structlog.testing.capture_logs() as captured:
        resp = await _post_signed(client_with_secret, _valid_payload())
        assert resp.status_code == 200
    _assert_no_secret_in(captured)


async def test_no_secret_leak_on_auth_failure(client_with_secret):
    """Missing-header / bad-sig paths don't leak the secret either."""
    with structlog.testing.capture_logs() as captured:
        resp = await client_with_secret.post(
            "/api/internal/operator-alert", json=_valid_payload()
        )
        assert resp.status_code == 401
    _assert_no_secret_in(captured)


async def test_no_secret_leak_on_delivery_failure(client_with_secret, monkeypatch):
    """*_failed log includes err message but never the HMAC secret."""

    async def _fake_send_fails(text, session, settings, **kwargs):
        raise RuntimeError("telegram http 500")

    monkeypatch.setattr(
        "scout.api.internal_alert.send_telegram_message", _fake_send_fails
    )
    with structlog.testing.capture_logs() as captured:
        resp = await _post_signed(client_with_secret, _valid_payload())
        assert resp.status_code == 502
    _assert_no_secret_in(captured)


async def test_no_secret_leak_on_disabled(client_no_secret):
    """503 path doesn't leak anything either (defensive — secret is empty
    on this path, but make sure no later refactor introduces leaks)."""
    with structlog.testing.capture_logs() as captured:
        resp = await client_no_secret.post(
            "/api/internal/operator-alert", json=_valid_payload()
        )
        assert resp.status_code == 503
    _assert_no_secret_in(captured)
