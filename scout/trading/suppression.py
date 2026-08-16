"""Suppression entry-gate (spec §5.2).

Must be imported only from `signals.py` dispatchers. The module-level state
(`_fallback_timestamps`, `_last_alerted_ts`) is process-local, which is safe
because gecko-alpha runs a single event-loop process.
"""

from __future__ import annotations

import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiohttp
import aiosqlite
import structlog

from scout import alerter
from scout.db import Database
from scout.timeutil import parole_window_open
from scout.trading.alert_events import (
    denial_digest,
    encode_state,
    payload_digest,
    record_alert_event,
)

log = structlog.get_logger()

# The ONLY `should_open` reason that consumes a parole retest slot. Every other
# reason returns without touching `parole_trades_remaining`, so this string is
# the authoritative "a slot was spent on this call" signal.
PAROLE_RETEST_REASON = "parole_retest"

_FALLBACK_WINDOW_SEC = 3600
_fallback_timestamps: "deque[float]" = deque()
# Initialize to -inf so the very first alert always passes the cooldown check.
# Using 0.0 is wrong on Linux where time.monotonic() can be small near boot
# (CI runners) — `now_ts - 0.0 >= cooldown` fails until the process has been
# alive for `cooldown` seconds. -inf removes that OS-dependence.
_last_alerted_ts: float = float("-inf")


def get_fallback_count() -> int:
    """Return current fallback-counter size. Public accessor for weekly digest."""
    return len(_fallback_timestamps)


async def should_open(db: Database, combo_key: str, *, settings) -> tuple[bool, str]:
    """Entry-gate: returns (allow, reason). Fail-open on DB error.

    `settings` is required so the fail-open alert can (a) respect
    `FEEDBACK_FALLBACK_ALERT_THRESHOLD` / `_COOLDOWN_SEC` and (b) build the
    real alerter.send_telegram_message(text, session, settings) payload.
    """
    allow, reason, _generation = await _open_gate(db, combo_key, settings=settings)
    return (allow, reason)


