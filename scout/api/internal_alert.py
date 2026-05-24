"""Internal HMAC-authed operator-alert endpoint (BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE).

Provides a single endpoint that the Hermes-side narrative-scanner dispatcher
(running on srilu under gecko-agent) — and any future internal caller — can
POST to in order to surface an operator-visible Telegram message.

    POST /api/internal/operator-alert
        Persists an operator alert by dispatching it through the existing
        scout.alerter.send_telegram_message infrastructure. HMAC-authed via
        the shared ``_verify_hmac`` helper from ``scout/api/narrative.py``
        BUT against an independent secret ``OPERATOR_ALERT_HMAC_SECRET`` —
        Reviewer 1 P1 fold: using the same secret as the narrative endpoint
        would mean the dispatcher could not raise a Telegram alert in the
        exact failure mode this endpoint exists to surface (missing/broken
        ``NARRATIVE_SCANNER_HMAC_SECRET``). Independent secrets break that
        circular dependency.

Evidence trigger (per backlog): activated 2026-05-18 after
narrative_alerts_inbound reached 204 rows (gate was >=10). The Hermes
dispatcher's prior Path B (log-only `narrative_dispatcher_misconfig`) is
replaced by a structured `*_dispatched` -> `*_delivered` -> `*_failed`
triplet around the Telegram call, per CLAUDE.md §12b.

Drift verdict: NET-NEW endpoint, but reuses the existing HMAC verification
primitive from `scout.api.narrative._verify_hmac` (which already folds in
V2-PR-review hardening: query-string binding, body-size cap, timestamp
window, replay LRU, structured rejection logging).

Hermes-first verdict (re-checked 2026-05-18): no installed/external skill
covers outbound Telegram operator-alert delivery that can be called as a
library from another Python service. Wiring into scout.alerter remains the
cheapest correct path.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

# Reuse the hardened HMAC verifier from the narrative router. These are
# package-private but already act as the canonical HMAC primitive for
# scout/api/* endpoints. If a third endpoint shows up, refactor to
# scout/api/_hmac.py.
from scout.alerter import send_telegram_message
from scout.api.narrative import _reject, _verify_hmac
from scout.config import Settings

_log = structlog.get_logger()


class OperatorAlertIn(BaseModel):
    """Inbound operator-alert payload.

    The body is intentionally minimal — Hermes-side composes the message
    text and tags the source (e.g., "narrative_dispatcher_misconfig").
    Receiving side simply delivers to Telegram with plain-text parse mode.
    """

    message: str = Field(..., min_length=1, max_length=4096)
    source: str = Field(..., min_length=1, max_length=64)


def create_router(settings: Settings) -> APIRouter:
    """Build the internal-alert APIRouter.

    Mounted onto the dashboard FastAPI app via
    ``app.include_router(create_router(settings))``. Feature-gated identically
    to the narrative router: empty ``NARRATIVE_SCANNER_HMAC_SECRET`` -> 503
    via ``_verify_hmac``.
    """
    router = APIRouter(prefix="/api/internal", tags=["internal-alert"])

    @router.post("/operator-alert", status_code=200)
    async def operator_alert(request: Request) -> dict[str, Any]:
        """HMAC-authed dispatcher for operator-visible Telegram alerts.

        Flow per CLAUDE.md §12b:
            1. HMAC verify against OPERATOR_ALERT_HMAC_SECRET (raises
               401/403/409/413/503 on failure). Independent of
               NARRATIVE_SCANNER_HMAC_SECRET so this endpoint can still
               authenticate even when the narrative endpoint is gated off.
            2. Parse payload (raises 400 on shape failure)
            3. Emit ``operator_alert_dispatched`` (BEFORE delivery)
            4. Call ``send_telegram_message`` with ``parse_mode=None``
               (§2.9 hygiene — caller is responsible for content)
            5. Emit ``operator_alert_delivered`` (on success) -> 200
               OR ``operator_alert_failed`` (on exception) -> 502

        Returns:
            {"status": "delivered", "source": <source>}
        Raises:
            HTTPException 502 on Telegram delivery failure (Hermes side
            sees a non-2xx so the alert isn't silently swallowed).
        """
        body = await _verify_hmac(
            request,
            settings,
            secret_field="OPERATOR_ALERT_HMAC_SECRET",
            feature_label="internal_alert",
        )
        try:
            payload = OperatorAlertIn.model_validate_json(body)
        except Exception as exc:  # noqa: BLE001 — surface to operator as 400
            raise _reject(400, "invalid_payload", f"invalid payload: {exc}")

        # §12b dispatched log: emitted BEFORE delivery so journalctl shows
        # the attempt even if the TG call hangs. Message length only
        # (no body content) keeps PII / operator-content out of structured
        # logs that may ship offsite.
        _log.info(
            "operator_alert_dispatched",
            source=payload.source,
            message_len=len(payload.message),
        )

        # Per-request session is fine here: alert rate is low (the gate
        # required >=10 narrative_alerts_inbound before this path activates;
        # operator-alert traffic is expected to be a small fraction of that
        # — single-digit alerts/day in steady state).
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            try:
                await send_telegram_message(
                    payload.message,
                    session=session,
                    settings=settings,
                    parse_mode=None,  # §2.9: caller-supplied text, no Markdown
                    raise_on_failure=True,  # so we can emit the *_failed log
                    source=f"internal_alert:{payload.source}",
                )
            except Exception as exc:  # noqa: BLE001 — alerter wraps everything
                _log.error(
                    "operator_alert_failed",
                    source=payload.source,
                    err=str(exc),
                    err_type=type(exc).__name__,
                )
                raise _reject(502, "delivery_failed", "telegram delivery failed")

        _log.info("operator_alert_delivered", source=payload.source)
        return {"status": "delivered", "source": payload.source}

    return router


def create_stub_router() -> APIRouter:
    """Stub router used when Settings load failed at module import.

    Mirrors the narrative stub: returns 503 with a distinct rejection log
    so the Hermes side sees a 503 (same disabled-feature contract) rather
    than a 404.
    """
    stub = APIRouter(prefix="/api/internal", tags=["internal-alert-stub"])

    @stub.post("/operator-alert")
    async def _stub_503() -> None:
        raise _reject(
            503, "settings_init_failed", "internal_alert: settings init failed"
        )

    return stub
