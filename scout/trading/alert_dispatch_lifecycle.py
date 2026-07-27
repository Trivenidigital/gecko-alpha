"""Shared Telegram-alert dispatch lifecycle (P0-1).

ONE write-ahead-intent state model for EVERY ``tg_alert_log``-backed Telegram
lane (trade-surface alerts + paper-trade alerts). No dispatch is ever recorded
as delivered (``sent``) until the provider actually accepts it, and a crash is
always distinguishable from a delivery.

States (persisted in ``tg_alert_log.outcome``; the CHECK is widened by
``db._migrate_tg_alert_log_dispatch_pending_outcome``):

- ``dispatch_pending``   — slot reserved; provider send NOT started
  (preprocessing: Minara lookup / formatting). A crash here stays *pending*.
- ``dispatch_attempted`` — committed IMMEDIATELY before the provider call. A
  crash here is reconciled to ``delivery_unknown_after_send`` (the provider may
  or may not have accepted it).
- ``sent``               — provider returned success. ONLY this counts as
  delivered.
- ``dispatch_failed``    — confirmed preprocessing/provider failure. Slot freed.
- ``delivery_unknown_after_send`` — a stale ``dispatch_attempted`` whose provider
  result is unprovable (the process died during/after the send).

Reservation semantics:
- CAP reserves ``sent`` + ``dispatch_pending`` + ``dispatch_attempted`` +
  ``delivery_unknown_after_send`` (a confirmed ``dispatch_failed`` frees the
  slot).
- DEDUP reserves ``sent`` + ``dispatch_pending`` + ``dispatch_attempted`` (an
  in-flight or delivered alert blocks a duplicate token claim).
- DELIVERED reporting keys strictly on ``sent`` — a pending/attempted/unknown
  row is NEVER counted as delivered.

The transition helpers are guarded on the expected prior state so promotion is
idempotent and a demote can never clobber a concurrently-promoted ``sent`` row.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

# --- outcome values ---------------------------------------------------------
DISPATCH_PENDING = "dispatch_pending"
DISPATCH_ATTEMPTED = "dispatch_attempted"
SENT = "sent"
DISPATCH_FAILED = "dispatch_failed"
DELIVERY_UNKNOWN = "delivery_unknown_after_send"

# --- reservation sets -------------------------------------------------------
CAP_RESERVED_STATES = (SENT, DISPATCH_PENDING, DISPATCH_ATTEMPTED, DELIVERY_UNKNOWN)
DEDUP_RESERVED_STATES = (SENT, DISPATCH_PENDING, DISPATCH_ATTEMPTED)

# Frozen (code-level) reconciliation age. A dispatch that has not reached a
# terminal state (sent / dispatch_failed / delivery_unknown) within this many
# seconds is treated as crashed. A send + promote/demote completes in well under
# a second, so 300s is a deterministic, conservative floor. FROZEN here per the
# reviewer ruling — it is NOT chosen ad hoc at runtime and NOT operator-tunable.
STALE_RECONCILE_SECONDS = 300


def _sql_in(states: tuple[str, ...]) -> str:
    """Render a tuple of states as a SQL ``IN (...)`` literal list (values are
    module constants, never user input — safe to inline)."""
    return "(" + ", ".join(f"'{s}'" for s in states) + ")"


def cap_reserved_in_clause() -> str:
    return _sql_in(CAP_RESERVED_STATES)


def dedup_reserved_in_clause() -> str:
    return _sql_in(DEDUP_RESERVED_STATES)


async def mark_attempted(db, row_id: int | None) -> None:
    """``dispatch_pending`` -> ``dispatch_attempted`` (committed IMMEDIATELY
    before the provider call). Guarded so it only advances a pending row."""
    if row_id is None:
        return
    await db.execute_write(
        f"UPDATE tg_alert_log SET outcome='{DISPATCH_ATTEMPTED}' "
        f"WHERE id=? AND outcome='{DISPATCH_PENDING}'",
        (row_id,),
    )


async def promote_sent(db, row_id: int | None) -> None:
    """``dispatch_attempted`` -> ``sent`` (ONLY after provider success).
    Idempotent: guarded on ``dispatch_attempted`` so a second call is a no-op."""
    if row_id is None:
        return
    await db.execute_write(
        f"UPDATE tg_alert_log SET outcome='{SENT}' "
        f"WHERE id=? AND outcome='{DISPATCH_ATTEMPTED}'",
        (row_id,),
    )


async def demote_failed(db, row_id: int | None, *, detail: str) -> None:
    """``dispatch_pending`` | ``dispatch_attempted`` -> ``dispatch_failed`` on a
    CONFIRMED non-delivery (preprocessing error, cancel before send, or a
    provider error return)."""
    if row_id is None:
        return
    await db.execute_write(
        f"UPDATE tg_alert_log SET outcome='{DISPATCH_FAILED}', detail=? "
        f"WHERE id=? AND outcome IN ('{DISPATCH_PENDING}', '{DISPATCH_ATTEMPTED}')",
        (detail, row_id),
    )


async def mark_delivery_unknown(db, row_id: int | None, *, detail: str) -> None:
    """``dispatch_attempted`` -> ``delivery_unknown_after_send`` when the send was
    interrupted (e.g. cancelled) after it began and its result is unprovable."""
    if row_id is None:
        return
    await db.execute_write(
        f"UPDATE tg_alert_log SET outcome='{DELIVERY_UNKNOWN}', detail=? "
        f"WHERE id=? AND outcome='{DISPATCH_ATTEMPTED}'",
        (detail, row_id),
    )


def merge_error_detail(detail: dict | str | None, error: str) -> str:
    """Build a detail JSON string preserving provenance + the failure reason."""
    if isinstance(detail, dict):
        return json.dumps({**detail, "error": error}, sort_keys=True)
    if detail:
        return json.dumps({"detail": str(detail), "error": error}, sort_keys=True)
    return json.dumps({"error": error}, sort_keys=True)


async def reconcile_stale(db, signal_type: str, *, now: datetime | None = None) -> dict:
    """Deterministic stale-intent sweep for one ``signal_type`` (P0-1).

    Owner: each dispatch lane calls this at dispatch entry (claim time), so the
    sweep is deterministic and self-owned — no separate daemon required. Age
    threshold is the FROZEN ``STALE_RECONCILE_SECONDS``.

      stale ``dispatch_pending``   -> ``dispatch_failed`` (never reached the
                                      provider — a preprocessing crash),
      stale ``dispatch_attempted`` -> ``delivery_unknown_after_send`` (send began
                                      but its result is unprovable).

    Returns ``{"pending_failed": n, "attempted_unknown": m}``.
    """
    if getattr(db, "_conn", None) is None:
        return {"pending_failed": 0, "attempted_unknown": 0}
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=STALE_RECONCILE_SECONDS)).isoformat()
    pending_failed = await db.execute_write(
        f"UPDATE tg_alert_log SET outcome='{DISPATCH_FAILED}', "
        f"detail=COALESCE(detail, '') || ' [reconciled: stale pending -> failed]' "
        f"WHERE signal_type=? AND outcome='{DISPATCH_PENDING}' AND alerted_at < ?",
        (signal_type, cutoff),
    )
    attempted_unknown = await db.execute_write(
        f"UPDATE tg_alert_log SET outcome='{DELIVERY_UNKNOWN}', "
        f"detail=COALESCE(detail, '') || ' [reconciled: stale attempted -> unknown]' "
        f"WHERE signal_type=? AND outcome='{DISPATCH_ATTEMPTED}' AND alerted_at < ?",
        (signal_type, cutoff),
    )
    return {"pending_failed": pending_failed, "attempted_unknown": attempted_unknown}
