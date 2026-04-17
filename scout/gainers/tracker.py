"""Top Gainers tracking -- store snapshots and compare with pipeline signals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from scout.db import Database

logger = structlog.get_logger(__name__)


def _parse_dt(s: str) -> datetime:
    """Parse ISO datetime string, ensure timezone-aware (UTC default)."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def prune_old_snapshots(db: "Database", retention_days: int = 7) -> int:
    """Delete gainers_snapshots older than retention_days. Returns rows deleted."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    cursor = await db._conn.execute(
        "DELETE FROM gainers_snapshots WHERE snapshot_at < ?", (cutoff,)
    )
    await db._conn.commit()
    deleted = cursor.rowcount
    if deleted:
        logger.info("gainers_snapshots_pruned", deleted=deleted, retention_days=retention_days)
    return deleted


async def store_top_gainers(
    db: "Database",
    raw_coins: list[dict],
    min_change: float = 20.0,
    max_mcap: float = 500_000_000,
) -> int:
    """Store top gainers from /coins/markets response.

    Filters for tokens with >min_change% 24h gain and <max_mcap market cap.
    Returns the number of rows stored.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    now = datetime.now(timezone.utc).isoformat()
    count = 0

    # Filter and sort by price change desc
    gainers = []
    for coin in raw_coins:
        coin_id = coin.get("id")
        if not coin_id:
            continue
        change = coin.get("price_change_percentage_24h") or 0
        mcap = coin.get("market_cap") or 0
        if change >= min_change and 0 < mcap < max_mcap:
            gainers.append(coin)

    gainers.sort(
        key=lambda c: c.get("price_change_percentage_24h") or 0, reverse=True
    )

    # Take top 20
    for coin in gainers[:20]:
        await db._conn.execute(
            """INSERT INTO gainers_snapshots
               (coin_id, symbol, name, price_change_24h, market_cap,
                volume_24h, price_at_snapshot, snapshot_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                coin["id"],
                (coin.get("symbol") or "???").upper(),
                coin.get("name") or "Unknown",
                coin.get("price_change_percentage_24h") or 0,
                coin.get("market_cap"),
                coin.get("total_volume"),
                coin.get("current_price"),
                now,
            ),
        )
        count += 1

    if count:
        await db._conn.commit()
        logger.info("gainers_snapshots_stored", count=count)

    return count


async def compare_gainers_with_signals(db: "Database") -> list[dict]:
    """For each top gainer in last 24h, check if our system detected it earlier.

    Same pattern as trending compare_with_signals.
    Returns list of comparison dicts.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    # Get distinct gainers from last 24h
    cursor = await db._conn.execute(
        """SELECT coin_id, symbol, name,
                  MAX(price_change_24h) as price_change_24h,
                  MIN(snapshot_at) as first_gainer_at
           FROM gainers_snapshots
           WHERE snapshot_at >= datetime('now', '-24 hours')
           GROUP BY coin_id""",
    )
    gainer_rows = await cursor.fetchall()
    if not gainer_rows:
        logger.info("gainers_tracker.compare_no_data")
        return []

    comparisons: list[dict] = []

    for row in gainer_rows:
        coin_id = row[0]
        symbol = row[1]
        name = row[2]
        price_change_24h = row[3]
        first_gainer_at_str = row[4]
        first_gainer_at = _parse_dt(first_gainer_at_str)

        comp: dict = {
            "coin_id": coin_id,
            "symbol": symbol,
            "name": name,
            "price_change_24h": price_change_24h,
            "appeared_on_gainers_at": first_gainer_at.isoformat(),
            "detected_by_narrative": 0,
            "narrative_lead_minutes": None,
            "detected_by_pipeline": 0,
            "pipeline_lead_minutes": None,
            "detected_by_chains": 0,
            "chains_lead_minutes": None,
            "detected_by_spikes": 0,
            "spikes_lead_minutes": None,
            "is_gap": 1,
        }

        # Check predictions table (narrative agent)
        cursor = await db._conn.execute(
            """SELECT MIN(predicted_at) FROM predictions
               WHERE (coin_id = ? OR LOWER(symbol) = LOWER(?))
                 AND predicted_at < datetime(?, '+5 minutes')""",
            (coin_id, symbol, first_gainer_at_str),
        )
        pred_row = await cursor.fetchone()
        if pred_row and pred_row[0]:
            pred_at = _parse_dt(pred_row[0])
            lead = (first_gainer_at - pred_at).total_seconds() / 60.0
            if lead < 0:
                lead = 0  # detected after, but within tolerance window
            comp["detected_by_narrative"] = 1
            comp["narrative_lead_minutes"] = round(lead, 1)
            comp["is_gap"] = 0

        # Check candidates table (pipeline)
        cursor = await db._conn.execute(
            """SELECT MIN(first_seen_at) FROM candidates
               WHERE (contract_address = ? OR LOWER(ticker) = LOWER(?))
                 AND first_seen_at < datetime(?, '+5 minutes')""",
            (coin_id, symbol, first_gainer_at_str),
        )
        cand_row = await cursor.fetchone()
        if cand_row and cand_row[0]:
            cand_at = _parse_dt(cand_row[0])
            lead = (first_gainer_at - cand_at).total_seconds() / 60.0
            if lead < 0:
                lead = 0  # detected after, but within tolerance window
            comp["detected_by_pipeline"] = 1
            comp["pipeline_lead_minutes"] = round(lead, 1)
            comp["is_gap"] = 0

        # Check signal_events table (chain signals)
        # Only use LIKE for symbols >= 4 chars to avoid short-symbol false positives.
        if len(symbol) >= 4:
            cursor = await db._conn.execute(
                """SELECT MIN(created_at) FROM signal_events
                   WHERE (token_id = ? OR LOWER(token_id) = LOWER(?)
                          OR LOWER(token_id) LIKE LOWER(? || '%')
                          OR LOWER(?) LIKE LOWER(token_id || '%'))
                     AND created_at < datetime(?, '+5 minutes')""",
                (coin_id, symbol, symbol, coin_id, first_gainer_at_str),
            )
        else:
            cursor = await db._conn.execute(
                """SELECT MIN(created_at) FROM signal_events
                   WHERE (token_id = ? OR LOWER(token_id) = LOWER(?))
                     AND created_at < datetime(?, '+5 minutes')""",
                (coin_id, symbol, first_gainer_at_str),
            )
        sig_row = await cursor.fetchone()
        if sig_row and sig_row[0]:
            sig_at = _parse_dt(sig_row[0])
            lead = (first_gainer_at - sig_at).total_seconds() / 60.0
            if lead < 0:
                lead = 0  # detected after, but within tolerance window
            comp["detected_by_chains"] = 1
            comp["chains_lead_minutes"] = round(lead, 1)
            comp["is_gap"] = 0

        # Check volume_spikes table
        cursor = await db._conn.execute(
            """SELECT MIN(detected_at) FROM volume_spikes
               WHERE coin_id = ? AND detected_at < datetime(?, '+5 minutes')""",
            (coin_id, first_gainer_at_str),
        )
        spike_row = await cursor.fetchone()
        if spike_row and spike_row[0]:
            spike_at = _parse_dt(spike_row[0])
            lead = (first_gainer_at - spike_at).total_seconds() / 60.0
            if lead < 0:
                lead = 0  # detected after, but within tolerance window
            comp["detected_by_spikes"] = 1
            comp["spikes_lead_minutes"] = round(lead, 1)
            comp["is_gap"] = 0

        comparisons.append(comp)

    # Look up detected_price from price_cache and preserve existing peaks
    for comp in comparisons:
        cid = comp["coin_id"]
        old_cursor = await db._conn.execute(
            "SELECT detected_price, peak_price, peak_gain_pct FROM gainers_comparisons WHERE coin_id = ?",
            (cid,),
        )
        old_row = await old_cursor.fetchone()
        if old_row and old_row[0]:
            comp["detected_price"] = old_row[0]
            comp["peak_price"] = old_row[1]
            comp["peak_gain_pct"] = old_row[2]
        else:
            pc = await db._conn.execute(
                "SELECT current_price FROM price_cache WHERE coin_id = ?", (cid,)
            )
            price_row = await pc.fetchone()
            comp["detected_price"] = price_row[0] if price_row and price_row[0] else None
            comp["peak_price"] = None
            comp["peak_gain_pct"] = None

    # Store comparisons (delete old for same coin_id then insert)
    for comp in comparisons:
        await db._conn.execute(
            "DELETE FROM gainers_comparisons WHERE coin_id = ?",
            (comp["coin_id"],),
        )
        await db._conn.execute(
            """INSERT INTO gainers_comparisons
               (coin_id, symbol, name, price_change_24h,
                appeared_on_gainers_at,
                detected_by_narrative, narrative_lead_minutes,
                detected_by_pipeline, pipeline_lead_minutes,
                detected_by_chains, chains_lead_minutes,
                detected_by_spikes, spikes_lead_minutes,
                is_gap, detected_price, peak_price, peak_gain_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                comp["coin_id"],
                comp["symbol"],
                comp["name"],
                comp["price_change_24h"],
                comp["appeared_on_gainers_at"],
                comp["detected_by_narrative"],
                comp["narrative_lead_minutes"],
                comp["detected_by_pipeline"],
                comp["pipeline_lead_minutes"],
                comp["detected_by_chains"],
                comp["chains_lead_minutes"],
                comp["detected_by_spikes"],
                comp["spikes_lead_minutes"],
                comp["is_gap"],
                comp["detected_price"],
                comp["peak_price"],
                comp["peak_gain_pct"],
            ),
        )
    await db._conn.commit()

    caught = sum(1 for c in comparisons if not c["is_gap"])
    logger.info(
        "gainers_tracker.comparisons_stored",
        total=len(comparisons),
        caught=caught,
        gaps=len(comparisons) - caught,
    )
    return comparisons


async def get_recent_gainers(
    db: "Database", limit: int = 20
) -> list[dict]:
    """Get recent gainers snapshots for the dashboard."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute(
        """SELECT coin_id, symbol, name, price_change_24h,
                  market_cap, volume_24h, snapshot_at, created_at
           FROM gainers_snapshots
           ORDER BY snapshot_at DESC, price_change_24h DESC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_gainers_comparisons(
    db: "Database", limit: int = 50
) -> list[dict]:
    """Get gainers comparisons for the dashboard."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute(
        """SELECT coin_id, symbol, name, price_change_24h,
                  appeared_on_gainers_at,
                  detected_by_narrative, narrative_lead_minutes,
                  detected_by_pipeline, pipeline_lead_minutes,
                  detected_by_chains, chains_lead_minutes,
                  detected_by_spikes, spikes_lead_minutes,
                  is_gap, detected_price, peak_price, peak_gain_pct, created_at
           FROM gainers_comparisons
           ORDER BY appeared_on_gainers_at DESC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_gainers_stats(db: "Database") -> dict:
    """Compute aggregate hit rate for gainers tracking."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM gainers_comparisons"
    )
    total = (await cursor.fetchone())[0]

    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM gainers_comparisons WHERE is_gap = 0"
    )
    caught = (await cursor.fetchone())[0]

    missed = total - caught

    # Average lead time across all detection methods
    cursor = await db._conn.execute(
        """SELECT AVG(lead) FROM (
             SELECT narrative_lead_minutes as lead FROM gainers_comparisons
               WHERE detected_by_narrative = 1 AND narrative_lead_minutes IS NOT NULL
             UNION ALL
             SELECT pipeline_lead_minutes FROM gainers_comparisons
               WHERE detected_by_pipeline = 1 AND pipeline_lead_minutes IS NOT NULL
             UNION ALL
             SELECT chains_lead_minutes FROM gainers_comparisons
               WHERE detected_by_chains = 1 AND chains_lead_minutes IS NOT NULL
             UNION ALL
             SELECT spikes_lead_minutes FROM gainers_comparisons
               WHERE detected_by_spikes = 1 AND spikes_lead_minutes IS NOT NULL
           )"""
    )
    lead_row = await cursor.fetchone()
    avg_lead = round(lead_row[0], 1) if lead_row and lead_row[0] is not None else None

    hit_rate = round((caught / total * 100) if total > 0 else 0, 1)

    return {
        "total_tracked": total,
        "caught": caught,
        "missed": missed,
        "hit_rate_pct": hit_rate,
        "avg_lead_minutes": avg_lead,
    }


