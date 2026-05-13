"""Narrative scanner cross-VPS integration endpoints (BL-NEW-NARRATIVE-SCANNER V1).

Two HMAC-authed endpoints consumed by the Hermes-based scanner running on
main-vps under gecko-agent user:

    GET  /api/coin/lookup?ca={ca}&chain={chain}
        Resolves a contract address to canonical CoinGecko + DexScreener data
        via the existing scout.ingestion machinery. Read-only.

    POST /api/narrative-alert
        Persists a Hermes-emitted narrative event into narrative_alerts_inbound.
        Idempotent via UNIQUE(event_id).

Both endpoints respond 503 when ``Settings.NARRATIVE_SCANNER_HMAC_SECRET`` is
empty — feature is gated off by default. See
``tasks/design_crypto_narrative_scanner.md`` for the full design, including
the concrete HMAC scheme (§3) and idempotency semantics (§5).

HMAC scheme (per §3):

    canonical = f"{METHOD}\\n{PATH}\\n{X-Timestamp}\\n{BODY}"
    signature = HMAC-SHA256(secret, canonical)

Server enforces:
    1. ``|now() - X-Timestamp| <= NARRATIVE_SCANNER_REPLAY_WINDOW_SEC`` (default 300s)
    2. Constant-time signature compare
    3. Per-secret replay LRU (300s + buffer = 600s effective)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import OrderedDict
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from scout.config import Settings

_log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Replay-protection LRU. In-process; resets on uvicorn restart (acceptable —
# the 300s timestamp window self-clears within that horizon).
# ---------------------------------------------------------------------------

_REPLAY_LRU_MAX = 10_000
_replay_seen: OrderedDict[str, float] = OrderedDict()


def _replay_check(timestamp: str, signature: str, ttl_sec: int) -> bool:
    """Return True if this (timestamp, signature) was already seen within ttl_sec.

    Side effect: records (timestamp, signature) on first sight; prunes expired
    entries and trims LRU to ``_REPLAY_LRU_MAX``.
    """
    key = f"{timestamp}:{signature}"
    now = time.time()
    # Prune expired entries (lazy)
    expired = [k for k, ts in _replay_seen.items() if now - ts > ttl_sec]
    for k in expired:
        _replay_seen.pop(k, None)
    if key in _replay_seen:
        return True
    _replay_seen[key] = now
    while len(_replay_seen) > _REPLAY_LRU_MAX:
        _replay_seen.popitem(last=False)
    return False


# ---------------------------------------------------------------------------
# HMAC verification dependency
# ---------------------------------------------------------------------------


def _compute_signature(
    secret: str, method: str, path: str, timestamp: str, body: bytes
) -> str:
    canonical = f"{method}\n{path}\n{timestamp}\n".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


async def _verify_hmac(request: Request, settings: Settings) -> None:
    """Verify HMAC headers; raise 401/403 on any failure. Constant-time compare.

    Feature gate: empty ``NARRATIVE_SCANNER_HMAC_SECRET`` → 503. Endpoints
    intended for the Hermes side; with no secret, no caller can authenticate
    and the feature is off.
    """
    if not settings.NARRATIVE_SCANNER_HMAC_SECRET:
        raise HTTPException(
            status_code=503,
            detail="narrative_scanner: feature disabled (NARRATIVE_SCANNER_HMAC_SECRET empty)",
        )

    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-Signature", "")
    if not timestamp or not signature:
        raise HTTPException(
            status_code=401, detail="missing X-Timestamp or X-Signature"
        )

    try:
        ts_int = int(timestamp)
    except ValueError:
        raise HTTPException(
            status_code=401, detail="X-Timestamp not an integer (unix seconds)"
        )

    now = int(time.time())
    window = settings.NARRATIVE_SCANNER_REPLAY_WINDOW_SEC
    if abs(now - ts_int) > window:
        raise HTTPException(
            status_code=401,
            detail=f"X-Timestamp outside replay window ({window}s)",
        )

    body = await request.body()
    expected = _compute_signature(
        settings.NARRATIVE_SCANNER_HMAC_SECRET,
        request.method,
        request.url.path,
        timestamp,
        body,
    )
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="HMAC signature mismatch")

    # Replay window = 2× clock-skew window (per §3). Detects retransmits.
    if _replay_check(timestamp, signature, ttl_sec=window * 2):
        raise HTTPException(
            status_code=409, detail="duplicate request (replay-cache hit)"
        )


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class NarrativeAlertIn(BaseModel):
    """Inbound narrative event from Hermes side.

    Idempotency key is the Hermes-computed ``event_id`` (sha256 of tweet_id +
    tweet_text_hash + extracted_ca). Gecko-alpha side stores via
    UNIQUE(event_id) — duplicates rejected silently as 200-OK no-op.
    """

    event_id: str = Field(..., min_length=16, max_length=128)
    tweet_id: str = Field(..., min_length=1, max_length=64)
    tweet_author: str = Field(..., min_length=1, max_length=64)
    tweet_ts: str = Field(..., min_length=1, max_length=64)
    tweet_text: str = Field(..., min_length=1, max_length=4096)
    tweet_text_hash: str = Field(..., min_length=16, max_length=128)
    extracted_cashtag: str | None = Field(default=None, max_length=32)
    extracted_ca: str | None = Field(default=None, max_length=64)
    extracted_chain: str | None = Field(default=None, max_length=16)
    resolved_coin_id: str | None = Field(default=None, max_length=128)
    narrative_theme: str | None = Field(default=None, max_length=64)
    urgency_signal: str | None = Field(default=None, max_length=32)
    classifier_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    classifier_version: str = Field(..., min_length=1, max_length=64)


class CoinLookupOut(BaseModel):
    """Canonical coin data returned from /api/coin/lookup.

    Best-effort union of CoinGecko + DexScreener data. ``found=False`` when
    no resolver path returned anything; Hermes side proceeds with
    ``resolved_coin_id=NULL`` in that case (deferred-resolution).
    """

    found: bool
    ca: str
    chain: str
    coin_id: str | None = None
    symbol: str | None = None
    name: str | None = None
    market_cap_usd: float | None = None
    liquidity_usd: float | None = None
    price_usd: float | None = None
    source: str | None = None  # "coingecko" | "dexscreener" | "geckoterminal" | null


# ---------------------------------------------------------------------------
# Router factory — caller injects db_path + settings to keep this testable
# without globals (per CLAUDE.md "no global state").
# ---------------------------------------------------------------------------


def create_router(
    db_path: str,
    settings: Settings,
) -> APIRouter:
    """Build the narrative-scanner APIRouter.

    Mounted onto an existing FastAPI app via ``app.include_router(create_router(...))``.
    The two endpoints are scoped under ``/api/`` to match dashboard conventions
    but live in a separate router for clean separation (per §10.5 of the design).
    """
    router = APIRouter(prefix="/api", tags=["narrative-scanner"])

    @router.get("/coin/lookup", response_model=CoinLookupOut)
    async def coin_lookup(
        ca: str,
        chain: str,
        request: Request,
    ) -> CoinLookupOut:
        """Resolve a contract address to canonical coin data."""
        await _verify_hmac(request, settings)
        # Basic shape validation
        if chain not in ("solana", "ethereum", "base"):
            raise HTTPException(
                status_code=400,
                detail="chain must be one of: solana, ethereum, base",
            )
        if not ca or len(ca) < 16 or len(ca) > 64:
            raise HTTPException(status_code=400, detail="ca shape invalid")

        from scout.api.narrative_resolver import resolve_ca

        data = await resolve_ca(db_path, ca=ca, chain=chain)
        if data is None:
            return CoinLookupOut(found=False, ca=ca, chain=chain)
        return CoinLookupOut(found=True, ca=ca, chain=chain, **data)

    @router.post("/narrative-alert", status_code=200)
    async def narrative_alert(
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        """Persist a Hermes-emitted narrative event.

        Idempotent: duplicate event_id returns 200 with ``{"status": "duplicate"}``;
        new event returns 200 with ``{"status": "created", "id": <row_id>}``.
        """
        await _verify_hmac(request, settings)
        body = await request.body()
        try:
            payload = NarrativeAlertIn.model_validate_json(body)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid payload: {e}")

        from scout.api.narrative_resolver import insert_narrative_alert

        result = await insert_narrative_alert(db_path, payload)
        return result

    return router
