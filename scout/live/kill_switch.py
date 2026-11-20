"""KillSwitch (spec §6). Atomic trigger/clear + G2 duration math.

The kill switch is the fail-safe that halts live trading. It is backed by two
tables:

* ``kill_events`` — append-only audit log of every trigger, with clear metadata
* ``live_control`` — single-row pointer (``id=1``) whose
  ``active_kill_event_id`` points at the currently-active ``kill_events`` row,
  or ``NULL`` when trading is allowed

Concurrency (spec §11.5): two ``trigger()`` calls can race when two independent
close paths each observe a daily-loss-cap breach. To collapse concurrent
triggers to a single active event, the atomic serialization point is the
conditional ``UPDATE live_control SET active_kill_event_id = ? WHERE id = 1
AND active_kill_event_id IS NULL``. Only one caller's UPDATE affects a row;
the loser deletes its speculative ``kill_events`` row and returns the winner's
id. aiosqlite's single-writer serializes the two UPDATEs so one always sees
NULL and the other does not.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog

from scout.config import Settings
from scout.db import Database
from scout.live.types import KillState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = structlog.get_logger(__name__)

# §12b (LIVE-01): auto-clearing a kill that expired more than this long ago
# means the kill was LATCHED — Gate 1 rejected every shadow open for the whole
# window and silently froze the soak. Such a clear gets an extra operator alert
# beyond the standard CLEARED notification. A normal ≤4h kill picked up on the
# next 60s tick expires only seconds-to-minutes past killed_until (below this),
# so steady-state auto-clears do not trip the latched alert.
_LATCHED_ALERT_THRESHOLD = timedelta(hours=1)
# trigger() retries an attempt that another writer's unlocked ROLLBACK
# interfered with, at most this many times in total.
_TRIGGER_ATTEMPTS = 3
# Set on an exception trigger() re-raises after counting kill_trigger_errors,
# so a caller that also counts failures does not count it twice.
_COUNTED_ATTR = "kill_trigger_errors_counted"

_last_attempt_at: datetime | None = None


def _unique_utc_now() -> datetime:
    """UTC now, strictly later than any value this function returned before
    in this process. trigger() stamps each attempt's row with it and later
    recognises its own row by (id, triggered_at); ids alone are not unique
    once a rollback has rewound AUTOINCREMENT, and a coarse clock could give
    two attempts the same time."""
    global _last_attempt_at
    now = datetime.now(timezone.utc)
    if _last_attempt_at is not None and now <= _last_attempt_at:
        now = _last_attempt_at + timedelta(microseconds=1)
    _last_attempt_at = now
    return now


def compute_kill_duration(trigger_time: datetime) -> timedelta:
    """Returns ``max(4h, time until next UTC midnight)``.

    Examples (spec §6.3, the "G2" math):

    * ``00:15 UTC`` → next midnight is 23h45m away → ``max(4h, 23h45m) = 23.75h``
    * ``12:00 UTC`` → 12h to midnight → ``max(4h, 12h) = 12h``
    * ``20:00 UTC`` → 4h to midnight → ``max(4h, 4h) = 4h``
    * ``23:55 UTC`` → 5min to midnight → ``max(4h, 5m) = 4h``

    The 4h floor prevents a last-minute trigger from clearing itself almost
    immediately; the next-midnight cap aligns the kill with the daily-cap
    rolling window so the next trading session starts clean.
    """
    next_midnight = datetime(
        trigger_time.year,
        trigger_time.month,
        trigger_time.day,
        tzinfo=timezone.utc,
    ) + timedelta(days=1)
    return max(timedelta(hours=4), next_midnight - trigger_time)


class KillSwitch:
    """Write-side + read-side for the kill switch.

    All methods assume :meth:`scout.db.Database.initialize` has been called so
    ``db._conn`` is a live connection.
    """

    def __init__(
        self,
        db: Database,
        *,
        alert_hook: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._db = db
        # §12b: optional plain-text alert sink for automated state changes.
        # Set ONLY at the automated construction site (main.py). The CLI builds
        # a hookless instance, so operator-initiated triggers/clears do not
        # self-notify (§12b exempts operator-initiated reversals). The hook MUST
        # send plain text (parse_mode=None): kill reasons/actors contain
        # underscores (e.g. binance_auth_revoked_mid_session, live_engine) that
        # Telegram MarkdownV1 would silently mangle (Class-3).
        self._alert_hook = alert_hook

    async def _emit_alert(
        self,
        *,
        event_type: str,
        kill_event_id: int | None,
        triggered_by: str | None = None,
        reason: str | None = None,
        killed_until: datetime | None = None,
        cleared_by: str | None = None,
        latched_hours: float | None = None,
        note: str | None = None,
    ) -> None:
        """Emit the §12b operator alert for an automated kill-switch transition.

        Wrapped in the dispatched/delivered/failed log triplet so every fire is
        traceable in journalctl regardless of delivery outcome. NEVER raises: the
        DB state change has already committed before this runs, so an alert
        failure must not corrupt the kill-switch contract — it is logged loudly
        instead (the §12b "log on failure" rule).
        """
        if self._alert_hook is None:
            return
        if event_type == "triggered":
            until = killed_until.isoformat() if killed_until is not None else "n/a"
            message = (
                "LIVE KILL-SWITCH TRIGGERED\n"
                f"event #{kill_event_id} by {triggered_by}\n"
                f"reason: {reason}\n"
                f"live trading halted until {until} UTC"
            )
        elif event_type == "not_engaged":
            message = (
                "LIVE KILL-SWITCH NOT ENGAGED\n"
                f"requested by {triggered_by}\n"
                f"reason: {reason}\n"
                "no kill could be confirmed in force — treat live trading "
                "as NOT halted and engage it by hand "
                "(python -m scout.live.cli_kill --on ...)"
            )
            if note:
                message += f"\n{note}"
        elif event_type == "latched_auto_clear":
            # LIVE-01: the kill outlived killed_until and froze the shadow soak.
            hours = latched_hours if latched_hours is not None else 0.0
            message = (
                f"shadow kill #{kill_event_id} auto-cleared; "
                f"was latched {hours:.0f}h past expiry — soak was frozen"
            )
        else:  # "cleared"
            message = (
                "LIVE KILL-SWITCH CLEARED\n"
                f"event #{kill_event_id} cleared by {cleared_by}\n"
                "live trading re-enabled"
            )
        log.info(
            "kill_switch_alert_dispatched",
            kill_event_id=kill_event_id,
            event_type=event_type,
        )
        try:
            await self._alert_hook(message)
            log.info(
                "kill_switch_alert_delivered",
                kill_event_id=kill_event_id,
                event_type=event_type,
            )
        except Exception as exc:
            log.exception(
                "kill_switch_alert_failed",
                kill_event_id=kill_event_id,
                event_type=event_type,
                err=str(exc),
                err_type=type(exc).__name__,
            )

    async def _active_kill_state(self) -> KillState | None:
        """Return the active (``cleared_at IS NULL``) kill **regardless of
        expiry**, or ``None`` if nothing is active.

        Unlike :meth:`is_active`, this does NOT treat a past-``killed_until``
        kill as inactive — :meth:`auto_clear_if_expired` needs the raw active
        row so it can still stamp ``cleared_at`` and alert on a latched kill.
        """
        assert self._db._conn is not None
        cur = await self._db._conn.execute("""
            SELECT ke.id, ke.killed_until, ke.reason, ke.triggered_by
              FROM live_control AS lc
              JOIN kill_events  AS ke ON ke.id = lc.active_kill_event_id
             WHERE lc.id = 1
               AND ke.cleared_at IS NULL
            """)
        row = await cur.fetchone()
        if row is None:
            return None
        return KillState(
            kill_event_id=row[0],
            killed_until=datetime.fromisoformat(row[1]),
            reason=row[2],
            triggered_by=row[3],
        )

    async def is_active(self) -> KillState | None:
        """Return the current :class:`KillState` or ``None``.

        Active = ``live_control.active_kill_event_id`` is non-NULL AND the
        referenced ``kill_events.cleared_at IS NULL`` AND ``killed_until`` is
        still in the future. The ``cleared_at`` guard is belt-and-braces
        against a partial/hand-edited row.

        Belt-and-braces (LIVE-01): a kill whose ``killed_until`` is already in
        the past reads as INACTIVE even when ``cleared_at`` is still NULL, so a
        missed :meth:`auto_clear_if_expired` tick cannot latch the kill forever
        and freeze the shadow soak (kill_events #1 latched ~33 days in prod).
        When this guard fires it logs ``kill_switch_expired_uncleared`` at
        WARNING (§12b visibility) — steady state it should never appear, because
        the per-tick / boot auto-clear stamps ``cleared_at`` first.
        """
        state = await self._active_kill_state()
        if state is None:
            return None
        now = datetime.now(timezone.utc)
        if state.killed_until < now:
            log.warning(
                "kill_switch_expired_uncleared",
                kill_event_id=state.kill_event_id,
                killed_until=state.killed_until.isoformat(),
                triggered_by=state.triggered_by,
            )
            return None
        return state

    async def _release_dead_pointer_locked(
        self, now: datetime
    ) -> tuple[int, str, timedelta | None] | None:
        """Null ``live_control.active_kill_event_id`` when it does not stand
        for a kill in force. Caller holds the txn lock and commits.

        Dead means: the referenced event row is missing, is already cleared,
        or is uncleared with ``killed_until`` strictly before ``now`` — the
        same expiry rule :meth:`is_active` applies. An expired event is
        stamped ``cleared_by='auto_expired'`` so it is not left dangling; an
        already-cleared event keeps its stamp. A ``killed_until`` that cannot
        be read is NOT treated as dead: the pointer is left alone, and that
        is logged at ERROR because it can stop a fresh kill from engaging.

        Returns ``(released_event_id, why, latched_for)`` or ``None`` if
        nothing was released. ``latched_for`` is how long an expired kill had
        sat past its expiry. The caller sends the operator alerts after it
        leaves the lock.
        """
        assert self._db._conn is not None
        cur = await self._db._conn.execute(
            "SELECT lc.active_kill_event_id, ke.id, ke.cleared_at, "
            "ke.killed_until, ke.triggered_by "
            "FROM live_control AS lc "
            "LEFT JOIN kill_events AS ke ON ke.id = lc.active_kill_event_id "
            "WHERE lc.id = 1"
        )
        row = await cur.fetchone()
        if row is None or row[0] is None:
            return None
        pointer, event_id, cleared_at, killed_until_raw, event_triggered_by = row
        latched_for: timedelta | None = None
        if event_id is None:
            why = "event_missing"
        elif cleared_at is not None:
            why = "event_already_cleared"
        else:
            try:
                killed_until = datetime.fromisoformat(killed_until_raw)
                expired = killed_until < now
            except (TypeError, ValueError):
                log.error(
                    "live_kill_dead_pointer_unreadable",
                    kill_event_id=pointer,
                    killed_until=killed_until_raw,
                    note="cannot tell whether this kill has expired, so it is "
                    "left in force; a new kill cannot engage until it is "
                    "cleared by hand",
                )
                return None
            if not expired:
                return None
            why = "event_expired"
            latched_for = now - killed_until
            await self._db._conn.execute(
                "UPDATE kill_events SET cleared_at = ?, cleared_by = 'auto_expired' "
                "WHERE id = ? AND cleared_at IS NULL",
                (now.isoformat(), pointer),
            )
        cur = await self._db._conn.execute(
            "UPDATE live_control SET active_kill_event_id = NULL "
            "WHERE id = 1 AND active_kill_event_id = ?",
            (pointer,),
        )
        if cur.rowcount != 1:
            return None
        log.warning(
            "live_kill_dead_pointer_released",
            released_kill_event_id=pointer,
            why=why,
            triggered_by=event_triggered_by,
            killed_until=killed_until_raw,
            latched_hours=(
                None
                if latched_for is None
                else round(latched_for.total_seconds() / 3600.0, 2)
            ),
        )
        return pointer, why, latched_for

    async def _pointer_is_dead_locked(self, now: datetime) -> bool:
        """Whether the current pointer stands for no kill in force (same rule
        as :meth:`_release_dead_pointer_locked`, read-only)."""
        assert self._db._conn is not None
        cur = await self._db._conn.execute(
            "SELECT lc.active_kill_event_id, ke.id, ke.cleared_at, ke.killed_until "
            "FROM live_control AS lc "
            "LEFT JOIN kill_events AS ke ON ke.id = lc.active_kill_event_id "
            "WHERE lc.id = 1"
        )
        row = await cur.fetchone()
        if row is None or row[0] is None:
            return True
        _pointer, event_id, cleared_at, killed_until_raw = row
        if event_id is None or cleared_at is not None:
            return True
        try:
            return datetime.fromisoformat(killed_until_raw) < now
        except (TypeError, ValueError):
            return False

    async def trigger(
        self,
        *,
        triggered_by: str,
        reason: str,
        duration: timedelta,
    ) -> tuple[int | None, bool]:
        """Insert kill_events row and atomically claim live_control.

        Concurrent triggers collapse: only the one that flips
        ``live_control.active_kill_event_id`` from NULL wins. The loser's
        speculative ``kill_events`` row is removed.

        A pointer that no longer stands for a kill in force — its event is
        expired, already cleared, or gone — is released first, inside the same
        transaction (:meth:`_release_dead_pointer_locked`). Otherwise the
        claim below would lose to a kill that :meth:`is_active` already
        reports as inactive, and a fresh breach could not halt trading.

        Some writers on the shared connection commit or roll back WITHOUT
        ``db._txn_lock``, so a foreign ROLLBACK can land between this method's
        statements and undo its pending writes. Each attempt therefore
        COMMITS first and then reads the committed pointer back: a win is
        reported only for a kill that is durably in force. An attempt that
        was undone is retried; at most ``_TRIGGER_ATTEMPTS`` attempts are
        made in total. If none engages a kill, the operator is alerted
        ("NOT ENGAGED") and ``(None, False)`` is returned.

        Returns ``(kill_event_id, i_am_winner)`` — the first element is the
        **active** event id (winner's id for both callers, so callers that
        only care about "which event is active" can ignore the second
        element), or ``None`` when no kill is in force. The second element is
        ``True`` only for the single caller whose claim engaged the kill;
        losers get ``False``. This distinction is what
        :func:`maybe_trigger_from_daily_loss` uses to report "did *this* call
        cause the kill".
        """
        assert self._db._conn is not None
        assert self._db._txn_lock is not None
        released: tuple[int, str, timedelta | None] | None = None
        killed_until = datetime.now(timezone.utc) + duration
        last_error: sqlite3.Error | None = None
        # Every speculative row this call inserted. A claim left uncommitted
        # by a failed commit can become durable during a LATER attempt; the
        # kill it engages is still this call's kill.
        # id -> triggered_at: the id alone is not proof, because a foreign
        # rollback also rolls back AUTOINCREMENT's counter and an undone id
        # can be handed to someone else's row.
        our_ids: dict[int, str] = {}
        # id -> the killed_until that row was written with, so a claim that
        # lands in a later attempt is announced with its own expiry.
        our_until: dict[int, datetime] = {}
        for attempt in range(1, _TRIGGER_ATTEMPTS + 1):
            async with self._db._txn_lock:
                # Judged under the lock: a trigger that queued behind another
                # holder must see the clock as it is when it gets in. Unique
                # per attempt: it is half of how this call knows its own row.
                now = _unique_utc_now()
                killed_until = now + duration
                ids_before = set(our_ids)
                verdict, event_id, attempt_released, error = (
                    await self._trigger_attempt_locked(
                        triggered_by=triggered_by,
                        reason=reason,
                        now=now,
                        killed_until=killed_until,
                        our_ids=our_ids,
                    )
                )
                for new in set(our_ids) - ids_before:
                    our_until[new] = killed_until
                # A release that a later attempt cannot see again (it was
                # committed, or half-committed by an error path) still
                # happened: keep the first one for the alert and the stamp.
                if released is None and attempt_released is not None:
                    released = attempt_released
                if verdict == "won" and released is not None:
                    await self._ensure_released_event_stamped_locked(released, now)
            last_error = error or last_error
            if verdict == "won":
                await self._announce_win(
                    event_id=event_id,
                    released=released,
                    triggered_by=triggered_by,
                    reason=reason,
                    killed_until=our_until.get(event_id, killed_until),
                    attempts=attempt,
                )
                return event_id, True
            if verdict == "lost":
                log.info(
                    "live_kill_event_trigger_lost_race",
                    winner_id=event_id,
                    attempts=attempt,
                )
                if error is not None:
                    # Trading is halted by the other kill, but this call
                    # failed for its own reason (a bad triggered_by, say);
                    # do not let that hide behind the lost race.
                    log.error(
                        "live_kill_trigger_error_while_lost",
                        winner_id=event_id,
                        triggered_by=triggered_by,
                        error=str(error),
                        error_type=type(error).__name__,
                    )
                return event_id, False
            log.warning(
                "live_kill_trigger_retry",
                attempt=attempt,
                final=attempt == _TRIGGER_ATTEMPTS,
                error=None if error is None else str(error),
                error_type=None if error is None else type(error).__name__,
            )

        # No attempt engaged a kill. If every commit failed, our claim may be
        # sitting in an open transaction that the NEXT commit on this shared
        # connection — anyone's, including the metric write below — would
        # make durable. Settle it ourselves first: one more commit, and if it
        # goes through, judge from committed state like any attempt.
        conn = self._db._conn
        pending: list[int] = []
        async with self._db._txn_lock:
            if conn.in_transaction:
                await self._commit_or_log_locked(
                    "trigger_final_settle", kill_event_ids=sorted(our_ids)
                )
            if not conn.in_transaction:
                try:
                    settled = await self._owned_pointer_locked(our_ids)
                except sqlite3.Error as exc:
                    settled, last_error = None, last_error or exc
                if settled is not None:
                    if released is not None:
                        await self._ensure_released_event_stamped_locked(
                            released, _unique_utc_now()
                        )
            else:
                settled = None
                # Still open: list the rows of ours it holds (a read inside
                # the open transaction sees them), so the operator knows
                # which claim may yet take effect.
                pending = await self._our_rows_present_locked(our_ids)
        unconfirmable = bool(conn.in_transaction)
        state = None
        if settled is None and not unconfirmable:
            try:
                state = await self.is_active()
            except ValueError:  # an unreadable killed_until on the pointer
                state = None
            except sqlite3.Error as exc:
                # The switch cannot be read at all: nothing can be confirmed.
                state, last_error = None, last_error or exc
            if state is not None and await self._is_ours(
                state, our_ids, triggered_by, reason
            ):
                # The judging read failed, but the kill in force is this
                # call's own: it engaged, so report it as engaged.
                settled = state.kill_event_id
        if settled is not None:
            await self._announce_win(
                event_id=settled,
                released=released,
                triggered_by=triggered_by,
                reason=reason,
                killed_until=our_until.get(settled, killed_until),
                attempts=_TRIGGER_ATTEMPTS,
            )
            return settled, True
        active_id = state.kill_event_id if state is not None else None
        if state is not None and last_error is not None:
            # Trading is halted by another kill, but this call failed for its
            # own reason; do not let that hide behind the other kill.
            log.error(
                "live_kill_trigger_error_while_lost",
                winner_id=active_id,
                triggered_by=triggered_by,
                error=str(last_error),
                error_type=type(last_error).__name__,
            )
        log.error(
            "live_kill_trigger_unconfirmed",
            triggered_by=triggered_by,
            reason=reason,
            attempts=_TRIGGER_ATTEMPTS,
            active_kill_event_id=active_id,
            transaction_left_open=unconfirmable,
            pending_kill_event_ids=pending,
            last_error=None if last_error is None else str(last_error),
        )
        will_raise = last_error is not None and state is None
        counted = False
        if state is None:
            # Counted here, once, for every caller: the engine's venue-fatal
            # kills and the CLI do not count failures themselves. The raised
            # error is marked so maybe_trigger_from_daily_loss does not count
            # it again. Skipped while a transaction is still open: the
            # metric's commit would make the pending claim durable behind
            # the alert's back.
            if unconfirmable:
                log.error(
                    "live_kill_trigger_metric_skipped",
                    why="transaction_left_open",
                    pending_kill_event_ids=pending,
                )
            else:
                try:
                    from scout.live.metrics import inc

                    await inc(self._db, "kill_trigger_errors")
                    counted = True
                except Exception:  # noqa: BLE001 - the alert below must still go
                    log.exception("live_kill_trigger_metric_failed")
            note = None
            if pending:
                note = (
                    "claim #"
                    + ", #".join(str(i) for i in pending)
                    + " is pending in an open transaction and may still take "
                    "effect on the next successful commit — check "
                    "`python -m scout.live.cli_kill --status`; if --on fails "
                    "with 'database is locked', restart the pipeline first"
                )
            await self._emit_alert(
                event_type="not_engaged",
                kill_event_id=None,
                triggered_by=triggered_by,
                reason=(
                    reason
                    if last_error is None
                    else f"{reason} (last error: {last_error})"
                ),
                note=note,
            )
        if will_raise:
            if counted:
                # Only when it really was counted: a caller that counts its
                # own failures must not skip one nobody recorded.
                try:
                    setattr(last_error, _COUNTED_ATTR, True)
                except AttributeError:
                    pass
            raise last_error
        return active_id, False

    async def _trigger_attempt_locked(
        self,
        *,
        triggered_by: str,
        reason: str,
        now: datetime,
        killed_until: datetime,
        our_ids: dict[int, str],
    ) -> tuple[
        str,
        int | None,
        tuple[int, str, timedelta | None] | None,
        sqlite3.Error | None,
    ]:
        """One insert-release-claim pass, then a check of what was COMMITTED.
        Caller holds the txn lock.

        Returns ``(verdict, event_id, released, error)`` where verdict is
        ``"won"`` (event_id is ours and durably in force), ``"lost"``
        (event_id is a different kill in force) or ``"retry"`` (the pointer is
        dead again: our work was undone — or a commit failed and nothing can
        be confirmed). Ends its transaction with COMMIT, never ROLLBACK — a
        rollback on the shared connection would discard other writers' work.
        If every commit fails the transaction stays open, and the verdict is
        then "retry", never "won": an open transaction's reads include its
        own uncommitted claim.
        """
        conn = self._db._conn
        assert conn is not None
        new_id: int | None = None
        released: tuple[int, str, timedelta | None] | None = None
        error: sqlite3.Error | None = None
        try:
            # Insert the row first — speculative. If we lose the race we
            # DELETE it below.
            await conn.execute(
                "INSERT INTO kill_events "
                "(triggered_by, reason, triggered_at, killed_until) "
                "VALUES (?, ?, ?, ?)",
                (triggered_by, reason, now.isoformat(), killed_until.isoformat()),
            )
            cur = await conn.execute("SELECT last_insert_rowid()")
            new_id = (await cur.fetchone())[0]
            our_ids[new_id] = now.isoformat()
            # Only after the INSERT has succeeded: if it raises (a CHECK on
            # triggered_by, say) nothing has been released.
            released = await self._release_dead_pointer_locked(now)
            # Conditional claim — only succeeds if no other trigger has won.
            cur = await conn.execute(
                "UPDATE live_control SET active_kill_event_id = ? "
                "WHERE id = 1 AND active_kill_event_id IS NULL",
                (new_id,),
            )
            if cur.rowcount != 1:
                await conn.execute("DELETE FROM kill_events WHERE id = ?", (new_id,))
            await conn.commit()
        except sqlite3.Error as exc:
            # e.g. the claim names a row a foreign rollback removed (FOREIGN
            # KEY), or the commit hit a lock. End the transaction; what it
            # left behind is judged below from the committed state.
            error = exc
            await self._commit_or_log_locked(
                "trigger_attempt", kill_event_id=new_id, triggered_by=triggered_by
            )

        if conn.in_transaction:
            # Both commits failed: what a read shows now is our own
            # uncommitted work, not what is durably in force. Judge nothing;
            # the next attempt's commit will settle it.
            return "retry", None, released, error

        # What is durably in force now? Committed state cannot be undone by a
        # later rollback, so this is the only safe basis for "won".
        try:
            cur = await conn.execute(
                "SELECT lc.active_kill_event_id, ke.id, ke.triggered_at "
                "FROM live_control AS lc "
                "LEFT JOIN kill_events AS ke ON ke.id = lc.active_kill_event_id "
                "WHERE lc.id = 1"
            )
            row = await cur.fetchone()
        except sqlite3.Error as exc:
            # Nothing can be judged; the next attempt (or the final settle)
            # reads again. If reads keep failing, the final step decides from
            # is_active(), or reports NOT ENGAGED if the switch cannot be
            # read at all.
            return "retry", None, released, error or exc
        pointer = row[0] if row is not None else None
        if pointer is not None and row[1] == pointer and our_ids.get(pointer) == row[2]:
            return "won", pointer, released, error

        # Our speculative rows, if any survived, must not linger unreferenced.
        # Matched on triggered_at as well, so a reused id is left alone.
        for orphan_id, orphan_at in sorted(our_ids.items()):
            if orphan_id == pointer:
                continue
            try:
                await conn.execute(
                    "DELETE FROM kill_events WHERE id = ? AND triggered_at = ? "
                    "AND id NOT IN ("
                    "SELECT active_kill_event_id FROM live_control "
                    "WHERE active_kill_event_id IS NOT NULL)",
                    (orphan_id, orphan_at),
                )
            except sqlite3.Error as exc:
                log.error(
                    "live_kill_trigger_orphan_row_not_removed",
                    kill_event_id=orphan_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        await self._commit_or_log_locked(
            "trigger_orphan_cleanup", kill_event_ids=sorted(our_ids)
        )
        try:
            dead = await self._pointer_is_dead_locked(now)
        except sqlite3.Error as exc:
            return "retry", None, released, error or exc
        if dead:
            return "retry", None, released, error
        return "lost", pointer, released, error

    async def _owned_pointer_locked(self, our_ids: dict[int, str]) -> int | None:
        """The active event id if it is one of this call's rows (matched on id
        AND triggered_at), else None. Caller holds the txn lock and has no
        transaction open, so this reads committed state."""
        conn = self._db._conn
        assert conn is not None
        cur = await conn.execute(
            "SELECT lc.active_kill_event_id, ke.id, ke.triggered_at "
            "FROM live_control AS lc "
            "LEFT JOIN kill_events AS ke ON ke.id = lc.active_kill_event_id "
            "WHERE lc.id = 1"
        )
        row = await cur.fetchone()
        if row is None or row[0] is None or row[1] != row[0]:
            return None
        return row[0] if our_ids.get(row[0]) == row[2] else None

    async def _our_rows_present_locked(self, our_ids: dict[int, str]) -> list[int]:
        """Which of this call's rows exist as this connection sees them —
        including uncommitted work in an open transaction."""
        conn = self._db._conn
        assert conn is not None
        present: list[int] = []
        for event_id, at in sorted(our_ids.items()):
            try:
                cur = await conn.execute(
                    "SELECT 1 FROM kill_events WHERE id = ? AND triggered_at = ?",
                    (event_id, at),
                )
                if await cur.fetchone() is not None:
                    present.append(event_id)
            except sqlite3.Error as exc:
                log.error(
                    "live_kill_pending_row_unreadable",
                    kill_event_id=event_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                present.append(event_id)  # unknown: say it may be pending
        return present

    async def _is_ours(
        self,
        state: KillState,
        our_ids: dict[int, str],
        triggered_by: str,
        reason: str,
    ) -> bool:
        """Whether the kill in force is one of this call's rows: same id and
        same triggered_at; if triggered_at cannot be read, same id, reason and
        triggered_by (a reused id would carry another writer's text)."""
        if state.kill_event_id not in our_ids:
            return False
        conn = self._db._conn
        assert conn is not None
        try:
            cur = await conn.execute(
                "SELECT triggered_at FROM kill_events WHERE id = ?",
                (state.kill_event_id,),
            )
            row = await cur.fetchone()
            return row is not None and row[0] == our_ids[state.kill_event_id]
        except sqlite3.Error:
            return state.reason == reason and state.triggered_by == triggered_by

    async def _announce_win(
        self,
        *,
        event_id: int,
        released: tuple[int, str, timedelta | None] | None,
        triggered_by: str,
        reason: str,
        killed_until: datetime,
        attempts: int,
    ) -> None:
        log.warning(
            "live_kill_event_triggered",
            kill_event_id=event_id,
            triggered_by=triggered_by,
            reason=reason,
            killed_until=killed_until.isoformat(),
            attempts=attempts,
        )
        # §12b: notify the operator OUTSIDE the txn lock — the HTTP send must
        # not hold the single DB writer. Winner-only.
        if (
            released is not None
            and released[2] is not None
            and released[2] > _LATCHED_ALERT_THRESHOLD
        ):
            await self._emit_alert(
                event_type="latched_auto_clear",
                kill_event_id=released[0],
                latched_hours=released[2].total_seconds() / 3600.0,
            )
        await self._emit_alert(
            event_type="triggered",
            kill_event_id=event_id,
            triggered_by=triggered_by,
            reason=reason,
            killed_until=killed_until,
        )

    async def _ensure_released_event_stamped_locked(
        self, released: tuple[int, str, timedelta | None], now: datetime
    ) -> None:
        """An expired kill released by an attempt that was later undone may
        have lost its ``auto_expired`` stamp while its pointer release stuck.
        Re-stamp it if so. Caller holds the txn lock."""
        released_id, why, _latched = released
        if why != "event_expired":
            return
        conn = self._db._conn
        assert conn is not None
        try:
            await conn.execute(
                "UPDATE kill_events SET cleared_at = ?, cleared_by = 'auto_expired' "
                "WHERE id = ? AND cleared_at IS NULL",
                (now.isoformat(), released_id),
            )
        except sqlite3.Error as exc:
            log.error(
                "live_kill_released_event_not_stamped",
                kill_event_id=released_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
        await self._commit_or_log_locked(
            "released_event_stamp", kill_event_id=released_id
        )

    async def _commit_or_log_locked(self, what: str, **context: object) -> None:
        """COMMIT, retrying once; log at ERROR if both fail. Caller holds the
        txn lock. A failed COMMIT leaves the write transaction open, holding
        the database's write lock against other processes, so it is worth one
        more try. Callers that must not act on uncommitted state check
        ``conn.in_transaction`` themselves afterwards."""
        conn = self._db._conn
        assert conn is not None
        last: sqlite3.Error | None = None
        for _ in range(2):
            try:
                await conn.commit()
                return
            except sqlite3.Error as exc:
                last = exc
        log.error(
            "live_kill_commit_failed",
            what=what,
            error=str(last),
            error_type=type(last).__name__,
            transaction_left_open=bool(conn.in_transaction),
            **context,
        )

    async def clear(self, *, cleared_by: str) -> int | None:
        """Clear the active kill, if any. Returns the cleared event id, or
        ``None`` when nothing was cleared.

        UPDATE ``live_control`` → NULL and stamp ``cleared_at`` + ``cleared_by``
        on the previously-active ``kill_events`` row. Both writes go through
        the txn lock so a concurrent ``trigger()`` cannot interleave.

        This is the operator's lever: it clears whichever kill is active when
        it reads the pointer. An event that already carries a clear stamp
        keeps it. ``None`` means either nothing was active, or the pointer
        moved between the read and the write (another process engaged a kill);
        callers that report to an operator should check :meth:`is_active`.
        """
        return await self._clear(cleared_by=cleared_by, only_event_id=None)

    async def _clear(self, *, cleared_by: str, only_event_id: int | None) -> int | None:
        """Clear the active kill and return its event id, or ``None`` if
        nothing was cleared.

        ``only_event_id`` makes the clear conditional: it applies only while
        the pointer still names that event. A caller that decided to clear
        from an earlier, unlocked read (:meth:`auto_clear_if_expired`) passes
        the event it judged, so it cannot clear a different kill that
        :meth:`trigger` engaged in between.
        """
        assert self._db._conn is not None
        assert self._db._txn_lock is not None
        async with self._db._txn_lock:
            now = datetime.now(timezone.utc)
            cur = await self._db._conn.execute(
                "SELECT active_kill_event_id FROM live_control WHERE id = 1"
            )
            row = await cur.fetchone()
            active_id = row[0] if row is not None else None
            if active_id is None:
                # Nothing to clear — commit nothing, return silently.
                return None
            if only_event_id is not None and active_id != only_event_id:
                log.info(
                    "live_kill_clear_skipped_superseded",
                    judged_kill_event_id=only_event_id,
                    active_kill_event_id=active_id,
                    cleared_by=cleared_by,
                )
                return None
            cur = await self._db._conn.execute(
                "UPDATE live_control SET active_kill_event_id = NULL "
                "WHERE id = 1 AND active_kill_event_id = ?",
                (active_id,),
            )
            if cur.rowcount != 1:
                # The pointer changed between the read above and this write:
                # another process (the kill CLI holds its own connection)
                # engaged a different kill. Nothing of ours is pending, so
                # there is nothing to undo — and nothing was cleared.
                # The UPDATE matched no row but still opened a write
                # transaction, which holds the database lock against every
                # other process. End it. COMMIT, not ROLLBACK: nothing of ours
                # is pending, and a rollback on this shared connection would
                # discard other writers' uncommitted work.
                await self._db._conn.commit()
                log.warning(
                    "live_kill_clear_skipped_pointer_moved",
                    read_kill_event_id=active_id,
                    cleared_by=cleared_by,
                )
                return None
            await self._db._conn.execute(
                "UPDATE kill_events SET cleared_at = ?, cleared_by = ? "
                "WHERE id = ? AND cleared_at IS NULL",
                (now.isoformat(), cleared_by, active_id),
            )
            await self._db._conn.commit()
            log.info(
                "live_kill_event_cleared",
                kill_event_id=active_id,
                cleared_by=cleared_by,
            )

        # §12b: notify the operator of an automated clear (auto-resume of live
        # trading) outside the txn lock. Hookless instances (CLI manual clear) do
        # not self-notify — operator-initiated reversals are §12b-exempt.
        await self._emit_alert(
            event_type="cleared",
            kill_event_id=active_id,
            cleared_by=cleared_by,
        )
        return active_id

    async def auto_clear_if_expired(self) -> bool:
        """If the active kill has expired, clear it with ``cleared_by='auto_expired'``.

        Returns ``True`` if a clear was performed, ``False`` otherwise. Safe to
        call on every scheduler tick and at boot. Uses :meth:`_active_kill_state`
        (not :meth:`is_active`) so it still stamps ``cleared_at`` on a kill that
        :meth:`is_active`'s belt-and-braces guard already treats as inactive.

        §12b (LIVE-01): if the kill had been latched more than
        ``_LATCHED_ALERT_THRESHOLD`` past expiry, the shadow soak was frozen the
        whole time — emit an extra operator alert naming the freeze duration.
        The generic CLEARED alert (from :meth:`clear`) does not convey a silent
        freeze, so the latched alert is additive, not a replacement.
        """
        state = await self._active_kill_state()
        if state is None:
            return False
        now = datetime.now(timezone.utc)
        if state.killed_until >= now:
            return False
        latched = now - state.killed_until
        # Conditional on the event judged above: the read was unlocked, and a
        # trigger() in between may have replaced that expired kill with a
        # fresh one, which must not be cleared here.
        cleared_id = await self._clear(
            cleared_by="auto_expired", only_event_id=state.kill_event_id
        )
        if cleared_id is None:
            return False
        if latched > _LATCHED_ALERT_THRESHOLD:
            latched_hours = latched.total_seconds() / 3600.0
            log.warning(
                "kill_switch_latched_auto_cleared",
                kill_event_id=state.kill_event_id,
                latched_hours=round(latched_hours, 1),
            )
            await self._emit_alert(
                event_type="latched_auto_clear",
                kill_event_id=state.kill_event_id,
                latched_hours=latched_hours,
            )
        return True


async def maybe_trigger_from_daily_loss(
    db: Database, ks: KillSwitch, settings: Settings
) -> bool:
    """Compute today-UTC closed-trade ``SUM(realized_pnl_usd)`` and trigger the
    kill switch when it breaches ``-LIVE_DAILY_LOSS_CAP_USD`` (spec §6.2).

    Concurrency (spec §11.5): two concurrent close paths can each observe a
    breach and both call :meth:`KillSwitch.trigger`. The durable serialization
    lives in ``trigger()``'s conditional
    ``UPDATE live_control SET active_kill_event_id = ? WHERE … IS NULL`` — only
    one caller claims. To return ``True`` **only for the winner**, this helper
    compares ``kill_events`` row count before and after ``trigger()``. The
    loser's speculative row is DELETEd inside ``trigger()``, so the loser sees
    an unchanged count and returns ``False``.

    Parameters
    ----------
    db:
        Open :class:`scout.db.Database` instance.
    ks:
        The :class:`KillSwitch` bound to ``db``.
    settings:
        Loaded :class:`scout.config.Settings` — uses
        ``LIVE_DAILY_LOSS_CAP_USD``.

    Returns
    -------
    bool
        ``True`` if **this** call's ``trigger()`` claimed the active slot,
        ``False`` if under-cap, already-killed, or lost a concurrent race.

    Raises
    ------
    Exception
        Any exception from ``trigger()`` is re-raised after incrementing the
        ``kill_trigger_errors`` metric and logging
        ``live_kill_trigger_failed`` at ERROR.
    """
    assert db._conn is not None
    cur = await db._conn.execute(
        "SELECT COALESCE(SUM(CAST(realized_pnl_usd AS REAL)), 0) "
        "FROM shadow_trades "
        "WHERE status LIKE 'closed_%' "
        "  AND date(closed_at) = date('now')"
    )
    daily_sum = (await cur.fetchone())[0]
    # LIVE-04 (audit S1-3): the shadow sum is blind to real live losses — in
    # live mode the engine skips the shadow write and closed fills land in
    # live_trades. Union today-UTC closed live PnL into the cap when
    # LIVE_TRADING_ENABLED=True so LIVE_DAILY_LOSS_CAP_USD can actually fire on a
    # real loss. Same date(closed_at)=date('now') predicate as the shadow query
    # (SQLite parses the T-separated ISO8601 the ledger stores; no INF-04
    # normalization regression — identical form both sides). Gated on the flag:
    # live_trades is empty in shadow/paper mode, and the flag makes the intent
    # explicit + the negative test precise.
    if getattr(settings, "LIVE_TRADING_ENABLED", False):
        cur = await db._conn.execute(
            "SELECT COALESCE(SUM(CAST(realized_pnl_usd AS REAL)), 0) "
            "FROM live_trades "
            "WHERE status LIKE 'closed_%' "
            "  AND date(closed_at) = date('now')"
        )
        daily_sum += (await cur.fetchone())[0]
    if daily_sum > -float(settings.LIVE_DAILY_LOSS_CAP_USD):
        return False
    # Cheap pre-check — not a guarantee under contention, the durable guard
    # is the UPDATE-WHERE-NULL inside trigger().
    if await ks.is_active() is not None:
        return False

    try:
        _id, i_won = await ks.trigger(
            triggered_by="daily_loss_cap",
            reason=(
                f"daily_sum={daily_sum:.2f} " f"cap=-{settings.LIVE_DAILY_LOSS_CAP_USD}"
            ),
            duration=compute_kill_duration(datetime.now(timezone.utc)),
        )
    except Exception as exc:
        from scout.live.metrics import inc

        if not getattr(exc, _COUNTED_ATTR, False):
            try:
                await inc(db, "kill_trigger_errors")
            except Exception:  # noqa: BLE001 - the error log below must still go
                log.exception("live_kill_trigger_metric_failed")
        log.error(
            "live_kill_trigger_failed",
            exception=str(exc),
            daily_sum=daily_sum,
        )
        raise
    return i_won
