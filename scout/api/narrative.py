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

HMAC scheme (per §3, V2-PR-review C1 fold):

    canonical = f"{METHOD}\\n{PATH}\\n{QUERY}\\n{X-Timestamp}\\n{BODY}"
    signature = HMAC-SHA256(secret, canonical)

NOTE: query-string is included in the canonical (V2 fold for B-C1) — without
this, a captured GET signature would replay against different ``?ca=`` values.

Server enforces:
    1. Body size <= ``NARRATIVE_SCANNER_MAX_BODY_BYTES`` (cap BEFORE HMAC).
    2. ``|now() - X-Timestamp| <= NARRATIVE_SCANNER_REPLAY_WINDOW_SEC`` (default 300s).
    3. Constant-time signature compare.
    4. Per-secret replay LRU (300s + buffer = 600s effective).

Deployment note (Vector B S2): replay-LRU is in-process. Run uvicorn with
``--workers 1`` until Day 2 moves the LRU to SQLite-backed shared state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import OrderedDict
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator

from scout.api.narrative_resolver import insert_narrative_alert, resolve_ca
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
    secret: str,
    method: str,
    path: str,
    query: str,
    timestamp: str,
    body: bytes,
) -> str:
    """Compute HMAC-SHA256 over the canonical-string.

    V2-PR-review C1 fold: ``query`` (raw query-string, without leading '?') is
    included in the canonical so GET signatures bind their query params.
    Without this, a captured signature for ``/api/coin/lookup?ca=X&chain=solana``
    would replay against ``?ca=ATTACKER_CA&chain=solana``.
    """
    canonical = f"{method}\n{path}\n{query}\n{timestamp}\n".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def _reject(status_code: int, reason: str, detail: str) -> HTTPException:
    """Helper: emit a structured rejection log AND return the HTTPException.

    V2-PR-review C-OG4 fold: every rejection path logs a
    ``narrative_scanner_request_rejected`` event with the reason code so
    journalctl can distinguish "feature off" from "bad headers" from
    "replay-cache hit" without correlating with uvicorn access logs.
    """
    _log.info("narrative_scanner_request_rejected", reason=reason, status=status_code)
    return HTTPException(status_code=status_code, detail=detail)


async def _verify_hmac(request: Request, settings: Settings) -> bytes:
    """Verify HMAC headers; raise 401/403/409/413/503 on any failure.

    Returns the request body bytes on success (caller doesn't need to re-read).
    Feature gate: empty ``NARRATIVE_SCANNER_HMAC_SECRET`` → 503.
    """
    if not settings.NARRATIVE_SCANNER_HMAC_SECRET:
        # V2-PR-review B-D1 fold: drop env-var name from detail (info-leak hardening).
        raise _reject(503, "disabled", "narrative_scanner: feature disabled")

    # Body-size cap BEFORE reading body or computing HMAC.
    # V2-PR-review B-D5 fold: prevents body-flood DoS by unauthenticated clients.
    content_length = request.headers.get("content-length", "0")
    try:
        cl_int = int(content_length)
    except ValueError:
        cl_int = 0
    max_body = settings.NARRATIVE_SCANNER_MAX_BODY_BYTES
    if cl_int > max_body:
        raise _reject(
            413,
            "body_too_large",
            f"body exceeds NARRATIVE_SCANNER_MAX_BODY_BYTES ({max_body})",
        )

    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-Signature", "")
    if not timestamp or not signature:
        raise _reject(401, "missing_headers", "missing X-Timestamp or X-Signature")

    try:
        ts_int = int(timestamp)
    except ValueError:
        raise _reject(401, "bad_timestamp", "X-Timestamp not an integer (unix seconds)")

    now = int(time.time())
    window = settings.NARRATIVE_SCANNER_REPLAY_WINDOW_SEC
    delta = abs(now - ts_int)
    if delta > window:
        # V2-PR-review C-SFC3 fold: log delta_sec so cross-VPS clock-skew
        # is diagnosable via journalctl grep.
        _log.warning(
            "narrative_scanner_timestamp_window_violation",
            delta_sec=delta,
            window_sec=window,
        )
        raise _reject(
            401,
            "out_of_window",
            f"X-Timestamp outside replay window ({window}s)",
        )

    body = await request.body()
    # Recheck size after read (Content-Length can lie / streaming uploads).
    if len(body) > max_body:
        raise _reject(
            413,
            "body_too_large",
            f"body exceeds NARRATIVE_SCANNER_MAX_BODY_BYTES ({max_body})",
        )

    expected = _compute_signature(
        settings.NARRATIVE_SCANNER_HMAC_SECRET,
        request.method,
        request.url.path,
        request.url.query,  # V2-PR-review B-C1 fold: bind query string
        timestamp,
        body,
    )
    if not hmac.compare_digest(expected, signature):
        raise _reject(403, "sig_mismatch", "HMAC signature mismatch")

    # Replay window = 2× clock-skew window (per §3). Detects retransmits.
    if _replay_check(timestamp, signature, ttl_sec=window * 2):
        raise _reject(409, "replay", "duplicate request (replay-cache hit)")

    return body


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


_ALLOWED_CHAINS = ("solana", "ethereum", "base")