async def _open_gate(
    db: Database, combo_key: str, *, settings
) -> tuple[bool, str, tuple | None]:
    """`should_open` + the parole GENERATION the decision was made against.

    The third element is non-None only when a parole slot was actually spent.
    It identifies the suppression generation the slot came from, so a refund
    can prove it is returning the slot to that same generation rather than to
    a replacement one installed by `combo_refresh` in the meantime.
    """
    try:
        cursor = await db._conn.execute(
            "SELECT suppressed, parole_at, parole_trades_remaining "
            "FROM combo_performance WHERE combo_key = ? AND window = '30d'",
            (combo_key,),
        )
        row = await cursor.fetchone()
    except aiosqlite.OperationalError as e:
        msg = str(e).lower()
        if "locked" in msg or "busy" in msg:
            await _record_fallback(db, combo_key, str(e), settings)
            return (True, "db_error_fallback_allow", None)
        log.exception(
            "suppression_db_operational_error",
            err_id="SUPP_DB_OP",
            combo_key=combo_key,
        )
        return (False, "error", None)
    except aiosqlite.Error:
        log.exception(
            "suppression_db_error",
            err_id="SUPP_DB_CORRUPT",
            combo_key=combo_key,
        )
        return (False, "error", None)

    if row is None:
        return (True, "cold_start", None)

    suppressed, parole_at, _ = row[0], row[1], row[2]

    if not suppressed:
        return (True, "ok", None)

    if parole_at is None:
        return (False, "suppressed", None)

    try:
        parole_dt = datetime.fromisoformat(parole_at)
    except (ValueError, TypeError) as e:
        await _record_fallback(db, combo_key, f"parole_at parse: {e}", settings)
        return (True, "db_error_fallback_allow", None)
    if parole_dt.tzinfo is None:
        parole_dt = parole_dt.replace(tzinfo=timezone.utc)
    if parole_dt > datetime.now(timezone.utc):
        return (False, "suppressed", None)

    # Everything above is a LOCK-FREE FAST PATH and is ADVISORY ONLY. It decides
    # whether entering the transaction is worthwhile; it decides nothing else.
    # `combo_refresh` can clear or re-latch suppression between that read and
    # the lock below, so the locked block re-validates the FULL admission state
    # (suppressed + parole_at + remaining), not just the counter. Re-reading
    # only `parole_trades_remaining` here was a TOCTOU: a re-suppression
    # installing a fresh future parole_at with a full slot budget would be seen
    # as "5 > 0" and admit a trade inside the NEW lock period.
    #
    # The asyncio.Lock ensures that two coroutines within the same event loop
    # (e.g. suppression._open_gate and combo_refresh.refresh_combo) cannot
    # interleave their BEGIN...COMMIT blocks across asyncio suspend points.
    # SQLite's per-file locking still protects against separate Connection
    # objects (see test_concurrent_decrement_grants_only_one).
    if db._txn_lock is None:
        raise RuntimeError(
            "Database._txn_lock is None — Database.initialize() was not awaited "
            "before should_open(). A fresh ephemeral Lock here would silently "
            "break mutual exclusion across concurrent callers."
        )
    # Set by the locked-and-busy branch below; every other path inside the lock
    # returns, so reaching the tail of this function means it was set.
    deferred_fallback_err: str | None = None
    async with db._txn_lock:
        try:
            await db._conn.execute("BEGIN IMMEDIATE")
            cur = await db._conn.execute(
                "SELECT suppressed, suppressed_at, parole_at, "
                "parole_trades_remaining "
                "FROM combo_performance WHERE combo_key = ? AND window = '30d'",
                (combo_key,),
            )
            reread = await cur.fetchone()
            if reread is None:
                await db._conn.execute("COMMIT")
                return (True, "cold_start", None)
            supp_now, supp_at_now, parole_at_now, remaining = (
                reread[0],
                reread[1],
                reread[2],
                reread[3],
            )

            # Suppression lifted while we were queueing for the lock.
            if not supp_now:
                await db._conn.execute("COMMIT")
                return (True, "ok", None)

            # Re-suppressed / re-paroled while we were queueing: the window we
            # validated on the fast path no longer exists. Deny rather than
            # spend a slot from a generation whose lock period is still running.
            if not parole_window_open(parole_at_now):
                # Recorded IN this transaction, before the COMMIT that closes
                # it: the denial and the state it was decided against are one
                # durable fact. A denial is the gate working correctly, not an
                # anomaly — hence its own event type rather than
                # `marker_anomaly`, which would misfile a normal decision as a
                # fault.
                #
                # PER-OCCURRENCE, unlike the `parole_exhausted` branch below.
                # This is a RACE, not a latched state: it fires only when
                # `combo_refresh` re-latches inside the window between the
                # lock-free read and the locked reservation, so each row is a
                # distinct event worth counting. There is no repetition here to
                # collapse, and collapsing it would destroy the only signal that
                # says how often the TOCTOU is actually being hit.
                #
                # That is a COUNTED fact, not a reading of this code path:
                # `SELECT transition, COUNT(*) FROM alert_events WHERE
                # event_type='parole_denied' GROUP BY transition` on prod over
                # 2026-08-15T10:48Z..2026-08-16T03:53Z returned exactly one row,
                # `parole_exhausted|20094` — ZERO rows for this branch in ~17h.
                # Recorded because assuming a branch is rare because it READS
                # like an edge case, without counting it, is precisely what
                # produced the 20,094-row fanout the branch below now fixes.
                await record_alert_event(
                    db,
                    event_type="parole_denied",
                    combo_key=combo_key,
                    transition="generation_changed_before_reservation",
                    detected_at=datetime.now(timezone.utc).isoformat(),
                    state_json=encode_state(
                        suppressed=supp_now,
                        suppressed_at=supp_at_now,
                        parole_at=parole_at_now,
                        parole_trades_remaining=remaining,
                    ),
                    detail="suppression was re-latched between the lock-free "
                    "read and the locked reservation; the validated window no "
                    "longer exists",
                    managed_txn=True,
                )
                await db._conn.execute("COMMIT")
                log.info(
                    "parole_generation_changed_before_reservation",
                    combo_key=combo_key,
                    detail="suppression state was re-latched between the "
                    "lock-free read and the locked reservation; no admission",
                )
                return (False, "suppressed", None)

            if remaining is None or remaining <= 0:
                # The parole budget is spent. This is the denial that explains
                # a stalled retest after the fact, and before the ledger it left
                # no trace at all — not even a log line.
                #
                # Written ONCE per distinct denial state, not once per attempt.
                # This branch is not the rare event the original brief assumed:
                # once a combo latches it is the steady state, so every dispatch
                # attempt re-enters here and a per-attempt row appended 20,094
                # byte-identical rows in 17h of prod across 3 combos. The digest
                # keys the row on combo + reason + generation, so a re-latch or a
                # different denial reason still records while the repetition
                # collapses — the same shape as the `suppression_transition`
                # delta gate and the `page_rearm` stamped-marker guard.
                await record_alert_event(
                    db,
                    event_type="parole_denied",
                    combo_key=combo_key,
                    transition="parole_exhausted",
                    detected_at=datetime.now(timezone.utc).isoformat(),
                    payload_hash=denial_digest(
                        combo_key=combo_key,
                        denial_reason="parole_exhausted",
                        suppressed=supp_now,
                        suppressed_at=supp_at_now,
                        parole_at=parole_at_now,
                    ),
                    state_json=encode_state(
                        suppressed=supp_now,
                        suppressed_at=supp_at_now,
                        parole_at=parole_at_now,
                        parole_trades_remaining=remaining,
                    ),
                    managed_txn=True,
                    dedupe_on_payload_hash=True,
                )
                await db._conn.execute("COMMIT")
                return (False, "parole_exhausted", None)
            await db._conn.execute(
                "UPDATE combo_performance SET parole_trades_remaining = ? "
                "WHERE combo_key = ? AND window = '30d'",
                (remaining - 1, combo_key),
            )
            # F3: the decrement is the admission decision. Recorded IN this
            # transaction (`managed_txn=True`) so the ledger row and the slot
            # spend are the same durable fact — a decrement with no event, or
            # an event with no decrement, would both be lies about admission.
            await record_alert_event(
                db,
                event_type="parole_slot_spent",
                combo_key=combo_key,
                transition="parole_retest",
                # The spend time, not the parole boundary. `parole_at` is a
                # property of the generation and already appears in
                # `state_json`; duplicating it here made `detected_at` mean
                # something different on this row than on every other one.
                detected_at=datetime.now(timezone.utc).isoformat(),
                state_json=encode_state(
                    suppressed=supp_now,
                    suppressed_at=supp_at_now,
                    parole_at=parole_at_now,
                    parole_trades_remaining_before=remaining,
                    parole_trades_remaining_after=remaining - 1,
                ),
                managed_txn=True,
            )
            await db._conn.commit()
            return (
                True,
                PAROLE_RETEST_REASON,
                (supp_now, supp_at_now, parole_at_now),
            )
        except aiosqlite.OperationalError as e:
            try:
                await db._conn.execute("ROLLBACK")
            except aiosqlite.Error as rb_err:
                log.warning(
                    "suppression_rollback_failed",
                    combo_key=combo_key,
                    err=str(rb_err),
                    err_id="SUPP_ROLLBACK",
                )
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                # DEFERRED past the lock release, not called here. This branch
                # runs with `db._txn_lock` HELD, and `_record_fallback` now
                # writes ledger rows through `record_alert_event`, which takes
                # that same non-reentrant lock — calling it in place would
                # self-deadlock until the writer's bounded acquire timed out.
                # Deferring is also strictly better for the pre-existing
                # aiohttp send it wraps: that no longer pins the trading lock
                # for the Telegram client timeout.
                deferred_fallback_err = f"parole_decrement: {e}"
            else:
                log.exception(
                    "suppression_db_operational_error",
                    err_id="SUPP_DB_OP",
                    combo_key=combo_key,
                )
                return (False, "error", None)
        except aiosqlite.Error:
            try:
                await db._conn.execute("ROLLBACK")
            except aiosqlite.Error:
                pass
            log.exception(
                "suppression_db_error",
                err_id="SUPP_DB_CORRUPT",
                combo_key=combo_key,
            )
            return (False, "error", None)
        except BaseException:
            # NON-aiosqlite escapes. Both handlers above already roll back; this
            # one exists because everything else that can be raised inside the
            # locked block — a wiring bug in the ledger writer, an assertion, a
            # cancellation — would otherwise leave `BEGIN IMMEDIATE` OPEN.
            #
            # MUST BE THE LAST CLAUSE. Python matches handlers in source order,
            # so putting this above the two aiosqlite handlers would swallow the
            # busy/locked branch and convert its deliberate fail-OPEN (see
            # `deferred_fallback_err`) into a raise — the dispatcher would count
            # an error and SKIP the candidate, inverting the contract stated in
            # `should_open`'s own docstring.
            #
            # WHAT THE LEAK COSTS, measured rather than reasoned:
            #   * The connection keeps the RESERVED write lock it took at
            #     BEGIN IMMEDIATE, so every OTHER PROCESS on this database
            #     (dashboard, cron, backup) gets "database is locked" for as
            #     long as the leak lasts — unbounded if no dispatch follows.
            #   * The next `BEGIN IMMEDIATE` raises OperationalError "cannot
            #     start a transaction within a transaction", whose message
            #     contains neither "locked" nor "busy", so it falls to the
            #     `else` above and returns (False, "error") — ONE admission
            #     LOST, not spent. That handler's own ROLLBACK then clears the
            #     leak, so the gate self-heals on the following call rather
            #     than staying down.
            #   * Slot accounting is structurally untouched: this raise happens
            #     on the DENIAL branch, which never reaches the decrement, and
            #     `_open_gate` is awaited outside `parole_reservation`'s try, so
            #     no `confirm()` runs. Verified 0 -> 0 on the raising combo and
            #     3 -> 3 on a healthy one.
            # So the ledger and the slot counters both keep looking clean while
            # a foreign process is locked out. That invisibility is the reason
            # this catches BaseException rather than Exception.
            try:
                # Guarded rather than blind: the two handlers above may already
                # have rolled back, and ROLLBACK with no active transaction is
                # itself an error — which must never displace the real one.
                if db._conn.in_transaction:
                    await db._conn.execute("ROLLBACK")
            except BaseException as rb_err:
                # BaseException, not Exception: aiosqlite hands ROLLBACK to a
                # worker thread and awaits a future, so during loop shutdown
                # this can raise CancelledError. Under cancellation the rollback
                # therefore is NOT guaranteed — no promise is made here beyond
                # never masking the original failure.
                log.warning(
                    "suppression_rollback_failed",
                    combo_key=combo_key,
                    err=str(rb_err),
                    err_id="SUPP_ROLLBACK",
                )
            # Bare `raise` — re-raises the ORIGINAL exception, never the
            # rollback's. A cleanup failure that swallows the real error is
            # worse than the leak this clause exists to close.
            raise

    return await _open_gate_tail(db, combo_key, settings, deferred_fallback_err)


