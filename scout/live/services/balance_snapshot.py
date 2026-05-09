"""BalanceSnapshot — periodically write wallet_snapshots row per venue.

Cadence: 300s (5 min) default. Asks adapter for relevant assets'
balances; writes append-only rows so historical capital trajectory is
queryable. Used by routing layer to set RouteCandidate.available_capital_
usd (M1: currently None; wallet integration in M1.5).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from scout.db import Database
from scout.live.services.base import VenueService

log = structlog.get_logger(__name__)


class BalanceSnapshot(VenueService):
    name = "balance_snapshot"
    cadence_seconds = 300.0

    # M1 captures USDT only; M2 may iterate over a per-venue asset list
    # (BTC, ETH, etc.) for total-portfolio accounting.
    assets: tuple[str, ...] = ("USDT",)

    async def run_once(self, *, adapter: Any, db: Database, venue: str) -> None:
        if db._conn is None:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        for asset in self.assets:
            try:
                balance = await adapter.fetch_account_balance(asset=asset)
                # USDT balance is its own USD value at par.
                balance_usd = balance if asset.upper() in {"USDT", "USDC"} else None
                await db._conn.execute(
                    """INSERT INTO wallet_snapshots
                       (venue, asset, balance, balance_usd, snapshot_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (venue, asset, float(balance), balance_usd, now_iso),
                )
            except NotImplementedError:
                log.info(
                    "balance_snapshot_adapter_not_wired",
                    venue=venue,
                    asset=asset,
                )
                return  # Don't keep iterating if the adapter isn't wired
            except Exception:
                log.exception("balance_snapshot_failed", venue=venue, asset=asset)
        await db._conn.commit()
