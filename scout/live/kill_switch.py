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
        kill_event_id: int,
        triggered_by: str | None = None,
        reason: str | None = None,
        killed_until: datetime | None = None,
        cleared_by: str | None = None,
        latched_hours: float | None = None,
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
    ) -> tuple[int, bool]:
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
        statements and undo its pending writes. Each attempt therefore checks
        its own result — a win is reported only once the pointer is read back
        naming the new event — and an attempt that was interfered with is
        retried, up to ``_TRIGGER_ATTEMPTS`` times.

        Returns ``(kill_event_id, i_am_winner)`` — the first element is the
        **active** event id (winner's id for both callers, so callers that
        only care about "which event is active" can ignore the second
        element). The second element is ``True`` only for the single caller
        whose UPDATE claimed the active slot; losers get ``False``. This
        distinction is what :func:`maybe_trigger_from_daily_loss` uses to
        report "did *this* call cause the kill".
        """
        assert self._db._conn is not None
        assert self._db._txn_lock is not None
        for attempt in range(1, _TRIGGER_ATTEMPTS + 1):
            last = attempt == _TRIGGER_ATTEMPTS
            async with self._db._txn_lock:
                # Judged under the lock: a trigger that queued behind another
                # holder must see the clock as it is when it gets in.
                now = datetime.now(timezone.utc)
                killed_until = now + duration
                outcome = await self._trigger_attempt_locked(
                    triggered_by=triggered_by,
                    reason=reason,
                    now=now,
                    killed_until=killed_until,
                    attempt=attempt,
                    last=last,
                )
            if outcome is None:
                continue  # interfered with; logged inside
            new_id, won, released = outcome
            if not won:
                return new_id, False
            # §12b: notify the operator OUTSIDE the txn lock — the HTTP send
            # must not hold the single DB writer. Winner-only.
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
                kill_event_id=new_id,
                triggered_by=triggered_by,
                reason=reason,
                killed_until=killed_until,
            )
            return new_id, True
        # Every attempt was interfered with. Report what is in force now,
        # and say loudly that this call did not engage a kill.
        state = await self._active_kill_state()
        current = state.kill_event_id if state is not None else None
        log.error(
            "live_kill_trigger_unconfirmed",
            triggered_by=triggered_by,
            reason=reason,
            attempts=_TRIGGER_ATTEMPTS,
            active_kill_event_id=current,
        )
        return current, False

    async def _trigger_attempt_locked(
        self,
        *,
        triggered_by: str,
        reason: str,
        now: datetime,
        killed_until: datetime,
        attempt: int,
        last: bool,
    ) -> tuple[int, bool, tuple[int, str, timedelta | None] | None] | None:
        """One insert-release-claim pass. Caller holds the txn lock.

        Returns ``(event_id, won, released)``, or ``None`` when a foreign
        rollback interfered and the attempt should be retried. Never leaves a
        write transaction open: every exit commits (never rolls back — a
        ROLLBACK on the shared connection would discard other writers' work).
        """
        conn = self._db._conn
        assert conn is not None
        new_id: int | None = None
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

            # Only after the INSERT has succeeded: if it raises (a CHECK on
            # triggered_by, say) nothing has been released.
            released = await self._release_dead_pointer_locked(now)

            # Conditional claim — only succeeds if no other trigger has won.
            cur = await conn.execute(
                "UPDATE live_control SET active_kill_event_id = ? "
                "WHERE id = 1 AND active_kill_event_id IS NULL",
                (new_id,),
            )
            claimed = cur.rowcount == 1

            if claimed:
                # Read back before claiming a win: a foreign rollback between
                # the INSERT and here would leave the pointer naming a row
                # that no longer exists, or no pointer at all.
                cur = await conn.execute(
                    "SELECT lc.active_kill_event_id, ke.id "
                    "FROM live_control AS lc "
                    "LEFT JOIN kill_events AS ke ON ke.id = lc.active_kill_event_id "
                    "WHERE lc.id = 1"
                )
                check = await cur.fetchone()
                if check is not None and check[0] == new_id and check[1] == new_id:
                    await conn.commit()
                    log.warning(
                        "live_kill_event_triggered",
                        kill_event_id=new_id,
                        triggered_by=triggered_by,
                        reason=reason,
                        killed_until=killed_until.isoformat(),
                    )
                    return new_id, True, released
                await conn.execute(
                    "UPDATE live_control SET active_kill_event_id = NULL "
                    "WHERE id = 1 AND active_kill_event_id = ?",
                    (new_id,),
                )
                await conn.execute("DELETE FROM kill_events WHERE id = ?", (new_id,))
                await conn.commit()
                return self._interfered(attempt, last, "claim_not_confirmed")

            # Lost the claim. Either a real kill is in force (a concurrent
            # trigger won), or a foreign rollback undid our release and the
            # pointer is dead again — in which case try once more.
            await conn.execute("DELETE FROM kill_events WHERE id = ?", (new_id,))
            if await self._pointer_is_dead_locked(now):
                await conn.commit()
                return self._interfered(attempt, last, "release_undone")
            cur = await conn.execute(
                "SELECT active_kill_event_id FROM live_control WHERE id = 1"
            )
            winner_id = (await cur.fetchone())[0]
            await conn.commit()
            log.info(
                "live_kill_event_trigger_lost_race",
                losing_speculative_id=new_id,
                winner_id=winner_id,
            )
            return winner_id, False, None
        except sqlite3.Error as exc:
            # e.g. the claim names a row a foreign rollback removed (FOREIGN
            # KEY). End our transaction without leaving an orphan row behind.
            if new_id is not None:
                try:
                    await conn.execute(
                        "DELETE FROM kill_events WHERE id = ? AND id NOT IN ("
                        "SELECT active_kill_event_id FROM live_control "
                        "WHERE active_kill_event_id IS NOT NULL)",
                        (new_id,),
                    )
                except sqlite3.Error:
                    pass
            await conn.commit()
            if last:
                raise
            log.warning(
                "live_kill_trigger_retry",
                attempt=attempt,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    @staticmethod
    def _interfered(attempt: int, last: bool, why: str) -> None:
        log.warning(
            "live_kill_trigger_retry",
            attempt=attempt,
            why=why,
            final=last,
        )
        return None

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

        await inc(db, "kill_trigger_errors")
        log.error(
            "live_kill_trigger_failed",
            exception=str(exc),
            daily_sum=daily_sum,
        )
        raise
    return i_won
