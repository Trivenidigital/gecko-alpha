"""F3 — writer for the append-only control-plane event ledger (`alert_events`).

WHY A LEDGER AND NOT LOGS. prod journald retention collapses to minutes during
the 03:00 backup window, so any control-plane fact that lives only in a log line
is unreconstructable after the fact. And the decisive suppression transitions
(initial latch, re-latch, clear, parole re-arm) plus the parole slot decrement
emitted NO log line at all — they existed only as mutated `combo_performance`
rows, which record the current state and say nothing about how it was reached.

WHY IT ALMOST NEVER RAISES. This ledger OBSERVES the control plane; it must
never be able to break it. Failure paths here are swallowed and logged
(`alert_event_write_failed`). The exception-swallow is deliberate and is
exercised by tests in BOTH transaction modes — an untested `except` block is
untested code, and this one exists precisely for the case nobody rehearses.

TWO documented exceptions to "never raises", both deliberate:

1. `asyncio.CancelledError` is a `BaseException` and propagates by design. It
   means the process is shutting down or the task was cancelled; swallowing it
   would make the ledger a place where cancellation goes to die.
2. In `managed_txn=True`, a failure that ABORTED the caller's open transaction
   is re-raised — see :func:`record_alert_event`. Swallowing that one would be
   the worst outcome in this module: the caller would carry on believing its
   state change is pending when SQLite has already discarded it.

TRANSACTION MODES. `managed_txn=True` means the CALLER owns the transaction and
will commit it: the row is a plain INSERT with no commit and no rollback, so the
event is atomic with the state change it records. `managed_txn=False` opens the
writer's own short transaction under `db._txn_lock` — used on the delivery axis,
where the state change already committed and the event is a separate fact.

Callers that already hold `db._txn_lock` MUST pass `managed_txn=True`:
`asyncio.Lock` is not reentrant, so the self-committed mode would deadlock. That
is enforced by review, not by a timeout — every unmanaged call site is verified
lock-free. An earlier draft used `asyncio.wait_for(lock.acquire(), ...)` as a
backstop; it was removed because a cancelled `acquire()` can leave the lock
permanently held, which converts a recoverable mistake into a wedged pipeline.

`delivery_result` is a general-purpose OUTCOME field, not only a delivery
verdict. On `alert_*` rows it carries `'ok'` / `'error:<ExcType>'`; on
`parole_slot_refunded` it carries `'ok'` / `'stale_generation'`; on
`reversal_pending_recorded`, `'ok'` / `'no_30d_row'`; on `marker_anomaly`, the
anomaly reason. One column, read in the context of its `event_type`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import structlog

log = structlog.get_logger()

# Mirrors the CHECK constraint in `_migrate_alert_events_v1`. The DB is the
# authority — this set exists so callers can be linted/read, not to gate writes.
EVENT_TYPES = frozenset(
    {
        "suppression_transition",
        "parole_slot_spent",
        "parole_slot_refunded",
        "reversal_pending_recorded",
        "alert_dispatched",
        "alert_delivered",
        "alert_failed",
        "marker_stamped",
        "marker_cleared",
        "marker_anomaly",
        "refresh_completed",
        "ledger_installed",
        "parole_denied",
    }
)

_INSERT_SQL = (
    "INSERT INTO alert_events "
    "(created_at, event_type, combo_key, signal_type, alert_source, transition, "
    " detected_at, delivery_result, retry, payload_hash, state_json, detail) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def payload_digest(text: str) -> str:
    """SHA-256 of the EXACT string that was sent.

    Hash the sent bytes and nothing else: no normalisation, no re-rendering, no
    `Decimal.normalize()`-style ambient formatting. The point of the digest is to
    prove which body went out, which is only true if it is taken over the body
    that went out.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode_state(**fields) -> str:
    """Canonical JSON for the `state_json` column. `sort_keys` so two rows
    describing the same state compare equal as text."""
    return json.dumps(fields, sort_keys=True, default=str)


def generation_state(row, *, prefix: str) -> dict:
    """Flatten a `combo_performance` 30d row (or ``None``) into the generation
    quadruple, key-prefixed so a before/after pair can share one dict.

    ``None`` means the row did not exist — recorded as explicit nulls rather
    than omitted keys, so "absent" and "unread" are distinguishable later.
    """
    if row is None:
        return {
            f"{prefix}_suppressed": None,
            f"{prefix}_suppressed_at": None,
            f"{prefix}_parole_at": None,
            f"{prefix}_parole_trades_remaining": None,
        }
    return {
        f"{prefix}_suppressed": row["suppressed"],
        f"{prefix}_suppressed_at": row["suppressed_at"],
        f"{prefix}_parole_at": row["parole_at"],
        f"{prefix}_parole_trades_remaining": row["parole_trades_remaining"],
    }


