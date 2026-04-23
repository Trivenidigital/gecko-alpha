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

import structlog

from scout.db import Database
from scout.live.types import KillState

log = structlog.get_logger(__name__)


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

    def __init__(self, db: Database) -> None:
        self._db = db

    async def is_active(self) -> KillState | None:
        """Return the current :class:`KillState` or ``None``.

        Active = ``live_control.active_kill_event_id`` is non-NULL AND the
        referenced ``kill_events.cleared_at IS NULL``. The NULL-on-cleared_at
        guard is belt-and-braces: :meth:`clear` always nulls both, but we
        re-check here so a partial/hand-edited row does not look active.
        """
        assert self._db._conn is not None
        cur = await self._db._conn.execute(
            """
            SELECT ke.id, ke.killed_until, ke.reason, ke.triggered_by
              FROM live_control AS lc
              JOIN kill_events  AS ke ON ke.id = lc.active_kill_event_id
             WHERE lc.id = 1
               AND ke.cleared_at IS NULL
            """
        )
        row = await cur.fetchone()
        if row is None:
            return None
        killed_until = datetime.fromisoformat(row[1])
        return KillState(
            kill_event_id=row[0],
            killed_until=killed_until,
            reason=row[2],
            triggered_by=row[3],
        )

    async def trigger(
        self,
        *,
        triggered_by: str,
        reason: str,
        duration: timedelta,
    ) -> int:
        """Insert kill_events row and atomically claim live_control.

        Concurrent triggers collapse: only the one that flips
        ``live_control.active_kill_event_id`` from NULL wins. The loser's
        speculative ``kill_events`` row is removed and the winner's id is
        returned to both callers.
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

            if claimed:
                await self._db._conn.commit()
                log.warning(
                    "kill_switch.triggered",
                    kill_event_id=new_id,
                    triggered_by=triggered_by,
                    reason=reason,
                    killed_until=killed_until.isoformat(),
                )
                return new_id

            # Lost the race — a concurrent trigger already claimed. Mark our
            # speculative row as superseded and return the winner's id.
            await self._db._conn.execute(
                "DELETE FROM kill_events WHERE id = ?", (new_id,)
            )
            cur = await self._db._conn.execute(
                "SELECT active_kill_event_id FROM live_control WHERE id = 1"
            )
            winner_id = (await cur.fetchone())[0]
            await self._db._conn.commit()
            log.info(
                "kill_switch.trigger_lost_race",
                losing_speculative_id=new_id,
                winner_id=winner_id,
            )
            return winner_id

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
                "UPDATE live_control SET active_kill_event_id = NULL "
                "WHERE id = 1"
            )
            await self._db._conn.execute(
                "UPDATE kill_events SET cleared_at = ?, cleared_by = ? "
                "WHERE id = ?",
                (now.isoformat(), cleared_by, active_id),
            )
            await self._db._conn.commit()
            log.info(
                "kill_switch.cleared",
                kill_event_id=active_id,
                cleared_by=cleared_by,
            )

    async def auto_clear_if_expired(self) -> bool:
        """If the active kill has expired, clear it with ``cleared_by='auto_expired'``.

        Returns ``True`` if a clear was performed, ``False`` otherwise. Safe
        to call on every scheduler tick.
        """
        state = await self.is_active()
        if state is None:
            return False
        now = datetime.now(timezone.utc)
        if state.killed_until >= now:
            return False
        await self.clear(cleared_by="auto_expired")
        return True
