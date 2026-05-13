"""Tests for BL-NEW-NARRATIVE-SCANNER V1 — cross-VPS Hermes integration endpoints.

Covers:
- HMAC scheme correctness (canonical-string, timestamp window, replay LRU)
- Feature-gate via empty NARRATIVE_SCANNER_HMAC_SECRET → 503
- POST /api/narrative-alert idempotency on event_id
- GET /api/coin/lookup CA-only resolution
- Payload validation (Pydantic shape)

Run: uv run pytest tests/test_narrative_scanner_api.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.api import create_app
from scout.config import Settings
from scout.db import Database

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_replay_lru():
    """Replay-LRU is process-scoped; reset between tests to avoid cross-test
    contamination (two tests in same second emit identical (ts, sig) → 409)."""
    from scout.api import narrative as _narrative_mod

    _narrative_mod._replay_seen.clear()
    yield
    _narrative_mod._replay_seen.clear()


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    d = Database(db_path)
    await d.initialize()
    yield d, str(db_path)
    await d.close()


def _make_settings(hmac_secret: str) -> Settings:
    """Construct a Settings with required fields stubbed (per conftest.py pattern)."""
    return Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        NARRATIVE_SCANNER_HMAC_SECRET=hmac_secret,
    )


@pytest.fixture
async def client_with_secret(db):
    """Client + DB with HMAC secret configured (feature enabled)."""
    secret = "x" * 64  # 32-byte hex
    import dashboard.api as api_mod

    api_mod._DASHBOARD_SETTINGS = _make_settings(secret)

    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None

    d, db_path = db
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, d, secret

    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


@pytest.fixture
async def client_no_secret(db):
    """Client + DB with NO HMAC secret (feature gated off)."""
    import dashboard.api as api_mod

    api_mod._DASHBOARD_SETTINGS = _make_settings("")

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign(
    secret: str, method: str, path: str, ts: str, body: bytes, query: str = ""
) -> str:
    """Canonical: METHOD\\n PATH\\n QUERY\\n TIMESTAMP\\n BODY.

    PR-V2 reviewer B-C1 fold: query string is included so GET signatures bind
    their query params. Test helper updated to match.
    """
    canonical = f"{method}\n{path}\n{query}\n{ts}\n".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def _valid_alert_payload(event_id: str | None = None) -> dict:
    # V2-PR-review A-N4 fold: event_id pinned to 64-char sha256 hex.
    if event_id is None:
        event_id = "a" * 64
    elif len(event_id) != 64:
        # Pad/truncate to 64 chars (sha256 hex)
        event_id = (event_id + "0" * 64)[:64]
    return {
        "event_id": event_id,
        "tweet_id": "1834567890",
        "tweet_author": "elonmusk",
        "tweet_ts": "2026-05-13T01:00:00Z",
        "tweet_text": "Goblins are back. $GOBLIN",
        "tweet_text_hash": "deadbeef" * 8,
        "extracted_cashtag": "$GOBLIN",
        "extracted_ca": "FoMoLanaJzCFkUEcVTbgScfhUC6axpkvfFV3KGNVpump",
        "extracted_chain": "solana",
        "resolved_coin_id": None,
        "narrative_theme": "meme-revival",
        "urgency_signal": "rumor",
        "classifier_confidence": 0.82,
        "classifier_version": "kimi-k2:v1",
    }


# ---------------------------------------------------------------------------
# Feature-gate tests
# ---------------------------------------------------------------------------


async def test_endpoint_503_when_secret_empty_lookup(client_no_secret):
    """Vector A FC-2 / §3: feature gated by empty HMAC secret → 503."""
    c, _ = client_no_secret
    resp = await c.get("/api/coin/lookup?ca=Foo&chain=solana")
    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"].lower()


async def test_endpoint_503_when_secret_empty_post(client_no_secret):
    c, _ = client_no_secret
    resp = await c.post("/api/narrative-alert", json=_valid_alert_payload())
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# HMAC scheme tests (canonical string, timestamp window, replay LRU)
# ---------------------------------------------------------------------------


async def test_post_succeeds_with_valid_hmac(client_with_secret):
    c, _, secret = client_with_secret
    payload = _valid_alert_payload()
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign(secret, "POST", "/api/narrative-alert", ts, body)
    resp = await c.post(
        "/api/narrative-alert",
        content=body,
        headers={
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200, resp.text
    body_out = resp.json()
    assert body_out["status"] == "created"
    assert "id" in body_out


async def test_post_rejects_missing_headers(client_with_secret):
    c, _, _ = client_with_secret
    resp = await c.post("/api/narrative-alert", json=_valid_alert_payload())
    assert resp.status_code == 401
    assert "missing" in resp.json()["detail"].lower()


async def test_post_rejects_stale_timestamp(client_with_secret):
    """§3: replay window enforced server-side (default 300s)."""
    c, _, secret = client_with_secret
    payload = _valid_alert_payload()
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()) - 10_000)  # 10000s in past = way outside 300s window
    sig = _sign(secret, "POST", "/api/narrative-alert", ts, body)
    resp = await c.post(
        "/api/narrative-alert",
        content=body,
        headers={
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401
    assert "replay window" in resp.json()["detail"].lower()


async def test_post_rejects_tampered_body(client_with_secret):
    """§3: signature over canonical-string includes body. Tamper → signature mismatch."""
    c, _, secret = client_with_secret
    payload = _valid_alert_payload()
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign(secret, "POST", "/api/narrative-alert", ts, body)
    # Tamper the body but reuse the signature
    tampered = body.replace(b"GOBLIN", b"HACKED")
    resp = await c.post(
        "/api/narrative-alert",
        content=tampered,
        headers={
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 403
    assert "mismatch" in resp.json()["detail"].lower()


async def test_post_rejects_wrong_secret(client_with_secret):
    c, _, _ = client_with_secret
    payload = _valid_alert_payload()
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign("wrong-secret", "POST", "/api/narrative-alert", ts, body)
    resp = await c.post(
        "/api/narrative-alert",
        content=body,
        headers={
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 403


async def test_post_replay_rejected(client_with_secret):
    """§3: replay-cache rejects duplicate (timestamp, signature) within window."""
    c, _, secret = client_with_secret
    payload = _valid_alert_payload(event_id="replay" + "x" * 30)
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign(secret, "POST", "/api/narrative-alert", ts, body)
    headers = {
        "X-Timestamp": ts,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }
    # First call succeeds
    resp1 = await c.post("/api/narrative-alert", content=body, headers=headers)
    assert resp1.status_code == 200
    # Second call with same headers → replay reject
    resp2 = await c.post("/api/narrative-alert", content=body, headers=headers)
    assert resp2.status_code == 409
    assert "replay-cache" in resp2.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Idempotency tests (event_id UNIQUE)
# ---------------------------------------------------------------------------


async def test_post_idempotent_on_event_id(client_with_secret):
    """Vector B HLD-2: same event_id → duplicate status, no second row."""
    c, _, secret = client_with_secret
    payload = _valid_alert_payload(event_id="dedupe" + "y" * 30)

    # First call
    body = json.dumps(payload).encode("utf-8")
    ts1 = str(int(time.time()))
    sig1 = _sign(secret, "POST", "/api/narrative-alert", ts1, body)
    resp1 = await c.post(
        "/api/narrative-alert",
        content=body,
        headers={
            "X-Timestamp": ts1,
            "X-Signature": sig1,
            "Content-Type": "application/json",
        },
    )
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "created"

    # Second call with same event_id but different ts (so replay-LRU doesn't catch it)
    await __import__("asyncio").sleep(1)
    ts2 = str(int(time.time()) + 1)
    sig2 = _sign(secret, "POST", "/api/narrative-alert", ts2, body)
    resp2 = await c.post(
        "/api/narrative-alert",
        content=body,
        headers={
            "X-Timestamp": ts2,
            "X-Signature": sig2,
            "Content-Type": "application/json",
        },
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "duplicate"


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


async def test_post_rejects_malformed_payload(client_with_secret):
    c, _, secret = client_with_secret
    bad_payload = {"event_id": "x"}  # missing required fields
    body = json.dumps(bad_payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign(secret, "POST", "/api/narrative-alert", ts, body)
    resp = await c.post(
        "/api/narrative-alert",
        content=body,
        headers={
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400
    assert "invalid payload" in resp.json()["detail"].lower()


async def test_post_rejects_invalid_confidence(client_with_secret):
    c, _, secret = client_with_secret
    payload = _valid_alert_payload(event_id="confbad" + "z" * 30)
    payload["classifier_confidence"] = 1.5  # out of [0, 1]
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign(secret, "POST", "/api/narrative-alert", ts, body)
    resp = await c.post(
        "/api/narrative-alert",
        content=body,
        headers={
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/coin/lookup tests
# ---------------------------------------------------------------------------


async def test_lookup_unknown_ca_returns_found_false(client_with_secret):
    """Unknown CA → found=false with reason='not_found' (V2-PR-review C-SFC2)."""
    c, _, secret = client_with_secret
    ca = "FoMoLanaJzCFkUEcVTbgScfhUC6axpkvfFV3KGNVpump"
    ts = str(int(time.time()))
    query = f"ca={ca}&chain=solana"
    sig = _sign(secret, "GET", "/api/coin/lookup", ts, b"", query=query)
    resp = await c.get(
        f"/api/coin/lookup?{query}",
        headers={"X-Timestamp": ts, "X-Signature": sig},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["found"] is False
    assert data["reason"] == "not_found"


async def test_lookup_rejects_bad_chain(client_with_secret):
    c, _, secret = client_with_secret
    ts = str(int(time.time()))
    query = "ca=Foo123Bar456&chain=bitcoin"
    sig = _sign(secret, "GET", "/api/coin/lookup", ts, b"", query=query)
    resp = await c.get(
        f"/api/coin/lookup?{query}",
        headers={"X-Timestamp": ts, "X-Signature": sig},
    )
    assert resp.status_code == 400
    assert "chain must be one of" in resp.json()["detail"].lower()


async def test_lookup_rejects_bad_ca_shape(client_with_secret):
    c, _, secret = client_with_secret
    ts = str(int(time.time()))
    query = "ca=tiny&chain=solana"
    sig = _sign(secret, "GET", "/api/coin/lookup", ts, b"", query=query)
    resp = await c.get(
        f"/api/coin/lookup?{query}",
        headers={"X-Timestamp": ts, "X-Signature": sig},
    )
    assert resp.status_code == 400


async def test_lookup_finds_known_ca(client_with_secret):
    """Inserts a candidate row directly, then verifies lookup resolves it."""
    c, db, secret = client_with_secret
    ca = "0x" + "a" * 40
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO candidates (
            contract_address, chain, token_name, ticker, market_cap_usd,
            liquidity_usd, volume_24h_usd, first_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ca,
            "ethereum",
            "AstoridFantasy",
            "ASTORID",
            50_000_000,
            1_500_000,
            8_000_000,
            now_iso,
        ),
    )
    await db._conn.commit()

    ts = str(int(time.time()))
    query = f"ca={ca}&chain=ethereum"
    sig = _sign(secret, "GET", "/api/coin/lookup", ts, b"", query=query)
    resp = await c.get(
        f"/api/coin/lookup?{query}",
        headers={"X-Timestamp": ts, "X-Signature": sig},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["found"] is True
    assert data["reason"] == "found"
    assert data["symbol"] == "ASTORID"
    assert data["name"] == "AstoridFantasy"
    assert data["market_cap_usd"] == 50_000_000
    assert data["source"] == "candidates"


async def test_lookup_hmac_binds_query_string(client_with_secret):
    """V2-PR-review B-C1 fold: signature MUST bind query string.

    Capture a valid signature for ?ca=X&chain=solana; replay against
    ?ca=Y&chain=solana with the same (ts, sig) — must reject as
    signature-mismatch (NOT replay, since the canonical changes when
    query changes).
    """
    c, _, secret = client_with_secret
    ts = str(int(time.time()))
    legitimate_query = "ca=legitimateCAabcdef1234567890abcd&chain=solana"
    sig_legit = _sign(
        secret, "GET", "/api/coin/lookup", ts, b"", query=legitimate_query
    )
    # Replay sig_legit against a different CA — must 403 (sig mismatch).
    attacker_query = "ca=ATTACKERaaaaaaaaaaaaaaaaaaaaaaaaaa&chain=solana"
    resp = await c.get(
        f"/api/coin/lookup?{attacker_query}",
        headers={"X-Timestamp": ts, "X-Signature": sig_legit},
    )
    assert resp.status_code == 403, (
        f"Expected 403 (sig mismatch) when query rewritten; "
        f"got {resp.status_code}: {resp.text}. C1 not fixed."
    )


async def test_lookup_evm_case_normalized(client_with_secret):
    """V2-PR-review A-coverage-gap fold: SQLite WHERE is case-sensitive; EVM
    addresses are checksum-mixed-case. Lookup must succeed even if Hermes
    sent lowercase but candidates stored mixed-case (or vice versa)."""
    c, db, secret = client_with_secret
    stored_ca = "0xAaBbCcDdEeFf" + "1" * 28  # mixed case
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO candidates (
            contract_address, chain, token_name, ticker, first_seen_at
        ) VALUES (?, ?, ?, ?, ?)""",
        (stored_ca, "ethereum", "MixedCaseToken", "MCT", now_iso),
    )
    await db._conn.commit()
    # Query with lowercase
    query_ca = stored_ca.lower()
    ts = str(int(time.time()))
    query = f"ca={query_ca}&chain=ethereum"
    sig = _sign(secret, "GET", "/api/coin/lookup", ts, b"", query=query)
    resp = await c.get(
        f"/api/coin/lookup?{query}",
        headers={"X-Timestamp": ts, "X-Signature": sig},
    )
    assert resp.status_code == 200
    assert resp.json()["found"] is True
    assert resp.json()["symbol"] == "MCT"


