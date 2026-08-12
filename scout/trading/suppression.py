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


def _parole_window_open(parole_at: str | None) -> bool:
    """Is ``parole_at`` a parsable timestamp that has already passed?

    Used INSIDE the locked transaction, where an absent/unparsable/future
    value all mean the same thing: the parole window this caller validated on
    the lock-free fast path is not the window that is current now. Fails
    CLOSED — an unparsable value denies rather than admits.
    """
    if parole_at is None:
        return False
    try:
        dt = datetime.fromisoformat(parole_at)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= datetime.now(timezone.utc)


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
            await _record_fallback(combo_key, str(e), settings)
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
        await _record_fallback(combo_key, f"parole_at parse: {e}", settings)
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
            if not _parole_window_open(parole_at_now):
                await db._conn.execute("COMMIT")
                log.info(
                    "parole_generation_changed_before_reservation",
                    combo_key=combo_key,
                    detail="suppression state was re-latched between the "
                    "lock-free read and the locked reservation; no admission",
                )
                return (False, "suppressed", None)

            if remaining is None or remaining <= 0:
                await db._conn.execute("COMMIT")
                return (False, "parole_exhausted", None)
            await db._conn.execute(
                "UPDATE combo_performance SET parole_trades_remaining = ? "
                "WHERE combo_key = ? AND window = '30d'",
                (remaining - 1, combo_key),
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
                await _record_fallback(combo_key, f"parole_decrement: {e}", settings)
                return (True, "db_error_fallback_allow", None)
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
            before_changes = db._conn.total_changes
            await db._conn.execute(
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
            changed = db._conn.total_changes - before_changes
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


async def _record_fallback(combo_key: str, err: str, settings) -> None:
    """Log + maintain the fail-open counter; fire Telegram alert with cooldown."""
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
        _last_alerted_ts = now_ts
        msg = (
            f"⚠ Suppression fail-open fired {len(_fallback_timestamps)}x "
            f"in last hour. DB may be degraded — combos are currently ungated."
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
                await alerter.send_telegram_message(
                    msg, session, settings, parse_mode=None, source="suppression"
                )
        except Exception:
            log.exception("suppression_fallback_alert_dispatch_error")