async def record_alert_event(
    db,
    *,
    event_type: str,
    combo_key: str | None = None,
    signal_type: str | None = None,
    alert_source: str | None = None,
    transition: str | None = None,
    detected_at: str | None = None,
    delivery_result: str | None = None,
    retry: bool | int | None = None,
    payload_hash: str | None = None,
    state_json: str | None = None,
    detail: str | None = None,
    managed_txn: bool = False,
) -> None:
    """Append one row to `alert_events`. See the module docstring for the two
    documented cases in which this raises.

    `managed_txn=True` — the caller owns an open transaction and will commit it;
    this is a bare INSERT. A failed INSERT has TWO possible effects, and the
    handler distinguishes them rather than assuming the benign one:

    * **statement-level abort** (constraint violations — SQLite's default
      ABORT conflict resolution): only the INSERT is undone, the caller's
      transaction survives intact, and the failure is swallowed and logged.
      This is the common case and the one a ledger must never escalate.
    * **transaction-level abort** (the IO class: SQLITE_FULL, SQLITE_IOERR,
      SQLITE_BUSY, SQLITE_NOMEM): SQLite discards the whole transaction. The
      failure is re-raised, because the caller would otherwise continue as if
      its state change were still pending.

    `managed_txn=False` — acquire `db._txn_lock`, INSERT, commit. On failure the
    writer rolls its own transaction back so the shared connection is never
    handed to the next writer half-open.
    """
    params = (
        datetime.now(timezone.utc).isoformat(),
        event_type,
        combo_key,
        signal_type,
        alert_source,
        transition,
        detected_at,
        delivery_result,
        # 0/1, not a count. `bool` IS an `int`, so normalise explicitly rather
        # than binding a raw bool and hoping the reader interprets it.
        None if retry is None else (1 if retry else 0),
        payload_hash,
        state_json,
        detail,
    )

    if managed_txn:
        # Sampled BEFORE the write so the handler can tell "the caller had a
        # transaction and SQLite threw it away" from "there was never one".
        was_in_txn = db._conn.in_transaction
        try:
            await db._conn.execute(_INSERT_SQL, params)
        except Exception as exc:
            _log_write_failure(exc, event_type, combo_key, managed_txn=True)
            if was_in_txn and not db._conn.in_transaction:
                # The failure ABORTED the caller's transaction rather than just
                # the statement. SQLite does this for the IO class —
                # SQLITE_FULL / IOERR / BUSY / NOMEM — and disk-full has real
                # history on this box. Swallowing here would be the worst
                # outcome in this module: `_open_gate` would return an
                # admission whose `parole_trades_remaining` decrement had
                # already been discarded, i.e. silent over-admission (D1
                # class). Re-raise so the caller's own failure path runs.
                raise
        return

    try:
        lock = db._txn_lock
        if lock is None:
            raise RuntimeError("Database._txn_lock is None — not initialized")
        await lock.acquire()
        try:
            try:
                await db._conn.execute(_INSERT_SQL, params)
                await db._conn.commit()
            except BaseException:
                try:
                    await db._conn.rollback()
                except Exception as rb_err:
                    # Never silent: if the rollback ALSO failed, the shared
                    # connection may be left half-open for the next writer, and
                    # that is a bigger problem than the lost ledger row this
                    # handler was reached for.
                    log.exception("alert_event_rollback_failed", err=str(rb_err))
                raise
        finally:
            lock.release()
    except Exception as exc:
        _log_write_failure(exc, event_type, combo_key, managed_txn=False)


def _log_write_failure(exc, event_type, combo_key, *, managed_txn: bool) -> None:
    """The one place the swallowed failure surfaces.

    `log`, not `logger` — this module binds the structlog logger under that name
    at module scope. A wrong name here would raise NameError from inside the
    handler whose whole job is to contain errors, converting a lost ledger row
    into a broken control path.
    """
    log.error(
        "alert_event_write_failed",
        err_id="ALERT_EVENT_WRITE",
        event_type=event_type,
        combo_key=combo_key,
        managed_txn=managed_txn,
        err=str(exc),
        err_type=type(exc).__name__,
    )
