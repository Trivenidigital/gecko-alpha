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
_NARRATIVE_HMAC_SECRET = "n" * 64  # distinct from operator-alert secret
_OPERATOR_HMAC_SECRET = "o" * 64  # distinct from narrative secret


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


def _make_settings(
    *,
    narrative_secret: str = "",
    operator_secret: str = "",
) -> Settings:
    """Construct Settings with explicit per-endpoint HMAC secrets.

    Reviewer 1 P1 fold: the two endpoints authenticate independently.
    Test helpers must therefore set each secret explicitly so coverage
    of gate-independence cases is unambiguous.
    """
    return Settings(
        TELEGRAM_BOT_TOKEN="test_bot_token_value",
        TELEGRAM_CHAT_ID="test_chat_id",
        ANTHROPIC_API_KEY="test_anthropic_key",
        NARRATIVE_SCANNER_HMAC_SECRET=narrative_secret,
        OPERATOR_ALERT_HMAC_SECRET=operator_secret,
    )


@pytest.fixture
async def client_with_secret(tmp_path):
    """Client with OPERATOR_ALERT_HMAC_SECRET set (the secret used by the
    internal-alert endpoint). Narrative secret also set to the same value
    by default so callers that sign with ``_HMAC_SECRET`` keep working.
    """
    import dashboard.api as api_mod

    api_mod._DASHBOARD_SETTINGS = _make_settings(
        narrative_secret=_HMAC_SECRET, operator_secret=_HMAC_SECRET
    )
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
    """Client with BOTH secrets empty (both endpoints gated off)."""
    import dashboard.api as api_mod

    api_mod._DASHBOARD_SETTINGS = _make_settings(
        narrative_secret="", operator_secret=""
    )
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
async def client_narrative_empty_operator_set(tmp_path):
    """Reviewer 1 P1 fold: narrative secret empty, operator-alert secret set.
    Smoke-test the documented failure mode — the dispatcher must still be
    able to deliver an operator alert when narrative ingestion is broken
    by a missing secret."""
    import dashboard.api as api_mod

    api_mod._DASHBOARD_SETTINGS = _make_settings(
        narrative_secret="", operator_secret=_OPERATOR_HMAC_SECRET
    )
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
async def client_operator_secret_empty(tmp_path):
    """Operator-alert secret empty (narrative secret set). The internal-alert
    endpoint must 503 regardless of narrative-side configuration."""
    import dashboard.api as api_mod

    api_mod._DASHBOARD_SETTINGS = _make_settings(
        narrative_secret=_NARRATIVE_HMAC_SECRET, operator_secret=""
    )
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


# ---------------------------------------------------------------------------
# Gate-independence tests (Reviewer 1 P1 fold)
#
# The internal-alert endpoint must authenticate independently of the
# narrative endpoint. The exact failure mode this endpoint exists to
# surface is "operator forgot to set NARRATIVE_SCANNER_HMAC_SECRET" — so
# if both endpoints gated on the same secret, the dispatcher would 503
# on the operator-alert path during the very failure it's trying to
# alert about.
# ---------------------------------------------------------------------------


def _sign_with(
    secret: str, method: str, path: str, ts: str, body: bytes
) -> str:
    """Sign with a caller-supplied secret (so tests can use the operator
    secret instead of the default _HMAC_SECRET)."""
    canonical = f"{method}\n{path}\n\n{ts}\n".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


async def _post_signed_with(
    client: AsyncClient, payload: dict, secret: str
):
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign_with(secret, "POST", "/api/internal/operator-alert", ts, body)
    return await client.post(
        "/api/internal/operator-alert",
        content=body,
        headers={
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
    )


async def test_operator_alert_works_when_narrative_secret_empty(
    client_narrative_empty_operator_set, monkeypatch
):
    """The smoke-test path: NARRATIVE_SCANNER_HMAC_SECRET is unset (narrative
    ingestion is gated off / would 503) but OPERATOR_ALERT_HMAC_SECRET is
    configured, so the dispatcher can still raise a Telegram alert for the
    very failure mode this endpoint exists to surface.
    """
    calls: list[dict] = []

    async def _fake_send(text, session, settings, **kwargs):
        calls.append({"text": text, "kwargs": kwargs})

    monkeypatch.setattr("scout.api.internal_alert.send_telegram_message", _fake_send)

    resp = await _post_signed_with(
        client_narrative_empty_operator_set,
        _valid_payload(),
        secret=_OPERATOR_HMAC_SECRET,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "delivered"
    assert len(calls) == 1


async def test_operator_alert_503_when_its_own_secret_empty(
    client_operator_secret_empty,
):
    """Independent gate: even with NARRATIVE_SCANNER_HMAC_SECRET set, an
    empty OPERATOR_ALERT_HMAC_SECRET must 503 the internal-alert endpoint.
    """
    # Sign with the narrative secret — it shouldn't matter, the gate is
    # the operator-alert secret which is empty.
    resp = await _post_signed_with(
        client_operator_secret_empty,
        _valid_payload(),
        secret=_NARRATIVE_HMAC_SECRET,
    )
    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"].lower()
    assert "internal_alert" in resp.json()["detail"].lower()


async def test_operator_alert_503_detail_does_not_say_narrative(
    client_operator_secret_empty,
):
    """The 503 detail must say ``internal_alert: feature disabled`` (not
    ``narrative_scanner``) so operator forensics know which secret to set.
    """
    resp = await _post_signed_with(
        client_operator_secret_empty,
        _valid_payload(),
        secret=_NARRATIVE_HMAC_SECRET,
    )
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "internal_alert" in detail.lower()
    assert "narrative_scanner" not in detail.lower()


async def test_narrative_endpoint_still_uses_narrative_secret_by_default(
    client_narrative_empty_operator_set,
):
    """Regression: the parameterized _verify_hmac default must preserve
    narrative-router behavior. With narrative secret empty, narrative-side
    endpoint should still 503 — same as before the refactor."""
    resp = await client_narrative_empty_operator_set.post(
        "/api/narrative-alert", json={"placeholder": "ignored"}
    )
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "narrative_scanner" in detail.lower()
