"""BL-NEW-LIVE-HYBRID M1 v2.1: Telegram approval gateway.

Operator commands (24h-ephemeral overrides written to
`live_operator_overrides` table):
  /allow-stack <token>        → ignore aggregator-guard for one trade on
                                token (24h)
  /auto-approve venue=<name>  → bypass approval gates 1-3 for this venue
  /approval-required venue=<name> → force approval gate (overrides
                                    new-venue + trade-size + health)
  /venue-revive name=<name>   → mark venue auth_ok=1 + clear dormancy

`request_operator_approval()` is the engine-side entrypoint:
  - publishes a Telegram alert ("paper trade #X needs approval; reply
    /yes <id> or /no <id> within 5 min")
  - polls live_operator_overrides for a matching response row
  - returns True/False/timeout

M1 ships the table-write commands + the request entrypoint as a SCAFFOLD
that integrates with the existing alerter module. The full bot-side
command parsing wires up to the existing telethon listener (BL-064)
in M1.5 — for M1 the operator can write rows directly via SQL or via
the dashboard, and the engine consults them.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from scout.db import Database

log = structlog.get_logger(__name__)

OVERRIDE_TTL_HOURS = 24
APPROVAL_TIMEOUT_DEFAULT_SEC = 300  # 5 min


async def set_operator_override(
    db: Database,
    *,
    override_type: str,
    venue: str | None = None,
    canonical: str | None = None,
    set_by: str | None = None,
) -> int:
    """Write a 24h-ephemeral override row. Returns inserted id.

    `override_type` must be one of: allow_stack, auto_approve,
    approval_required, venue_revive (matches CHECK constraint on
    live_operator_overrides table).
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=OVERRIDE_TTL_HOURS)
    cur = await db._conn.execute(
        """INSERT INTO live_operator_overrides
           (override_type, venue, canonical, set_at, expires_at, set_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            override_type,
            venue,
            canonical,
            now.isoformat(),
            expires.isoformat(),
            set_by,
        ),
    )
    await db._conn.commit()
    log.info(
        "operator_override_set",
        override_type=override_type,
        venue=venue,
        canonical=canonical,
        set_by=set_by,
        expires_at=expires.isoformat(),
    )
    # Best-effort fetch of the inserted rowid (lastrowid on aiosqlite cursor)
    return cur.lastrowid or 0


async def has_active_override(
    db: Database, *, override_type: str, venue: str | None = None
) -> bool:
    """Returns True if an unexpired override of `override_type` exists
    for `venue` (NULL `venue` matches any-venue overrides too).
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    now_iso = datetime.now(timezone.utc).isoformat()
    if venue is None:
        cur = await db._conn.execute(
            """SELECT 1 FROM live_operator_overrides
               WHERE override_type = ? AND expires_at > ? LIMIT 1""",
            (override_type, now_iso),
        )
    else:
        cur = await db._conn.execute(
            """SELECT 1 FROM live_operator_overrides
               WHERE override_type = ?
                 AND (venue = ? OR venue IS NULL)
                 AND expires_at > ?
               LIMIT 1""",
            (override_type, venue, now_iso),
        )
    return (await cur.fetchone()) is not None


async def _send_approval_alert(
    *,
    session: Any,
    settings: Any,
    paper_trade: Any,
    venue: str | None,
    gate: str | None,
    timeout_sec: float,
) -> None:
    """Best-effort Telegram notification that a live trade needs approval.

    LIVE-07 (S2-1 fix): before this, ``request_operator_approval`` only
    logged + polled, so every early live trade blocked ``timeout_sec`` then
    rejected with the operator never told there was anything to approve.

    §12b: emits ``live_approval_alert_dispatched`` / ``_delivered`` structured
    logs around the send so the fire is traceable regardless of delivery
    outcome (the alerter logs only on failure). ``parse_mode=None`` because
    the body carries underscore-bearing signal/gate names (``gainers_early``,
    ``new_venue_gate``) that MarkdownV1 would mangle without erroring.

    The send is best-effort: a delivery failure is logged but does NOT block
    the override poll (the trade fails closed on timeout, never on the alert).
    Skips silently (with a log) when no aiohttp session / settings are
    injected — the call sites that cannot supply them keep the prior
    log-and-poll behaviour rather than crashing.
    """
    pt_id = getattr(paper_trade, "id", None)
    if session is None or settings is None:
        log.info(
            "live_approval_alert_skipped",
            paper_trade_id=pt_id,
            venue=venue,
            gate=gate,
            reason="no_session_or_settings",
        )
        return
    from scout import alerter  # local import (Windows OpenSSL)

    minutes = max(int(timeout_sec // 60), 1)
    body = (
        f"Live trade #{pt_id} needs approval "
        f"(venue={venue}, gate={gate}). "
        f"Set /auto-approve venue={venue} within {minutes} min or it "
        f"will be rejected."
    )
    try:
        log.info(
            "live_approval_alert_dispatched",
            paper_trade_id=pt_id,
            venue=venue,
            gate=gate,
        )
        await alerter.send_telegram_message(
            body,
            session,
            settings,
            parse_mode=None,
            source="live_approval",
        )
        log.info(
            "live_approval_alert_delivered",
            paper_trade_id=pt_id,
            venue=venue,
            gate=gate,
        )
    except Exception as exc:
        log.exception(
            "live_approval_alert_failed",
            paper_trade_id=pt_id,
            venue=venue,
            gate=gate,
            err=str(exc),
            err_type=type(exc).__name__,
        )


async def request_operator_approval(
    db: Database,
    *,
    paper_trade: Any,
    candidate: Any,
    gate: str | None,
    timeout_sec: float = APPROVAL_TIMEOUT_DEFAULT_SEC,
    session: Any = None,
    settings: Any = None,
) -> bool:
    """Engine-side approval entrypoint. Posts a Telegram alert and polls
    `live_operator_overrides` for an `auto_approve` row matching the
    candidate's venue.

    Returns:
      True  — operator approved (auto_approve override set within window)
      False — denied or timeout

    `session` (aiohttp.ClientSession) + `settings` are used to deliver the
    operator-facing Telegram alert; when either is omitted the function
    degrades to the prior log-and-poll behaviour (see
    :func:`_send_approval_alert`). The bot-side command parsing that turns an
    operator reply into an override row wires up to the telethon listener
    (BL-064) in M1.5; for M1 the operator writes rows via SQL / the
    dashboard, and the engine consults them here.
    """
    log.info(
        "operator_approval_requested",
        paper_trade_id=getattr(paper_trade, "id", None),
        venue=getattr(candidate, "venue", None),
        gate=gate,
        timeout_sec=timeout_sec,
    )
    venue = getattr(candidate, "venue", None)
    # LIVE-07: actually notify the operator that a trade is waiting.
    await _send_approval_alert(
        session=session,
        settings=settings,
        paper_trade=paper_trade,
        venue=venue,
        gate=gate,
        timeout_sec=timeout_sec,
    )
    poll_interval = 2.0
    deadline = asyncio.get_event_loop().time() + timeout_sec
    while asyncio.get_event_loop().time() < deadline:
        if await has_active_override(db, override_type="auto_approve", venue=venue):
            log.info(
                "operator_approval_granted",
                paper_trade_id=getattr(paper_trade, "id", None),
                venue=venue,
            )
            return True
        await asyncio.sleep(poll_interval)
    log.info(
        "operator_approval_timeout",
        paper_trade_id=getattr(paper_trade, "id", None),
        venue=venue,
        timeout_sec=timeout_sec,
    )
    return False
