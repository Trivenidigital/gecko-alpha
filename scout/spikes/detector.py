"""Volume history tracking + spike detection from CoinGecko /coins/markets data."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

from scout.spikes.models import VolumeSpike

if TYPE_CHECKING:
    from scout.db import Database

logger = structlog.get_logger(__name__)


async def record_volume(db: "Database", raw_coins: list[dict]) -> int:
    """Store volume snapshot from CoinGecko /coins/markets response.

    Returns the number of rows inserted. Also prunes history older than 7 days.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for coin in raw_coins:
        coin_id = coin.get("id")
        if not coin_id:
            continue
        volume = coin.get("total_volume") or 0
        if volume <= 0:
            continue
        await db._conn.execute(
            """INSERT INTO volume_history_cg
               (coin_id, symbol, name, volume_24h, market_cap, price, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                coin_id,
                (coin.get("symbol") or "???").upper(),
                coin.get("name") or "Unknown",
                float(volume),
                coin.get("market_cap"),
                coin.get("current_price"),
                now,
            ),
        )
        count += 1

    if count:
        await db._conn.commit()

    # Prune records older than 7 days
    await db._conn.execute(
        "DELETE FROM volume_history_cg WHERE datetime(recorded_at) < datetime('now', '-7 days')"
    )
    await db._conn.commit()

    logger.info("volume_history_recorded", count=count)
    return count


async def detect_spikes(
    db: "Database",
    min_spike_ratio: float = 5.0,
    max_mcap: float = 500_000_000,
) -> list[VolumeSpike]:
    """Find tokens where current volume > min_spike_ratio * 7-day average.

    Only considers tokens with market_cap between 0 and max_mcap.
    Inserts detected spikes into the volume_spikes table (deduplicating by
    coin_id + detected_at date).

    Returns list of newly detected spikes.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # Find spikes: latest volume vs 7-day average (excluding the latest entry)
    cursor = await db._conn.execute(
        """
        SELECT
            latest.coin_id,
            latest.symbol,
            latest.name,
            latest.volume_24h AS current_vol,
            AVG(hist.volume_24h) AS avg_vol,
            latest.market_cap,
            latest.price
        FROM volume_history_cg latest
        JOIN volume_history_cg hist
            ON hist.coin_id = latest.coin_id
            AND datetime(hist.recorded_at) >= datetime('now', '-7 days')
            AND hist.id != latest.id
        WHERE latest.recorded_at = (
            SELECT MAX(recorded_at)
            FROM volume_history_cg
            WHERE coin_id = latest.coin_id
        )
        GROUP BY latest.coin_id
        HAVING COUNT(hist.id) >= 3
            AND avg_vol > 0
            AND current_vol / avg_vol > ?
            AND COALESCE(latest.market_cap, 0) < ?
            AND COALESCE(latest.market_cap, 0) > 0
        ORDER BY current_vol / avg_vol DESC
        """,
        (min_spike_ratio, max_mcap),
    )
    rows = await cursor.fetchall()

    spikes: list[VolumeSpike] = []
    today = now.strftime("%Y-%m-%d")  # UTC date -- avoids local timezone drift

    for row in rows:
        coin_id = row[0]
        symbol = row[1]
        name = row[2]
        current_vol = float(row[3])
        avg_vol = float(row[4])
        market_cap = row[5]
        price = row[6]
        ratio = current_vol / avg_vol if avg_vol > 0 else 0

        # Dedup: skip if already recorded today for this coin
        dup_cursor = await db._conn.execute(
            """SELECT COUNT(*) FROM volume_spikes
               WHERE coin_id = ? AND date(detected_at) = ?""",
            (coin_id, today),
        )
        dup_row = await dup_cursor.fetchone()
        if dup_row and dup_row[0] > 0:
            continue

        # Look up price_change_24h from price_cache if available
        price_change_24h = None
        pc_cursor = await db._conn.execute(
            "SELECT price_change_24h FROM price_cache WHERE coin_id = ?",
            (coin_id,),
        )
        pc_row = await pc_cursor.fetchone()
        if pc_row:
            price_change_24h = pc_row[0]

        # Insert into volume_spikes
        await db._conn.execute(
            """INSERT INTO volume_spikes
               (coin_id, symbol, name, current_volume, avg_volume_7d,
                spike_ratio, market_cap, price, price_change_24h, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                coin_id,
                symbol,
                name,
                current_vol,
                avg_vol,
                round(ratio, 2),
                market_cap,
                price,
                price_change_24h,
                now_iso,
            ),
        )

        spike = VolumeSpike(
            coin_id=coin_id,
            symbol=symbol,
            name=name,
            current_volume=current_vol,
            avg_volume_7d=avg_vol,
            spike_ratio=round(ratio, 2),
            market_cap=market_cap,
            price=price,
            price_change_24h=price_change_24h,
            detected_at=now,
        )
        spikes.append(spike)

    if spikes:
        await db._conn.commit()
        logger.info("volume_spikes_detected", count=len(spikes))

    return spikes


async def detect_7d_momentum(
    db: "Database",
    raw_coins: list[dict],
    min_7d_change: float = 100.0,
    max_mcap: float = 500_000_000,
    min_volume_24h: float = 100_000,
) -> list[dict]:
    """Find tokens with extreme 7-day returns from already-fetched data.

    Pandora-type catches: +438% 7d that slip under the daily radar.
    No extra API calls -- filters the raw /coins/markets data we already have.
    Requires minimum $100K 24h volume to filter out illiquid junk.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    today_utc = now_dt.strftime("%Y-%m-%d")  # UTC date -- avoids local timezone drift
    results: list[dict] = []

    for coin in raw_coins:
        cid = coin.get("id")
        if not cid:
            continue
        change_7d = coin.get("price_change_percentage_7d_in_currency") or 0
        mcap = coin.get("market_cap") or 0
        volume = coin.get("total_volume") or 0

        if change_7d < min_7d_change or mcap <= 0 or mcap > max_mcap:
            continue
        if volume < min_volume_24h:
            continue

        # Dedup: skip if already detected today for this coin (UTC)
        cursor = await db._conn.execute(
            "SELECT id FROM momentum_7d WHERE coin_id = ? AND date(detected_at) = ?",
            (cid, today_utc),
        )
        if await cursor.fetchone():
            continue

        row_data = {
            "coin_id": cid,
            "symbol": (coin.get("symbol") or "").upper(),
            "name": coin.get("name") or "",
            "price_change_7d": change_7d,
            "price_change_24h": coin.get("price_change_percentage_24h") or 0,
            "market_cap": mcap,
            "current_price": coin.get("current_price"),
            "volume_24h": coin.get("total_volume") or 0,
        }

        # Persist to momentum_7d table
        await db._conn.execute(
            """INSERT INTO momentum_7d
               (coin_id, symbol, name, price_change_7d, price_change_24h,
                market_cap, current_price, volume_24h, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_data["coin_id"],
                row_data["symbol"],
                row_data["name"],
                row_data["price_change_7d"],
                row_data["price_change_24h"],
                row_data["market_cap"],
                row_data["current_price"],
                row_data["volume_24h"],
                now,
            ),
        )

        results.append(row_data)

    if results:
        await db._conn.commit()
        logger.info("momentum_7d_detected", count=len(results))

    return results


async def get_recent_momentum_7d(db: "Database", limit: int = 20) -> list[dict]:
    """Get recent 7d momentum detections for the dashboard."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute(
        """SELECT coin_id, symbol, name, price_change_7d, price_change_24h,
                  market_cap, current_price, volume_24h,
                  detected_at, created_at
           FROM momentum_7d
           ORDER BY detected_at DESC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_momentum_7d_stats(db: "Database") -> dict:
    """Momentum 7d stats: counts for today and this week."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM momentum_7d WHERE date(detected_at) = date('now')"
    )
    today_count = (await cursor.fetchone())[0]

    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM momentum_7d WHERE datetime(detected_at) >= datetime('now', '-7 days')"
    )
    week_count = (await cursor.fetchone())[0]

    cursor = await db._conn.execute("""SELECT AVG(price_change_7d) FROM momentum_7d
           WHERE datetime(detected_at) >= datetime('now', '-7 days')""")
    row = await cursor.fetchone()
    avg_change = round(row[0], 1) if row and row[0] else 0.0

    return {
        "detections_today": today_count,
        "detections_this_week": week_count,
        "avg_7d_change": avg_change,
    }


async def get_recent_spikes(db: "Database", limit: int = 20) -> list[dict]:
    """Get recent volume spikes for the dashboard."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute(
        """SELECT coin_id, symbol, name, current_volume, avg_volume_7d,
                  spike_ratio, market_cap, price, price_change_24h,
                  detected_at, created_at
           FROM volume_spikes
           ORDER BY detected_at DESC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_spike_stats(db: "Database") -> dict:
    """Spike detection stats: counts for today and this week."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM volume_spikes WHERE date(detected_at) = date('now')"
    )
    today_count = (await cursor.fetchone())[0]

    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM volume_spikes WHERE datetime(detected_at) >= datetime('now', '-7 days')"
    )
    week_count = (await cursor.fetchone())[0]

    cursor = await db._conn.execute("""SELECT AVG(spike_ratio) FROM volume_spikes
           WHERE datetime(detected_at) >= datetime('now', '-7 days')""")
    row = await cursor.fetchone()
    avg_ratio = round(row[0], 2) if row and row[0] else 0.0

    return {
        "spikes_today": today_count,
        "spikes_this_week": week_count,
        "avg_spike_ratio": avg_ratio,
    }
