"""RateLimitAccountantStub — conservative 50% headroom placeholder.

Per design v2.1: M1 ships a stub that unconditionally writes
`headroom_pct=50.0` so the routing layer's rate-limit awareness has
a source of truth. M2 wires a real accountant that tracks per-venue
request counts and computes actual headroom.

Conservative 50% means: routing avoids piling more requests onto a
venue when the stub says "you're at half capacity" — even if reality
is 100% headroom. This is the fail-safe direction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from scout.db import Database
from scout.live.services.base import VenueService

log = structlog.get_logger(__name__)


class RateLimitAccountantStub(VenueService):
    name = "rate_limit_stub"
    cadence_seconds = 30.0

    # M1 conservative default; do NOT raise this without a real accountant.
    HEADROOM_PCT = 50.0

    async def run_once(self, *, adapter: Any, db: Database, venue: str) -> None:
        if db._conn is None:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        await db._conn.execute(
            """INSERT INTO venue_rate_state
               (venue, last_updated_at, requests_per_min_cap,
                requests_seen_60s, headroom_pct)
               VALUES (?, ?, 1200, 0, ?)
               ON CONFLICT (venue) DO UPDATE SET
                 last_updated_at = excluded.last_updated_at,
                 headroom_pct = excluded.headroom_pct""",
            (venue, now_iso, self.HEADROOM_PCT),
        )
        await db._conn.commit()
        log.debug(
            "rate_limit_stub_updated",
            venue=venue,
            headroom_pct=self.HEADROOM_PCT,
        )