# ---------------------------------------------------------------------------
# Migration / schema tests
# ---------------------------------------------------------------------------


async def test_migration_creates_narrative_alerts_inbound(db):
    """The new migration should create the narrative_alerts_inbound table."""
    _, db_path = db
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='narrative_alerts_inbound'"
        )
        row = await cur.fetchone()
        assert row is not None
        # Verify expected columns
        cur = await conn.execute("PRAGMA table_info(narrative_alerts_inbound)")
        cols = {c[1] for c in await cur.fetchall()}
        assert "event_id" in cols
        assert "tweet_text_hash" in cols
        assert "classifier_version" in cols
        assert "resolved_coin_id" in cols
        # And the UNIQUE index on event_id
        cur = await conn.execute("PRAGMA index_list(narrative_alerts_inbound)")
        index_list = await cur.fetchall()
        # SQLite creates an auto-index for UNIQUE; verify by attempting duplicate insert
        await conn.execute(
            """INSERT INTO narrative_alerts_inbound (
                event_id, tweet_id, tweet_author, tweet_ts, tweet_text,
                tweet_text_hash, classifier_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("dup_event_id", "1", "x", "2026-01-01", "t", "h", "v"),
        )
        await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                """INSERT INTO narrative_alerts_inbound (
                    event_id, tweet_id, tweet_author, tweet_ts, tweet_text,
                    tweet_text_hash, classifier_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("dup_event_id", "2", "x", "2026-01-01", "t", "h", "v"),
            )
            await conn.commit()


async def test_hmac_secret_validator_rejects_short_secret():
    """V2-PR-review B-S1 fold: Settings rejects HMAC secrets < 32 chars
    (empty is allowed as gated-off sentinel)."""
    # Empty is OK (gated-off)
    Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        NARRATIVE_SCANNER_HMAC_SECRET="",
    )
    # >=32 chars is OK
    Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        NARRATIVE_SCANNER_HMAC_SECRET="x" * 64,
    )
    # 1-31 chars must reject
    with pytest.raises(Exception):
        Settings(
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            NARRATIVE_SCANNER_HMAC_SECRET="too-short",
        )