async def update_gainers_peaks(db: "Database") -> int:
    """Update peak prices for all gainers comparisons using current price_cache data.

    Uses a single JOIN query instead of N+1 per-row lookups.
    Only uses prices updated within the last hour to avoid stale peaks.
    Returns the number of rows updated.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    conn = db._conn
    cursor = await conn.execute(
        """SELECT gc.id, gc.coin_id, gc.detected_price, gc.peak_price,
                  pc.current_price, pc.updated_at
           FROM gainers_comparisons gc
           JOIN price_cache pc ON gc.coin_id = pc.coin_id
           WHERE gc.detected_price IS NOT NULL
             AND gc.detected_price > 0
             AND pc.current_price IS NOT NULL
             AND pc.updated_at >= datetime('now', '-1 hour')"""
    )
    rows = await cursor.fetchall()
    updated = 0

    for row in rows:
        current_price = row["current_price"]
        old_peak = row["peak_price"] or row["detected_price"] or 0

        if current_price > old_peak:
            peak_gain = ((current_price - row["detected_price"]) / row["detected_price"]) * 100
            await conn.execute(
                "UPDATE gainers_comparisons SET peak_price = ?, peak_gain_pct = ? WHERE id = ?",
                (current_price, peak_gain, row["id"]),
            )
            updated += 1

    if updated:
        await conn.commit()
        logger.info("gainers_tracker.peaks_updated", count=updated)

    return updated