async def _open_gate_tail(
    db: Database, combo_key: str, settings, deferred_fallback_err: str | None
) -> tuple[bool, str, tuple | None]:
    """Everything :func:`_open_gate` does after releasing ``db._txn_lock``.

    Extracted so the invariant guard below is reachable by a test. Reaching
    here means the locked block fell through without returning, which today
    happens on exactly one path: the locked-and-busy fail-open, which sets
    ``deferred_fallback_err``.

    The guard exists because the FALL-THROUGH is the dangerous direction. This
    function's default answer is "admit the trade" (fail-open), so a future edit
    that adds a non-returning path inside the lock would silently start
    admitting trades against a combo whose suppression state was never
    validated. Deny instead: an unexplained fall-through is a bug, and the safe
    reading of a bug at an entry gate is "do not open".
    """
    if deferred_fallback_err is None:
        log.error(
            "suppression_open_gate_fell_through",
            combo_key=combo_key,
            err_id="SUPP_FALLTHROUGH",
            detail="the locked block exited without returning and without "
            "recording a fail-open reason; denying rather than admitting an "
            "unvalidated trade",
        )
        return (False, "error", None)
    await _record_fallback(db, combo_key, deferred_fallback_err, settings)
    return (True, "db_error_fallback_allow", None)


class ParoleReservation:
    """One parole retest slot, held for the duration of one admission attempt.

    ``should_open`` decrements ``parole_trades_remaining`` at the GATE, before
    the downstream checks that can decline the admission. Without this token a
    declined admission permanently consumes a retest slot: on 2026-08-08 the
    ``losers_contrarian`` pilot lost 2 of its 5 slots to a token that could
    never open (lane-scoped prefilter vs ``open_trade``'s global dedup), and
    was left with 3 observations against a registered 120.

    A reservation is bound to ONE suppression GENERATION — the
    ``(suppressed, suppressed_at, parole_at)`` triple observed inside the
    locked transaction that spent the slot. ``combo_refresh`` changes that
    triple whenever it clears or re-latches suppression, and leaves it
    untouched on its preserve branch; neither the decrement nor the refund
    writes it. A refund therefore proves it is returning the slot to the same
    generation it came from — a stale refund into a REPLACEMENT generation
    would be over-admission against a fresh lock period.

    SCOPE — deliberately PROCESS-LOCAL and TRANSACTION-LOCAL. The token lives
    only in this process's memory for the lifetime of its ``async with`` block
    and is **not** crash-durable. A process death between decrement and refund
    leaks the slot, which UNDER-admits. That is the required failure direction:
    the decrement stays pessimistic and is never deferred to commit time. Do
    not add cross-restart idempotency here — the invariant is "never
    over-admit", not "never lose a slot".
    """

    __slots__ = (
        "allow",
        "reason",
        "_db",
        "_settings",
        "_combo_key",
        "_generation",
        "_slot_taken",
        "_settled",
    )

    def __init__(
        self,
        db: Database,
        settings,
        combo_key: str,
        allow: bool,
        reason: str,
        generation: tuple | None,
    ):
        self.allow = allow
        self.reason = reason
        self._db = db
        self._settings = settings
        self._combo_key = combo_key
        self._generation = generation
        self._slot_taken = reason == PAROLE_RETEST_REASON and generation is not None
        self._settled = False

    @property
    def slot_taken(self) -> bool:
        """Did this call actually spend a parole slot?"""
        return self._slot_taken

    @property
    def settled(self) -> bool:
        """Has this token been resolved (confirmed or refunded)?"""
        return self._settled

    def confirm(self) -> None:
        """Mark the slot as legitimately spent — call ONLY on a verified commit.

        Verified commit means ``engine.open_trade`` returned a trade id. A
        ``None`` return is a verified NO-commit (every such path in
        ``open_trade`` / ``paper.execute_buy`` precedes ``conn.commit()``), and
        an exception is AMBIGUOUS — never confirm or refund on one.
        """
        self._settled = True

    async def _settle_refund(self) -> None:
        """Return the slot if it was taken and never confirmed. Idempotent.

        INTERNAL — driven solely by ``parole_reservation``'s exit. Dispatchers
        must not call it: a manual refund followed by a commit would
        over-admit, which would make the invariant a matter of caller
        discipline rather than structure.
        """
        if self._settled or not self._slot_taken:
            return
        # Flip BEFORE awaiting: a re-entrant call across the await point must
        # be a no-op, so double refund is structurally impossible.
        self._settled = True
        await _refund_parole_slot(
            self._db, self._combo_key, self._settings, self._generation
        )