async def test_post_rejects_oversize_body(client_with_secret):
    """V2-PR-review B-D5 fold: body-size cap rejects multi-MB payloads
    BEFORE HMAC compute."""
    c, _, secret = client_with_secret
    payload = _valid_alert_payload(event_id="b" * 64)
    # Inflate tweet_text to >16KB (cap default)
    payload["tweet_text"] = "x" * 20_000
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign(secret, "POST", "/api/narrative-alert", ts, body)
    resp = await c.post(
        "/api/narrative-alert",
        content=body,
        headers={
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
    )
    assert resp.status_code == 413
    assert "body exceeds" in resp.json()["detail"].lower()


async def test_post_rejects_chain_ca_shape_mismatch(client_with_secret):
    """V2-PR-review B-S3 fold: model_validator enforces chain×CA pairing.

    Solana CA with chain=ethereum must reject; EVM CA with chain=solana must reject.
    """
    c, _, secret = client_with_secret
    # Solana base58 CA with chain=ethereum
    payload = _valid_alert_payload(event_id="c" * 64)
    payload["extracted_ca"] = "FoMoLanaJzCFkUEcVTbgScfhUC6axpkvfFV3KGNVpump"
    payload["extracted_chain"] = "ethereum"
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign(secret, "POST", "/api/narrative-alert", ts, body)
    resp = await c.post(
        "/api/narrative-alert",
        content=body,
        headers={
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400
    assert (
        "extracted_ca for chain=ethereum" in resp.json()["detail"].lower()
        or "must match" in resp.json()["detail"].lower()
    )


async def test_post_accepts_cashtag_only_no_ca(client_with_secret):
    """V2-PR-review B-S3 fold edge case: cashtag-only with no CA + no chain
    is allowed (deferred-resolution case)."""
    c, _, secret = client_with_secret
    payload = _valid_alert_payload(event_id="d" * 64)
    payload["extracted_ca"] = None
    payload["extracted_chain"] = None
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign(secret, "POST", "/api/narrative-alert", ts, body)
    resp = await c.post(
        "/api/narrative-alert",
        content=body,
        headers={
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200


async def test_503_detail_does_not_leak_env_var_name(client_no_secret):
    """V2-PR-review B-D1 fold: 503 detail should NOT mention the env var name."""
    c, _ = client_no_secret
    resp = await c.get("/api/coin/lookup?ca=Foo&chain=solana")
    assert resp.status_code == 503
    detail = resp.json()["detail"].lower()
    assert "narrative_scanner_hmac_secret" not in detail


async def test_event_id_must_be_64_chars(client_with_secret):
    """V2-PR-review A-N4 fold: event_id pinned to 64-char sha256 hex."""
    c, _, secret = client_with_secret
    payload = _valid_alert_payload()
    payload["event_id"] = "a" * 32  # too short
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign(secret, "POST", "/api/narrative-alert", ts, body)
    resp = await c.post(
        "/api/narrative-alert",
        content=body,
        headers={
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


async def test_migration_is_idempotent(db):
    """Re-running initialize() should not error on existing table."""
    d, db_path = db
    # initialize was called in the fixture; call again
    await d.initialize()
    # If we got here without exception, idempotency holds.
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT cutover_ts FROM paper_migrations WHERE name='bl_narrative_scanner_v1'"
        )
        row = await cur.fetchone()
        assert row is not None
