"""OBSERVE phase — CoinGecko category polling, acceleration detection, market regime."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
import structlog

from scout.db import Database
from scout.narrative.models import CategoryAcceleration, CategorySnapshot

logger = structlog.get_logger(__name__)

CATEGORIES_URL = "https://api.coingecko.com/api/v3/coins/categories"


async def fetch_categories(
    session: aiohttp.ClientSession,
    api_key: str = "",
    max_retries: int = 3,
) -> list[dict]:
    """GET CoinGecko /coins/categories with exponential backoff on 429."""
    headers: dict[str, str] = {}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key

    for attempt in range(max_retries):
        try:
            async with session.get(CATEGORIES_URL, headers=headers) as resp:
                if resp.status == 429:
                    wait = 2 ** (attempt + 1)
                    logger.warning("coingecko_429_retry", attempt=attempt, wait=wait)
                    await asyncio.sleep(wait)
                    continue
                if resp.status != 200:
                    logger.error("coingecko_categories_error", status=resp.status)
                    return []
                data = await resp.json()
                result = data if isinstance(data, list) else []
                await asyncio.sleep(1)  # rate-limit: space out CoinGecko calls
                return result
        except Exception:
            logger.exception("coingecko_categories_exception", attempt=attempt)
            return []

    logger.error("coingecko_categories_exhausted_retries", max_retries=max_retries)
    return []


def parse_category_response(
    data: list[dict],
    market_regime: str,
) -> list[CategorySnapshot]:
    """Parse CoinGecko category response into snapshots, skipping invalid entries."""
    snapshots: list[CategorySnapshot] = []
    now = datetime.now(timezone.utc)

    for entry in data:
        try:
            market_cap = entry.get("market_cap")
            market_cap_change_24h = entry.get("market_cap_change_24h")
            volume_24h = entry.get("volume_24h")

            if market_cap is None or market_cap_change_24h is None or volume_24h is None:
                continue

            snapshots.append(
                CategorySnapshot(
                    category_id=entry["id"],
                    name=entry["name"],
                    market_cap=float(market_cap),
                    market_cap_change_24h=float(market_cap_change_24h),
                    volume_24h=float(volume_24h),
                    coin_count=None,
                    market_regime=market_regime,
                    snapshot_at=now,
                )
            )
        except Exception:
            logger.exception("parse_category_entry_error", entry_id=entry.get("id"))
            continue

    return snapshots


def detect_market_regime(weighted_change_24h: float) -> str:
    """Classify market regime based on weighted 24h change."""
    if weighted_change_24h > 3.0:
        return "BULL"
    if weighted_change_24h < -3.0:
        return "BEAR"
    return "CRAB"


def compute_acceleration(
    current: list[CategorySnapshot],
    previous: list[CategorySnapshot],
    accel_threshold: float,
    vol_threshold: float,
) -> list[CategoryAcceleration]:
    """Compute acceleration between two snapshot sets, matched by category_id."""
    prev_map = {s.category_id: s for s in previous}
    results: list[CategoryAcceleration] = []

    for cur in current:
        prev = prev_map.get(cur.category_id)
        if prev is None:
            continue

        acceleration = cur.market_cap_change_24h - prev.market_cap_change_24h

        if prev.volume_24h == 0:
            volume_growth_pct = 0.0
        else:
            volume_growth_pct = ((cur.volume_24h - prev.volume_24h) / prev.volume_24h) * 100

        coin_count_change: int | None = None
        if cur.coin_count is not None and prev.coin_count is not None:
            coin_count_change = cur.coin_count - prev.coin_count

        is_heating = acceleration > accel_threshold and volume_growth_pct > vol_threshold

        results.append(
            CategoryAcceleration(
                category_id=cur.category_id,
                name=cur.name,
                current_velocity=cur.market_cap_change_24h,
                previous_velocity=prev.market_cap_change_24h,
                acceleration=acceleration,
                volume_24h=cur.volume_24h,
                volume_growth_pct=volume_growth_pct,
                coin_count_change=coin_count_change,
                is_heating=is_heating,
            )
        )

    return results


async def store_snapshot(db: Database, snapshots: list[CategorySnapshot]) -> None:
    """INSERT each snapshot into category_snapshots table."""
    if db._conn is None:
        raise RuntimeError("Database not initialized. Call initialize() first.")

    for snap in snapshots:
        await db._conn.execute(
            """INSERT INTO category_snapshots
               (category_id, name, market_cap, market_cap_change_24h,
                volume_24h, coin_count, market_regime, snapshot_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snap.category_id,
                snap.name,
                snap.market_cap,
                snap.market_cap_change_24h,
                snap.volume_24h,
                snap.coin_count,
                snap.market_regime,
                snap.snapshot_at.isoformat(),
            ),
        )
    await db._conn.commit()
    logger.info("stored_category_snapshots", count=len(snapshots))


async def load_snapshots_at(
    db: Database,
    target_time: datetime,
) -> list[CategorySnapshot]:
    """Load most-recent snapshot per category_id at or before target_time."""
    if db._conn is None:
        raise RuntimeError("Database not initialized. Call initialize() first.")

    cursor = await db._conn.execute(
        """SELECT category_id, name, market_cap, market_cap_change_24h,
                  volume_24h, coin_count, market_regime, snapshot_at
           FROM category_snapshots
           WHERE snapshot_at <= ?
           ORDER BY snapshot_at DESC
           LIMIT 500""",
        (target_time.isoformat(),),
    )
    rows = await cursor.fetchall()

    seen: set[str] = set()
    results: list[CategorySnapshot] = []
    for row in rows:
        cid = row[0]
        if cid in seen:
            continue
        seen.add(cid)
        results.append(
            CategorySnapshot(
                category_id=row[0],
                name=row[1],
                market_cap=row[2],
                market_cap_change_24h=row[3],
                volume_24h=row[4],
                coin_count=row[5],
                market_regime=row[6],
                snapshot_at=datetime.fromisoformat(row[7]),
            )
        )

    return results


async def prune_old_snapshots(db: Database, retention_days: int) -> int:
    """DELETE snapshots older than retention_days. Return rows deleted."""
    if db._conn is None:
        raise RuntimeError("Database not initialized. Call initialize() first.")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    cursor = await db._conn.execute(
        "DELETE FROM category_snapshots WHERE snapshot_at < ?",
        (cutoff,),
    )
    await db._conn.commit()
    logger.info("pruned_old_snapshots", deleted=cursor.rowcount, retention_days=retention_days)
    return cursor.rowcount
