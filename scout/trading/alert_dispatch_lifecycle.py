"""Shared Telegram-alert dispatch lifecycle (P0-1 + P0-2).

ONE write-ahead-intent state model, with an ownership LEASE, for EVERY
``tg_alert_log``-backed Telegram lane (trade-surface alerts + paper-trade
alerts). No dispatch is ever recorded as delivered (``sent``) until the provider
actually accepts it, a crash is always distinguishable from a delivery, and a
stale-intent reconciliation can NEVER demote a dispatch that its owner is still
actively sending.

States (persisted in ``tg_alert_log.outcome``; the CHECK is widened by
``db._migrate_tg_alert_log_dispatch_pending_outcome``):

- ``dispatch_pending``   — slot reserved; provider send NOT started
  (preprocessing: Minara lookup / formatting). A crash here stays *pending* and
  is reconciled to ``dispatch_failed`` (the provider was never reached).
- ``dispatch_attempted`` — committed IMMEDIATELY before the provider call. A
  crash here is reconciled to ``delivery_unknown_after_send`` (the provider may
  or may not have accepted it).
- ``sent``               — provider returned success. ONLY this counts as
  delivered.
- ``dispatch_failed``    — confirmed preprocessing/provider failure, OR a stale
  ``dispatch_pending`` (never reached the provider). Slot freed.
- ``delivery_unknown_after_send`` — a ``dispatch_attempted`` whose provider
  result is unprovable (the process died / was cancelled during-or-after the
  send). It is treated as *possibly delivered*: it reserves BOTH cap AND dedup
  and is NEVER automatically retried (P0-1 ruling).
- ``unknown_resolved_retryable`` — an operator-only terminal state. NOTHING in
  this module (and nothing automatic anywhere) ever writes it: no transition
  helper and no reconciliation path produces it. It exists ONLY so a human
  operator, having independently confirmed a ``delivery_unknown_after_send`` did
  NOT deliver, can explicitly move it into a state the dedup/cap arithmetic
  treats as free — turning an unknown into retry permission requires an explicit
  operator action, never a silent automatic conversion. (This module ships the
  state name + the invariant; the operator tooling itself is out of scope.)

Reservation semantics:
- CAP reserves ``sent`` + ``dispatch_pending`` + ``dispatch_attempted`` +
  ``delivery_unknown_after_send`` (a confirmed ``dispatch_failed`` or an
  operator ``unknown_resolved_retryable`` frees the slot).
- DEDUP reserves ``sent`` + ``dispatch_pending`` + ``dispatch_attempted`` +
  ``delivery_unknown_after_send`` (an in-flight, delivered, OR unprovable alert
  all block a duplicate token claim — an unknown is NOT auto-retryable).
- DELIVERED reporting keys strictly on ``sent`` — a pending/attempted/unknown/
  retryable row is NEVER counted as delivered.

Ownership lease (P0-2):
- Each dispatch claims its ``dispatch_pending`` row with a unique
  ``dispatch_lease_token`` (see :func:`new_lease`) and stamps
  ``dispatch_state_updated_at``.
- EVERY owner transition (attempted / sent / failed / unknown) is guarded on
  that token AND refreshes ``dispatch_state_updated_at``, and RETURNS whether
  the compare-and-set actually applied. The caller MUST check the return: a
  ``False`` from ``mark_attempted`` means the lease was reclaimed by a stale
  sweep and the provider call MUST be aborted.
- :func:`reconcile_stale` reclaims ONLY *expired* leases — age measured from
  ``dispatch_state_updated_at`` (the last owner heartbeat), never from the
  original ``alerted_at``. A dispatch that is actively preprocessing or sending
  keeps its lease fresh (each transition refreshes it), so a concurrent sweep
  can never demote it.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import structlog

log = structlog.get_logger(__name__)

# --- outcome values ---------------------------------------------------------
DISPATCH_PENDING = "dispatch_pending"
DISPATCH_ATTEMPTED = "dispatch_attempted"
SENT = "sent"
DISPATCH_FAILED = "dispatch_failed"
DELIVERY_UNKNOWN = "delivery_unknown_after_send"
# Operator-only terminal state — nothing automatic ever writes it (see module docstring).
UNKNOWN_RESOLVED_RETRYABLE = "unknown_resolved_retryable"

# --- reservation sets -------------------------------------------------------
CAP_RESERVED_STATES = (SENT, DISPATCH_PENDING, DISPATCH_ATTEMPTED, DELIVERY_UNKNOWN)
# P0-1: delivery_unknown reserves dedup too — an unprovable send is NOT
# auto-retryable; only an operator (via UNKNOWN_RESOLVED_RETRYABLE) frees it.
DEDUP_RESERVED_STATES = (SENT, DISPATCH_PENDING, DISPATCH_ATTEMPTED, DELIVERY_UNKNOWN)

# Frozen (code-level) reconciliation age. A dispatch whose lease heartbeat
# (``dispatch_state_updated_at``) has not advanced within this many seconds is
# treated as crashed and its lease is reclaimed. A send + promote/demote
# completes in well under a second, so 300s is a deterministic, conservative
# floor. FROZEN here per the reviewer ruling — NOT chosen ad hoc at runtime and
# NOT operator-tunable.
STALE_RECONCILE_SECONDS = 300

# B2 liveness: a HARD end-to-end deadline (frozen, code-level) for the ENTIRE
# provider send operation — including its internal pacing wait + 429 retry waits +
# the HTTP call. It is STRICTLY BELOW STALE_RECONCILE_SECONDS, so a send can never
# outlive the lease-heartbeat set at mark_attempted: reconcile only reclaims leases
# whose heartbeat is older than STALE_RECONCILE_SECONDS, and the send either
# completes or times out (→ delivery_unknown) before that. An active provider call
# therefore can NEVER be demoted by a concurrent reconciliation. Each lane wraps its
# send in ``asyncio.wait_for(..., timeout=SEND_OPERATION_DEADLINE_SECONDS)``.
SEND_OPERATION_DEADLINE_SECONDS = 240
assert SEND_OPERATION_DEADLINE_SECONDS < STALE_RECONCILE_SECONDS, (
    "the provider-operation deadline must be strictly below the stale-reconcile "
    "threshold so an active send can never be reclaimed as stale"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_lease() -> str:
    """Return a fresh, unique ownership token for one dispatch attempt."""
    return uuid.uuid4().hex


def _sql_in(states: tuple[str, ...]) -> str:
    """Render a tuple of states as a SQL ``IN (...)`` literal list (values are
    module constants, never user input — safe to inline)."""
    return "(" + ", ".join(f"'{s}'" for s in states) + ")"


def cap_reserved_in_clause() -> str:
    return _sql_in(CAP_RESERVED_STATES)


def dedup_reserved_in_clause() -> str:
    return _sql_in(DEDUP_RESERVED_STATES)


async def mark_attempted(db, row_id: int | None, *, lease: str) -> bool:
    """``dispatch_pending`` -> ``dispatch_attempted`` (committed IMMEDIATELY
    before the provider call). Guarded on the owner ``lease`` so it only advances
    THIS caller's pending row, and refreshes the lease heartbeat. Returns True iff
    the compare-and-set applied — a False means the lease was reclaimed (stale
    sweep) and the caller MUST abort the provider send."""
    if row_id is None:
        return False
    rc = await db.execute_write(
        f"UPDATE tg_alert_log SET outcome='{DISPATCH_ATTEMPTED}', "
        f"dispatch_state_updated_at=? "
        f"WHERE id=? AND outcome='{DISPATCH_PENDING}' AND dispatch_lease_token=?",
        (_now_iso(), row_id, lease),
    )
    return rc == 1


async def promote_sent(db, row_id: int | None, *, lease: str) -> bool:
    """``dispatch_attempted`` -> ``sent`` (ONLY after provider success). Guarded
    on the owner ``lease`` so a stale sweep that already moved the row to
    ``delivery_unknown_after_send`` can never be clobbered back to ``sent``.
    Returns True iff the row was actually promoted."""
    if row_id is None:
        return False
    rc = await db.execute_write(
        f"UPDATE tg_alert_log SET outcome='{SENT}', dispatch_state_updated_at=? "
        f"WHERE id=? AND outcome='{DISPATCH_ATTEMPTED}' AND dispatch_lease_token=?",
        (_now_iso(), row_id, lease),
    )
    return rc == 1


async def _read_row_state(db, row_id: int) -> tuple[str | None, str | None]:
    """Read the current ``(outcome, dispatch_lease_token)`` for a row — a plain
    read on the shared connection (no transaction needed)."""
    conn = getattr(db, "_conn", None)
    if conn is None:
        return (None, None)
    cur = await conn.execute(
        "SELECT outcome, dispatch_lease_token FROM tg_alert_log WHERE id=?",
        (row_id,),
    )
    row = await cur.fetchone()
    return (row[0], row[1]) if row is not None else (None, None)


async def recover_sent_after_confirmed_acceptance(
    db, row_id: int | None, *, lease: str
) -> bool:
    """Ownership-safe recovery (B2 durable-success consistency): a CONFIRMED
    provider acceptance whose row was moved to ``delivery_unknown_after_send`` by a
    racing reconcile (which preserves the lease token) is promoted unknown -> sent,
    guarded on the owner ``lease`` — so ONLY the acceptance owner can perform it.
    Records the recovery in ``detail`` (a durable DB audit trail) AND emits a
    structured ``alert_dispatch_recovery`` audit log capturing: the original lease,
    the current row state at recovery time (outcome + lease), the acceptance
    evidence, and whether the recovery actually changed the row. Returns True iff
    recovered.

    Under the SEND_OPERATION_DEADLINE_SECONDS guarantee this path is unreachable
    in practice (an active send cannot be reconciled); it exists so a confirmed
    acceptance ALWAYS ends in durable ``sent`` instead of a warning + inconsistent
    return."""
    if row_id is None:
        return False
    # Current row state at recovery time (audit input) — read BEFORE the CAS.
    state_at_recovery, lease_at_recovery = await _read_row_state(db, row_id)
    rc = await db.execute_write(
        f"UPDATE tg_alert_log SET outcome='{SENT}', dispatch_state_updated_at=?, "
        f"detail=COALESCE(detail, '') || "
        f"' [recovered: confirmed provider acceptance after reconcile]' "
        f"WHERE id=? AND outcome='{DELIVERY_UNKNOWN}' AND dispatch_lease_token=?",
        (_now_iso(), row_id, lease),
    )
    recovered = rc == 1
    log.info(
        "alert_dispatch_recovery",
        tg_alert_log_id=row_id,
        original_lease=lease,
        state_at_recovery=state_at_recovery,
        lease_at_recovery=lease_at_recovery,
        acceptance_evidence="provider_confirmed_acceptance",
        recovered=recovered,
    )
    return recovered


async def _is_sent_under_lease(db, row_id: int, lease: str) -> bool:
    outcome, row_lease = await _read_row_state(db, row_id)
    return outcome == SENT and row_lease == lease


async def finalize_confirmed_sent(db, row_id: int | None, *, lease: str) -> bool:
    """Persist a CONFIRMED provider acceptance as durable ``sent`` (B2). Contract:
    returns True IFF the row is now durably ``sent`` UNDER THIS LEASE — the caller
    MUST derive its returned/counted/logged outcome from this boolean so DB state,
    return value, counters, and logs can never disagree.

    Order: (1) the normal ``dispatch_attempted -> sent`` promotion; (2) if a racing
    reconcile already moved the row to ``delivery_unknown_after_send``, the
    ownership-safe unknown -> sent recovery; (3) idempotent short-circuit — if the
    row is ALREADY ``sent`` under our lease (acceptance already proven durable),
    return True without a further write. Returns False ONLY when ownership is lost
    AND durable recovery cannot independently prove acceptance (e.g. an operator
    resolved the row to ``unknown_resolved_retryable`` or a newer attempt rotated
    the lease token)."""
    if row_id is None:
        return False
    if await promote_sent(db, row_id, lease=lease):
        return True
    if await recover_sent_after_confirmed_acceptance(db, row_id, lease=lease):
        return True
    # Neither transition applied — succeed ONLY if the row is already durably 'sent'
    # under our lease (idempotent); otherwise ownership is lost -> False.
    return await _is_sent_under_lease(db, row_id, lease)


async def demote_failed(db, row_id: int | None, *, lease: str, detail: str) -> bool:
    """``dispatch_pending`` | ``dispatch_attempted`` -> ``dispatch_failed`` on a
    CONFIRMED non-delivery (preprocessing error, cancel before send, or a
    provider error return). Guarded on the owner ``lease``. Returns success."""
    if row_id is None:
        return False
    rc = await db.execute_write(
        f"UPDATE tg_alert_log SET outcome='{DISPATCH_FAILED}', detail=?, "
        f"dispatch_state_updated_at=? "
        f"WHERE id=? AND outcome IN ('{DISPATCH_PENDING}', '{DISPATCH_ATTEMPTED}') "
        f"AND dispatch_lease_token=?",
        (detail, _now_iso(), row_id, lease),
    )
    return rc == 1


async def mark_delivery_unknown(
    db, row_id: int | None, *, lease: str, detail: str
) -> bool:
    """``dispatch_attempted`` -> ``delivery_unknown_after_send`` when the send was
    interrupted (e.g. cancelled) after it began and its result is unprovable.
    Guarded on the owner ``lease``. Returns success."""
    if row_id is None:
        return False
    rc = await db.execute_write(
        f"UPDATE tg_alert_log SET outcome='{DELIVERY_UNKNOWN}', detail=?, "
        f"dispatch_state_updated_at=? "
        f"WHERE id=? AND outcome='{DISPATCH_ATTEMPTED}' AND dispatch_lease_token=?",
        (detail, _now_iso(), row_id, lease),
    )
    return rc == 1


def merge_error_detail(detail: dict | str | None, error: str) -> str:
    """Build a detail JSON string preserving provenance + the failure reason."""
    if isinstance(detail, dict):
        return json.dumps({**detail, "error": error}, sort_keys=True)
    if detail:
        return json.dumps({"detail": str(detail), "error": error}, sort_keys=True)
    return json.dumps({"error": error}, sort_keys=True)


async def reconcile_stale(db, signal_type: str, *, now: datetime | None = None) -> dict:
    """Deterministic *expired-lease* reclamation for one ``signal_type`` (P0-2).

    Owner: each dispatch lane calls this at dispatch entry (claim time), so the
    sweep is deterministic and self-owned — no separate daemon required. Age is
    measured from the lease heartbeat ``dispatch_state_updated_at`` (falling back
    to ``alerted_at`` only for legacy rows written before the lease column), NOT
    from ``alerted_at`` — so a dispatch that is actively preprocessing or sending
    (and therefore refreshing its heartbeat) is NEVER reclaimed. The threshold is
    the FROZEN ``STALE_RECONCILE_SECONDS``.

      stale ``dispatch_pending``   -> ``dispatch_failed`` (never reached the
                                      provider — a preprocessing crash),
      stale ``dispatch_attempted`` -> ``delivery_unknown_after_send`` (send began
                                      but its result is unprovable).

    The swept rows have their heartbeat stamped to ``now`` so the sweep is
    idempotent. Returns ``{"pending_failed": n, "attempted_unknown": m}``.
    """
    if getattr(db, "_conn", None) is None:
        return {"pending_failed": 0, "attempted_unknown": 0}
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    cutoff = (now - timedelta(seconds=STALE_RECONCILE_SECONDS)).isoformat()
    pending_failed = await db.execute_write(
        f"UPDATE tg_alert_log SET outcome='{DISPATCH_FAILED}', "
        f"dispatch_state_updated_at=?, "
        f"detail=COALESCE(detail, '') || ' [reconciled: stale pending -> failed]' "
        f"WHERE signal_type=? AND outcome='{DISPATCH_PENDING}' "
        f"AND COALESCE(dispatch_state_updated_at, alerted_at) < ?",
        (now_iso, signal_type, cutoff),
    )
    attempted_unknown = await db.execute_write(
        f"UPDATE tg_alert_log SET outcome='{DELIVERY_UNKNOWN}', "
        f"dispatch_state_updated_at=?, "
        f"detail=COALESCE(detail, '') || ' [reconciled: stale attempted -> unknown]' "
        f"WHERE signal_type=? AND outcome='{DISPATCH_ATTEMPTED}' "
        f"AND COALESCE(dispatch_state_updated_at, alerted_at) < ?",
        (now_iso, signal_type, cutoff),
    )
    return {"pending_failed": pending_failed, "attempted_unknown": attempted_unknown}