@asynccontextmanager
async def parole_reservation(db: Database, combo_key: str, *, settings):
    """Run the entry gate and guarantee an unspent parole slot is returned.

    Refund on normal exit without ``confirm()`` (covers every caller-side
    decline: suppressed, unpriced, pilot-cap, and ``open_trade`` returning
    ``None``). NEVER refund when the body raises.
    """
    allow, reason, generation = await _open_gate(db, combo_key, settings=settings)
    res = ParoleReservation(db, settings, combo_key, allow, reason, generation)
    try:
        yield res
    except BaseException:
        # AMBIGUOUS — after `paper.execute_buy` returns a durable trade id,
        # `open_trade` still awaits `_emit_decision` and then
        # `_spawn_tg_alert`, neither of which is individually guarded. An
        # exception can therefore surface with the paper_trades row ALREADY
        # committed. Refunding here would hand back a slot for a trade that
        # exists — over-admission. Leak it instead.
        #
        # (`stamp_entry_snapshot` is NOT such a source: `execute_buy` wraps it
        # in its own try/except after the commit, so it cannot escape.)
        #
        # The `raise` below already skips the refund, so this `confirm()` is
        # belt-and-braces — it settles the TOKEN, so that moving the refund
        # into a `finally:` (the natural refactor instinct) still cannot
        # refund an ambiguous outcome. Pinned by
        # test_reservation_token_is_settled_after_exception.
        res.confirm()
        raise
    await res._settle_refund()


