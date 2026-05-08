"""DormancyJob — daily flag of zero-fill venues.

Per design v2.1: a venue with `fills_30d_count=0` is dormant — routing
layer skips it. The job updates `is_dormant` based on actual live_trades
counts in the past 30 days. Cadence: 24 hours (run once per day).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from scout.db import Database
from scout.live.services.base import VenueService

log = structlog.get_logger(__name__)


class DormancyJob(VenueService):
    name = "dormancy"
    cadence_seconds = 86400.0  # 24 hours

    DORMANCY_LOOKBACK_DAYS = 30

    async def run_once(
        self, *, adapter: Any, db: Database, venue: str
    ) -> None:
        if db._conn is None:
            return
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=self.DORMANCY_LOOKBACK_DAYS)
        ).isoformat()
        cur = await db._conn.execute(
            """SELECT COUNT(*) FROM live_trades
               WHERE venue = ? AND status LIKE 'closed%'
                 AND created_at >= ?""",
            (venue, cutoff),
        )
        fills_30d = (await cur.fetchone())[0]
        is_dormant = 1 if fills_30d == 0 else 0
        now_iso = datetime.now(timezone.utc).isoformat()
        # Update the latest venue_health row for this venue. Insert new
        # row if no probe exists yet (HealthProbe service hasn't run).
        cur = await db._conn.execute(
            "SELECT 1 FROM venue_health WHERE venue = ? LIMIT 1",
            (venue,),
        )
        has_probe = (await cur.fetchone()) is not None
        if has_probe:
            # Update most-recent row's is_dormant + fills_30d_count
            await db._conn.execute(
                """UPDATE venue_health
                   SET is_dormant = ?, fills_30d_count = ?
                   WHERE venue = ?
                     AND probe_at = (
                       SELECT MAX(probe_at) FROM venue_health WHERE venue = ?
                     )""",
                (is_dormant, fills_30d, venue, venue),
            )
        else:
            await db._conn.execute(
                """INSERT INTO venue_health
                   (venue, probe_at, rest_responsive, ws_connected,
                    auth_ok, last_balance_fetch_ok, is_dormant,
                    fills_30d_count)
                   VALUES (?, ?, 0, 0, 0, 0, ?, ?)""",
                (venue, now_iso, is_dormant, fills_30d),
            )
        await db._conn.commit()
        log.info(
            "dormancy_job_updated",
            venue=venue,
            fills_30d=fills_30d,
            is_dormant=is_dormant,
        )
