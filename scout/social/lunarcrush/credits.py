"""Persistent credit ledger for LunarCrush daily budget tracking.

Survives restart via ``social_credit_ledger`` (design spec §6/§7). Any
accessor method that could be called after a UTC midnight rollover calls
``maybe_rollover()`` first so ``credits_used`` reflects only today's
consumption.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Callable, Optional

import structlog

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.db import Database

logger = structlog.get_logger(__name__)


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class CreditLedger:
    """Tracks credits consumed today, with UTC-midnight rollover.

    ``clock`` is injected for deterministic midnight-rollover tests.
    """

    def __init__(
        self,
        settings: "Settings",
        *,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self.credits_used: int = 0
        self._utc_date: date = clock().date()
        self._dirty = False

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def maybe_rollover(self) -> bool:
        """If the clock has crossed midnight UTC, zero the counter. Returns True if rolled over."""
        today = self._clock().date()
        if today != self._utc_date:
            logger.info(
                "social_credit_midnight_rollover",
                prev_date=str(self._utc_date),
                prev_used=self.credits_used,
                new_date=str(today),
            )
            self._utc_date = today
            self.credits_used = 0
            self._dirty = True
            return True
        return False

    @property
    def utc_date(self) -> date:
        return self._utc_date

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def mark_clean(self) -> None:
        self._dirty = False

    # ------------------------------------------------------------------
    # Consumption / policy
    # ------------------------------------------------------------------

    def consume(self, credits: int) -> None:
        self.maybe_rollover()
        if credits <= 0:
            return
        self.credits_used += credits
        self._dirty = True

    def fraction_used(self) -> float:
        self.maybe_rollover()
        budget = max(int(self._settings.LUNARCRUSH_DAILY_CREDIT_BUDGET), 1)
        return self.credits_used / budget

    def is_exhausted(self) -> bool:
        return self.fraction_used() >= float(self._settings.LUNARCRUSH_CREDIT_HARD_PCT)

    def is_soft_budget_hit(self) -> bool:
        return self.fraction_used() >= float(self._settings.LUNARCRUSH_CREDIT_SOFT_PCT)

    def current_poll_interval(self) -> int:
        """Return the configured poll interval, downshifted when soft-pressed."""
        if self.is_soft_budget_hit():
            return int(self._settings.LUNARCRUSH_POLL_INTERVAL_SOFT)
        return int(self._settings.LUNARCRUSH_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def hydrate(self, db: "Database") -> None:
        """Load today's row (if any) from ``social_credit_ledger``."""
        if db._conn is None:
            return
        today_str = self._clock().strftime("%Y-%m-%d")
        cursor = await db._conn.execute(
            "SELECT credits_used FROM social_credit_ledger WHERE utc_date = ?",
            (today_str,),
        )
        row = await cursor.fetchone()
        if row is not None:
            self.credits_used = int(row[0])
            self._utc_date = self._clock().date()
            self._dirty = False


async def flush_credit_ledger(db: "Database", ledger: CreditLedger) -> None:
    """Persist today's credit-used count to ``social_credit_ledger``."""
    if db._conn is None:
        return
    if not ledger.is_dirty:
        return
    today_str = ledger.utc_date.strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT OR REPLACE INTO social_credit_ledger
           (utc_date, credits_used, last_updated)
           VALUES (?, ?, ?)""",
        (today_str, ledger.credits_used, now_iso),
    )
    await db._conn.commit()
    ledger.mark_clean()
