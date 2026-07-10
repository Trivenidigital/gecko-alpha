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
        now = datetime.now(timezone.utc)
        killed_until = now + duration
        async with self._db._txn_lock:
            # Insert the row first — speculative. If we lose the race we
            # DELETE it below.
            await self._db._conn.execute(
                "INSERT INTO kill_events "
                "(triggered_by, reason, triggered_at, killed_until) "
                "VALUES (?, ?, ?, ?)",
                (triggered_by, reason, now.isoformat(), killed_until.isoformat()),
            )
            cur = await self._db._conn.execute("SELECT last_insert_rowid()")
            new_id = (await cur.fetchone())[0]

            # Conditional claim — only succeeds if no other trigger has won.
            cur = await self._db._conn.execute(
                "UPDATE live_control SET active_kill_event_id = ? "
                "WHERE id = 1 AND active_kill_event_id IS NULL",
                (new_id,),
            )
            claimed = cur.rowcount == 1

            if not claimed:
                # Lost the race — a concurrent trigger already claimed. Mark our
                # speculative row as superseded and return the winner's id. No
                # alert here: the winner emits it (avoids duplicate notifications).
                await self._db._conn.execute(
                    "DELETE FROM kill_events WHERE id = ?", (new_id,)
                )
                cur = await self._db._conn.execute(
                    "SELECT active_kill_event_id FROM live_control WHERE id = 1"
                )
                winner_id = (await cur.fetchone())[0]
                await self._db._conn.commit()
                log.info(
                    "live_kill_event_trigger_lost_race",
                    losing_speculative_id=new_id,
                    winner_id=winner_id,
                )
                return winner_id, False

            await self._db._conn.commit()
            log.warning(
                "live_kill_event_triggered",
                kill_event_id=new_id,
                triggered_by=triggered_by,
                reason=reason,
                killed_until=killed_until.isoformat(),
            )

        # §12b: notify the operator OUTSIDE the txn lock — the HTTP send must not
        # hold the single DB writer. Winner-only (the loser returned above).
        await self._emit_alert(
            event_type="triggered",
            kill_event_id=new_id,
            triggered_by=triggered_by,
            reason=reason,
            killed_until=killed_until,
        )
        return new_id, True

    async def clear(self, *, cleared_by: str) -> None:
        """Clear the active kill, if any.

        UPDATE ``live_control`` → NULL and stamp ``cleared_at`` + ``cleared_by``
        on the previously-active ``kill_events`` row. Both writes go through
        the txn lock so a concurrent ``trigger()`` cannot interleave.
        """
        assert self._db._conn is not None
        assert self._db._txn_lock is not None
        now = datetime.now(timezone.utc)
        async with self._db._txn_lock:
            cur = await self._db._conn.execute(
                "SELECT active_kill_event_id FROM live_control WHERE id = 1"
            )
            row = await cur.fetchone()
            active_id = row[0] if row is not None else None
            if active_id is None:
                # Nothing to clear — commit nothing, return silently.
                return
            await self._db._conn.execute(
                "UPDATE live_control SET active_kill_event_id = NULL " "WHERE id = 1"
            )
            await self._db._conn.execute(
                "UPDATE kill_events SET cleared_at = ?, cleared_by = ? " "WHERE id = ?",
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
        await self.clear(cleared_by="auto_expired")
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