async def _refund_parole_slot(
    db: Database, combo_key: str, settings, generation: tuple | None
) -> None:
    """Give one unspent retest slot back — atomically, bounded, and in-generation.

    Guarded on ``generation``: the refund only lands if
    ``(suppressed, suppressed_at, parole_at)`` still match the values observed
    when the slot was taken. If ``combo_refresh`` cleared or re-latched
    suppression in between, the slot belongs to a generation that no longer
    exists and the refund is a NO-OP — crediting it to the replacement
    generation would grant an extra admission against a fresh lock period.

    Also clamped at ``FEEDBACK_PAROLE_RETEST_TRADES``: the schema stores only
    the remaining count, never the original grant size, so the ceiling is the
    only available bound.

    Never raises into the dispatcher: a bookkeeping failure must not break
    dispatch. A failed or stale refund leaks the slot and under-admits.
    """
    if generation is None:
        return
    if db._txn_lock is None:
        raise RuntimeError(
            "Database._txn_lock is None — Database.initialize() was not awaited "
            "before parole_reservation(). A fresh ephemeral Lock here would "
            "silently break mutual exclusion against the decrement."
        )
    ceiling = settings.FEEDBACK_PAROLE_RETEST_TRADES
    async with db._txn_lock:
        try:
            await db._conn.execute("BEGIN IMMEDIATE")
            cur = await db._conn.execute(
                "SELECT parole_trades_remaining FROM combo_performance "
                "WHERE combo_key = ? AND window = '30d'",
                (combo_key,),
            )
            pre_row = await cur.fetchone()
            remaining_before = pre_row[0] if pre_row else None
            cur = await db._conn.execute(
                "UPDATE combo_performance "
                "SET parole_trades_remaining = "
                "    MIN(COALESCE(parole_trades_remaining, 0) + 1, ?) "
                "WHERE combo_key = ? AND window = '30d' "
                # `IS` (not `=`) — NULL-safe, so a cleared generation whose
                # suppressed_at/parole_at are NULL compares correctly instead
                # of silently matching nothing for the wrong reason.
                "  AND suppressed IS ? AND suppressed_at IS ? AND parole_at IS ?",
                (ceiling, combo_key, *generation),
            )
            # THIS statement's affected-row count — never
            # `Connection.total_changes`, which is connection-wide and
            # cumulative. The refund runs on the shared connection, so an
            # unrelated write landing between the two reads would make a
            # generation-bound UPDATE that matched ZERO rows report success,
            # defeating the stale-generation check exactly as it did at the two
            # sibling marker sites (combo_refresh.py).
            changed = cur.rowcount
            clamped = (
                changed != 0
                and remaining_before is not None
                and remaining_before + 1 > ceiling
            )
            # F3: recorded in the SAME transaction as the credit, so the ledger
            # cannot claim a refund that rolled back.
            await record_alert_event(
                db,
                event_type="parole_slot_refunded",
                combo_key=combo_key,
                delivery_result="ok" if changed else "stale_generation",
                detail="clamped at ceiling" if clamped else None,
                state_json=encode_state(
                    suppressed=generation[0],
                    suppressed_at=generation[1],
                    parole_at=generation[2],
                    parole_trades_remaining_before=remaining_before,
                    ceiling=ceiling,
                    rows_changed=changed,
                ),
                managed_txn=True,
            )
            await db._conn.commit()
            if changed == 0:
                log.info(
                    "parole_refund_stale_generation",
                    combo_key=combo_key,
                    detail="suppression generation changed between reservation "
                    "and refund; slot NOT credited to the replacement "
                    "generation (leaks one slot — under-admits)",
                )
            else:
                log.info(
                    "parole_slot_refunded",
                    combo_key=combo_key,
                    ceiling=ceiling,
                )
        except aiosqlite.Error as e:
            try:
                await db._conn.execute("ROLLBACK")
            except aiosqlite.Error:
                pass
            log.warning(
                "parole_refund_failed",
                combo_key=combo_key,
                err=str(e),
                err_id="SUPP_REFUND",
                detail="slot leaked — under-admits, never over-admits",
            )