class NarrativeAlertIn(BaseModel):
    """Inbound narrative event from Hermes side.

    Idempotency key is the Hermes-computed ``event_id`` (sha256 of tweet_id +
    tweet_text_hash + extracted_ca). Gecko-alpha side stores via
    UNIQUE(event_id) — duplicates rejected silently as 200-OK no-op.

    V2-PR-review A-N4 fold: ``event_id`` pinned to 64-char sha256 hex
    (Hermes-side spec).

    V2-PR-review B-S3 fold: ``(extracted_chain, extracted_ca)`` pair is
    validated for shape consistency via the model_validator below — Solana
    must be base58 32-44 chars; ETH/BASE must be ``0x`` + 40 hex.
    """

    event_id: str = Field(..., min_length=64, max_length=64)
    tweet_id: str = Field(..., min_length=1, max_length=64)
    tweet_author: str = Field(..., min_length=1, max_length=64)
    tweet_ts: str = Field(..., min_length=1, max_length=64)
    tweet_text: str = Field(..., min_length=1, max_length=4096)
    tweet_text_hash: str = Field(..., min_length=16, max_length=128)
    extracted_cashtag: str | None = Field(default=None, max_length=32)
    extracted_ca: str | None = Field(default=None, max_length=64)
    extracted_chain: Literal["solana", "ethereum", "base"] | None = None
    resolved_coin_id: str | None = Field(default=None, max_length=128)
    narrative_theme: str | None = Field(default=None, max_length=64)
    urgency_signal: str | None = Field(default=None, max_length=32)
    classifier_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    classifier_version: str = Field(..., min_length=1, max_length=64)

    @model_validator(mode="after")
    def _validate_chain_ca_consistency(self) -> "NarrativeAlertIn":
        """V2-PR-review B-S3: enforce chain×CA shape pairing.

        - solana → base58, 32-44 chars, no '0x' prefix
        - ethereum, base → ^0x[a-fA-F0-9]{40}$
        - missing chain → CA must also be None (cashtag-only is allowed)
        """
        ca = self.extracted_ca
        chain = self.extracted_chain
        if ca is None and chain is None:
            return self
        if ca is None:
            # Chain set but no CA — Hermes side may surface a cashtag-only event.
            return self
        if chain is None:
            raise ValueError("extracted_ca provided but extracted_chain missing")
        if chain == "solana":
            if ca.startswith("0x") or not (32 <= len(ca) <= 44):
                raise ValueError(
                    "extracted_ca for chain=solana must be base58, 32-44 chars, no 0x prefix"
                )
        else:  # ethereum, base
            import re as _re

            if not _re.fullmatch(r"0x[a-fA-F0-9]{40}", ca):
                raise ValueError(
                    f"extracted_ca for chain={chain} must match ^0x[a-fA-F0-9]{{40}}$"
                )
        return self


class CoinLookupOut(BaseModel):
    """Canonical coin data returned from /api/coin/lookup.

    Best-effort union of CoinGecko + DexScreener data. ``found=False`` when
    no resolver path returned anything; Hermes side proceeds with
    ``resolved_coin_id=NULL`` in that case (deferred-resolution).

    V2-PR-review C-SFC2 fold: ``reason`` differentiates "CA genuinely unknown"
    from "resolver-side error" so Hermes can branch.
    """

    found: bool
    ca: str
    chain: str
    reason: Literal["found", "not_found", "resolver_error"] = "found"
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
        # Basic shape validation (chain whitelist + length)
        if chain not in _ALLOWED_CHAINS:
            raise _reject(
                400,
                "bad_chain",
                f"chain must be one of: {', '.join(_ALLOWED_CHAINS)}",
            )
        if not ca or len(ca) < 16 or len(ca) > 64:
            raise _reject(400, "bad_ca_shape", "ca shape invalid")

        result = await resolve_ca(db_path, ca=ca, chain=chain)
        if result is None:
            return CoinLookupOut(found=False, ca=ca, chain=chain, reason="not_found")
        if result.get("_resolver_error"):
            # V2-PR-review C-SFC2 fold: distinguish unknown-CA from resolver-broken.
            return CoinLookupOut(
                found=False, ca=ca, chain=chain, reason="resolver_error"
            )
        return CoinLookupOut(found=True, ca=ca, chain=chain, reason="found", **result)

    @router.post("/narrative-alert", status_code=200)
    async def narrative_alert(
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        """Persist a Hermes-emitted narrative event.

        Idempotent: duplicate event_id returns 200 with ``{"status": "duplicate"}``;
        new event returns 200 with ``{"status": "created", "id": <row_id>}``.
        """
        body = await _verify_hmac(request, settings)
        try:
            payload = NarrativeAlertIn.model_validate_json(body)
        except Exception as e:
            raise _reject(400, "invalid_payload", f"invalid payload: {e}")

        try:
            result = await insert_narrative_alert(db_path, payload)
            return result
        except Exception as exc:
            # V2-PR-review C-OG3 fold: structured failure log tying the 500
            # back to event_id + tweet_id so journalctl is searchable.
            _log.error(
                "narrative_alert_insert_failed",
                event_id=payload.event_id,
                tweet_id=payload.tweet_id,
                err=str(exc),
                err_type=type(exc).__name__,
            )
            raise

    return router


def create_stub_router() -> APIRouter:
    """Stub router used when Settings load failed at module import.

    V2-PR-review C-SFC1 fold: returns 503 on both endpoints with
    ``detail="dashboard_settings_init_failed"`` so the Hermes side gets a
    503 (same disabled-feature contract) rather than a 404 (which would be
    indistinguishable from "endpoint doesn't exist"). Each request emits a
    distinct rejection log for operator forensics.
    """
    stub = APIRouter(prefix="/api", tags=["narrative-scanner-stub"])

    async def _stub_503() -> None:
        raise _reject(
            503, "settings_init_failed", "narrative_scanner: settings init failed"
        )

    @stub.get("/coin/lookup")
    async def stub_lookup(ca: str = "", chain: str = "") -> None:
        await _stub_503()

    @stub.post("/narrative-alert")
    async def stub_post() -> None:
        await _stub_503()

    return stub
