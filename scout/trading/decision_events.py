"""Fail-soft trade dispatch decision event persistence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from scout.db import Database

log = structlog.get_logger(__name__)


async def emit_trade_decision(
    db: Database,
    *,
    token_id: str,
    signal_type: str,
    decision: str,
    reason: str,
    source_module: str,
    signal_combo: str | None = None,
    paper_trade_id: int | None = None,
    event_data: dict[str, Any] | None = None,
) -> int | None:
    """Append one trade admission/skip decision.

    This is observability, not control flow. Failures are logged and swallowed
    so the trading path keeps the exact pre-existing behavior.
    """
    conn = db._conn
    if conn is None:
        log.warning(
            "trade_decision_event_skipped_db_closed",
            token_id=token_id,
            signal_type=signal_type,
            decision=decision,
            reason=reason,
        )
        return None

    payload = event_data or {}
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        cursor = await conn.execute(
            """INSERT INTO trade_decision_events
               (token_id, signal_type, decision, reason, source_module,
                signal_combo, paper_trade_id, event_data, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                token_id,
                signal_type,
                decision,
                reason,
                source_module,
                signal_combo,
                paper_trade_id,
                json.dumps(payload, sort_keys=True, default=str),
                created_at,
            ),
        )
        await conn.commit()
        event_id = int(cursor.lastrowid)
        log.debug(
            "trade_decision_event_emitted",
            event_id=event_id,
            token_id=token_id,
            signal_type=signal_type,
            decision=decision,
            reason=reason,
        )
        return event_id
    except Exception as exc:
        log.warning(
            "trade_decision_event_emit_failed",
            token_id=token_id,
            signal_type=signal_type,
            decision=decision,
            reason=reason,
            error=str(exc),
        )
        return None