async def _record_fallback(db: Database, combo_key: str, err: str, settings) -> None:
    """Log + maintain the fail-open counter; fire Telegram alert with cooldown.

    ``db`` is threaded through solely so the §12b send can be bracketed with F3
    ledger rows. Every call site reaches this from ``_open_gate``, which already
    holds it. Ledger writes here are best-effort by construction: this path fires
    when the DB is degraded, so the ledger INSERT may well fail too — the writer
    swallows that and the fail-open decision is unaffected.

    The fail-open COUNTER is maintained unconditionally — it measures DB
    degradation, which happened whether or not anyone was told. The COOLDOWN is
    not: ``_last_alerted_ts`` advances only after a confirmed delivery, so a
    rejected page does not consume the window and the next fail-open re-attempts
    immediately.
    """
    global _last_alerted_ts
    log.error(
        "suppression_db_error",
        combo_key=combo_key,
        err=err,
        err_id="SUPP_DB_FAIL",
    )
    now_ts = time.monotonic()
    _fallback_timestamps.append(now_ts)
    while (
        _fallback_timestamps and now_ts - _fallback_timestamps[0] > _FALLBACK_WINDOW_SEC
    ):
        _fallback_timestamps.popleft()

    threshold = settings.FEEDBACK_FALLBACK_ALERT_THRESHOLD
    cooldown = settings.FEEDBACK_FALLBACK_ALERT_COOLDOWN_SEC
    if len(_fallback_timestamps) >= threshold and now_ts - _last_alerted_ts >= cooldown:
        msg = (
            f"⚠ Suppression fail-open fired {len(_fallback_timestamps)}x "
            f"in last hour. DB may be degraded — combos are currently ungated."
        )
        digest = payload_digest(msg)
        await record_alert_event(
            db,
            event_type="alert_dispatched",
            combo_key=combo_key,
            alert_source="suppression",
            retry=0,
            payload_hash=digest,
        )
        try:
            # One-shot aiohttp session — fallbacks are rare (DB-degraded),
            # so the overhead of opening+closing a connection pool once per
            # alert is acceptable vs. threading a long-lived session through
            # every dispatcher.
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                # parse_mode=None — plain-text fail-open alert, no
                # formatting; passing explicit keeps §12b discipline even
                # when the message body looks safe today.
                # ``raise_on_failure=True`` is LOAD-BEARING (ruled #525 class).
                # The alerter defaults to False and merely LOGS a non-200 or a
                # network error, so without it this call returned normally on a
                # rejected page and the `alert_delivered` ledger row below
                # stamped anyway. A durable ledger asserting deliveries that
                # never happened is worse than no ledger — it converts an
                # unknown into a false certainty.
                #
                # Containment is unchanged: the raise is caught below, recorded
                # as `alert_failed`, and never propagates. A failed
                # system-health page must not break the suppression path, which
                # is already running degraded (this is the fail-open branch).
                await alerter.send_telegram_message(
                    msg,
                    session,
                    settings,
                    parse_mode=None,
                    source="suppression",
                    raise_on_failure=True,
                )
        except Exception as exc:
            log.exception("suppression_fallback_alert_dispatch_error")
            await record_alert_event(
                db,
                event_type="alert_failed",
                combo_key=combo_key,
                alert_source="suppression",
                delivery_result=f"error:{type(exc).__name__}",
                retry=0,
                payload_hash=digest,
            )
            return
        # Cooldown consumed only by a page that ACTUALLY LANDED. Stamping at
        # decision time meant a rejected page burned the whole window: the
        # operator was told nothing, and the next fail-open inside the cooldown
        # was suppressed on the strength of a delivery that never happened.
        # This path fires when the DB is already degraded, so silence there is
        # the most expensive kind.
        _last_alerted_ts = now_ts
        await record_alert_event(
            db,
            event_type="alert_delivered",
            combo_key=combo_key,
            alert_source="suppression",
            delivery_result="ok",
            retry=0,
            payload_hash=digest,
        )
