"""HealthProbe — per-venue REST + balance check, write venue_health row.

Cadence: 60s default. Writes auth_ok, rest_responsive (REST hit timing),
last_balance_fetch_ok (balance fetched cleanly), is_dormant=0 for active
venues. Routing layer reads the latest probe per venue.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import structlog

from scout.db import Database
from scout.live.services.base import VenueService

log = structlog.get_logger(__name__)


class HealthProbe(VenueService):
    name = "health_probe"
    cadence_seconds = 60.0

    async def run_once(
        self, *, adapter: Any, db: Database, venue: str
    ) -> None:
        if db._conn is None:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        rest_responsive = 1
        rest_latency_ms: int | None = None
        auth_ok = 1
        last_balance_fetch_ok = 1
        error_text: str | None = None
        balance_usd: float | None = None

        # Probe via fetch_account_balance — lowest-coupling REST call
        # that exercises auth path. Time the call; use NotImplementedError
        # vs other exceptions to distinguish unwired vs broken.
        t0 = time.monotonic()
        try:
            balance_usd = await asyncio.wait_for(
                adapter.fetch_account_balance(asset="USDT"), timeout=10.0
            )
            rest_latency_ms = int((time.monotonic() - t0) * 1000)
        except NotImplementedError as exc:
            # Adapter not yet wired (BL-055 shadow / M1.5 scaffold) —
            # mark not-rest-responsive but keep auth_ok=1 (we can't
            # tell from a stub whether auth is real).
            rest_responsive = 0
            last_balance_fetch_ok = 0
            error_text = f"NotImplementedError: {exc}"
        except asyncio.TimeoutError:
            rest_responsive = 0
            last_balance_fetch_ok = 0
            error_text = "fetch_account_balance: 10s timeout"
        except Exception as exc:
            rest_responsive = 0
            last_balance_fetch_ok = 0
            # Best-effort auth-vs-other distinction by error string;
            # M1 keeps it simple with everything = auth_ok=0 on
            # non-timeout exceptions.
            auth_ok = 0
            error_text = f"{type(exc).__name__}: {exc}"

        await db._conn.execute(
            """INSERT INTO venue_health
               (venue, probe_at, rest_responsive, rest_latency_ms,
                ws_connected, rate_limit_headroom_pct, auth_ok,
                last_balance_fetch_ok, last_quote_at, fills_30d_count,
                is_dormant, error_text)
               VALUES (?, ?, ?, ?, 0, NULL, ?, ?, ?, 0, 0, ?)""",
            (
                venue,
                now_iso,
                rest_responsive,
                rest_latency_ms,
                auth_ok,
                last_balance_fetch_ok,
                now_iso if balance_usd is not None else None,
                error_text,
            ),
        )
        await db._conn.commit()
        log.info(
            "health_probe_completed",
            venue=venue,
            auth_ok=auth_ok,
            rest_responsive=rest_responsive,
            latency_ms=rest_latency_ms,
            balance_usd=balance_usd,
        )
