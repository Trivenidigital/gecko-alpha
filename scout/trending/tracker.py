"""Trending Snapshot Tracker -- fetch, store, compare, and report."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp
import structlog

from scout.ingestion.coingecko import CG_BASE, _get_with_backoff
from scout.trending.models import TrendingComparison, TrendingSnapshot, TrendingStats

if TYPE_CHECKING:
    from scout.db import Database
    from scout.models import CandidateToken

logger = structlog.get_logger(__name__)


def _parse_dt(s: str) -> datetime:
    """Parse ISO datetime string, ensure timezone-aware (UTC default)."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def fetch_and_store_trending(
    session: aiohttp.ClientSession,
    db: "Database",
    api_key: str = "",
) -> list[TrendingSnapshot]:
    """Fetch /search/trending and store each coin as a snapshot row.

    Returns the list of snapshots stored (empty on failure).
    """
    params: dict[str, str] = {}
    if api_key:
        params["x_cg_demo_api_key"] = api_key

    data = await _get_with_backoff(session, f"{CG_BASE}/search/trending", params or None)
    if not data or not isinstance(data, dict):
        logger.warning("trending_tracker.fetch_empty")
        return []

    coins = data.get("coins", [])
    now = datetime.now(timezone.utc)
    snapshots: list[TrendingSnapshot] = []

    for rank, entry in enumerate(coins[:15]):
        item = entry.get("item", {})
        coin_id = item.get("id")
        if not coin_id:
            continue

        snap = TrendingSnapshot(
            coin_id=coin_id,
            symbol=item.get("symbol", "???"),
            name=item.get("name", "Unknown"),
            market_cap_rank=item.get("market_cap_rank"),
            trending_score=float(rank + 1),  # 1 = most trending
            snapshot_at=now,
        )
        snapshots.append(snap)

    # Persist
    if snapshots and db._conn is not None:
        for snap in snapshots:
            await db._conn.execute(
                """INSERT INTO trending_snapshots
                   (coin_id, symbol, name, market_cap_rank, trending_score, snapshot_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    snap.coin_id,
                    snap.symbol,
                    snap.name,
                    snap.market_cap_rank,
                    snap.trending_score,
                    snap.snapshot_at.isoformat(),
                ),
            )
        await db._conn.commit()
        logger.info("trending_tracker.stored_snapshots", count=len(snapshots))

    return snapshots


async def store_trending_from_candidates(
    db: "Database",
    candidates: "list[CandidateToken]",
) -> list[TrendingSnapshot]:
    """Store trending snapshots from already-fetched CandidateToken list.

    This avoids a duplicate /search/trending API call when the main pipeline
    has already fetched trending data via ``cg_fetch_trending``.
    """
    now = datetime.now(timezone.utc)
    snapshots: list[TrendingSnapshot] = []

    for token in candidates:
        if not token.contract_address or token.contract_address == "unknown":
            continue

        snap = TrendingSnapshot(
            coin_id=token.contract_address,  # CoinGecko slug stored here
            symbol=token.ticker,
            name=token.token_name,
            market_cap_rank=None,
            trending_score=float(token.cg_trending_rank) if token.cg_trending_rank else None,
            snapshot_at=now,
        )
        snapshots.append(snap)

    if snapshots and db._conn is not None:
        for snap in snapshots:
            await db._conn.execute(
                """INSERT INTO trending_snapshots
                   (coin_id, symbol, name, market_cap_rank, trending_score, snapshot_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    snap.coin_id,
                    snap.symbol,
                    snap.name,
                    snap.market_cap_rank,
                    snap.trending_score,
                    snap.snapshot_at.isoformat(),
                ),
            )
        await db._conn.commit()
        logger.info("trending_tracker.stored_from_candidates", count=len(snapshots))

    return snapshots


async def compare_with_signals(db: "Database") -> list[TrendingComparison]:
    """For each token that appeared on trending in the last 24h, check if
    our system detected it earlier.

    Returns list of comparison results.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    # 1. Get distinct coins from trending_snapshots in last 24h
    cursor = await db._conn.execute(
        """SELECT coin_id, symbol, name, MIN(snapshot_at) as first_trending_at
           FROM trending_snapshots
           WHERE snapshot_at >= datetime('now', '-24 hours')
           GROUP BY coin_id""",
    )
    trending_rows = await cursor.fetchall()
    if not trending_rows:
        logger.info("trending_tracker.compare_no_data")
        return []

    comparisons: list[TrendingComparison] = []

    for row in trending_rows:
        coin_id = row[0]
        symbol = row[1]
        name = row[2]
        first_trending_at_str = row[3]
        first_trending_at = _parse_dt(first_trending_at_str)

        comp = TrendingComparison(
            coin_id=coin_id,
            symbol=symbol,
            name=name,
            appeared_on_trending_at=first_trending_at,
        )

        # 2a. Check predictions table (narrative agent)
        cursor = await db._conn.execute(
            """SELECT MIN(predicted_at) FROM predictions
               WHERE (coin_id = ? OR LOWER(symbol) = LOWER(?))
                 AND predicted_at < datetime(?, '+5 minutes')""",
            (coin_id, symbol, first_trending_at_str),
        )
        pred_row = await cursor.fetchone()
        if pred_row and pred_row[0]:
            pred_at = _parse_dt(pred_row[0])
            lead = (first_trending_at - pred_at).total_seconds() / 60.0
            if lead < 0:
                lead = 0  # detected after, but within tolerance window
            comp.detected_by_narrative = True
            comp.narrative_detected_at = pred_at
            comp.narrative_lead_minutes = lead
            comp.is_gap = False

        # 2b. Check candidates table (pipeline)
        cursor = await db._conn.execute(
            """SELECT MIN(first_seen_at) FROM candidates
               WHERE (contract_address = ? OR LOWER(ticker) = LOWER(?))
                 AND first_seen_at < datetime(?, '+5 minutes')""",
            (coin_id, symbol, first_trending_at_str),
        )
        cand_row = await cursor.fetchone()
        if cand_row and cand_row[0]:
            cand_at = _parse_dt(cand_row[0])
            lead = (first_trending_at - cand_at).total_seconds() / 60.0
            if lead < 0:
                lead = 0  # detected after, but within tolerance window
            comp.detected_by_pipeline = True
            comp.pipeline_detected_at = cand_at
            comp.pipeline_lead_minutes = lead
            comp.is_gap = False

        # 2c. Check signal_events table (chain signals)
        # Match on coin_id (CoinGecko slug) exactly, or symbol via LIKE prefix
        # to handle cases like token_id="bless" matching coin_id="bless-network".
        # Only use LIKE for symbols >= 4 chars to avoid short-symbol false positives.
        if len(symbol) >= 4:
            cursor = await db._conn.execute(
                """SELECT MIN(created_at) FROM signal_events
                   WHERE (token_id = ? OR LOWER(token_id) = LOWER(?)
                          OR LOWER(token_id) LIKE LOWER(? || '%')
                          OR LOWER(?) LIKE LOWER(token_id || '%'))
                     AND created_at < datetime(?, '+5 minutes')""",
                (coin_id, symbol, symbol, coin_id, first_trending_at_str),
            )
        else:
            cursor = await db._conn.execute(
                """SELECT MIN(created_at) FROM signal_events
                   WHERE (token_id = ? OR LOWER(token_id) = LOWER(?))
                     AND created_at < datetime(?, '+5 minutes')""",
                (coin_id, symbol, first_trending_at_str),
            )
        sig_row = await cursor.fetchone()
        if sig_row and sig_row[0]:
            sig_at = _parse_dt(sig_row[0])
            lead = (first_trending_at - sig_at).total_seconds() / 60.0
            if lead < 0:
                lead = 0  # detected after, but within tolerance window
            comp.detected_by_chains = True
            comp.chains_detected_at = sig_at
            comp.chains_lead_minutes = lead
            comp.is_gap = False

        comparisons.append(comp)

    # 3. Look up detected_price from price_cache and preserve existing peaks
    detected_prices: dict[str, float | None] = {}
    existing_peaks: dict[str, tuple[float | None, float | None]] = {}
    for comp in comparisons:
        # Preserve peak from previous row (if it was already tracked)
        old_cursor = await db._conn.execute(
            "SELECT detected_price, peak_price, peak_gain_pct FROM trending_comparisons WHERE coin_id = ?",
            (comp.coin_id,),
        )
        old_row = await old_cursor.fetchone()
        if old_row and old_row[0]:
            detected_prices[comp.coin_id] = old_row[0]
            existing_peaks[comp.coin_id] = (old_row[1], old_row[2])
        else:
            # First time: look up current price from price_cache as detected_price
            pc = await db._conn.execute(
                "SELECT current_price FROM price_cache WHERE coin_id = ?",
                (comp.coin_id,),
            )
            price_row = await pc.fetchone()
            detected_prices[comp.coin_id] = price_row[0] if price_row and price_row[0] else None
            existing_peaks[comp.coin_id] = (None, None)

    # 4. Store comparisons (INSERT OR REPLACE by coin_id)
    for comp in comparisons:
        # Delete old comparison for this coin_id to avoid duplicates
        await db._conn.execute(
            "DELETE FROM trending_comparisons WHERE coin_id = ?",
            (comp.coin_id,),
        )
        det_price = detected_prices.get(comp.coin_id)
        old_peak, old_peak_pct = existing_peaks.get(comp.coin_id, (None, None))
        await db._conn.execute(
            """INSERT INTO trending_comparisons
               (coin_id, symbol, name, appeared_on_trending_at,
                detected_by_narrative, narrative_detected_at, narrative_lead_minutes,
                detected_by_pipeline, pipeline_detected_at, pipeline_lead_minutes,
                detected_by_chains, chains_detected_at, chains_lead_minutes,
                is_gap, detected_price, peak_price, peak_gain_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                comp.coin_id,
                comp.symbol,
                comp.name,
                comp.appeared_on_trending_at.isoformat(),
                1 if comp.detected_by_narrative else 0,
                comp.narrative_detected_at.isoformat() if comp.narrative_detected_at else None,
                comp.narrative_lead_minutes,
                1 if comp.detected_by_pipeline else 0,
                comp.pipeline_detected_at.isoformat() if comp.pipeline_detected_at else None,
                comp.pipeline_lead_minutes,
                1 if comp.detected_by_chains else 0,
                comp.chains_detected_at.isoformat() if comp.chains_detected_at else None,
                comp.chains_lead_minutes,
                1 if comp.is_gap else 0,
                det_price,
                old_peak,
                old_peak_pct,
            ),
        )
    await db._conn.commit()
    logger.info(
        "trending_tracker.comparisons_stored",
        total=len(comparisons),
        caught=sum(1 for c in comparisons if not c.is_gap),
        gaps=sum(1 for c in comparisons if c.is_gap),
    )
    return comparisons


async def get_trending_stats(db: "Database") -> TrendingStats:
    """Compute aggregate hit rate, average lead time, and gap counts."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute("SELECT COUNT(*) FROM trending_comparisons")
    total = (await cursor.fetchone())[0]

    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM trending_comparisons WHERE is_gap = 0"
    )
    caught = (await cursor.fetchone())[0]

    missed = total - caught

    # Average and best lead time across all detection methods
    cursor = await db._conn.execute(
        """SELECT AVG(lead), MIN(lead) FROM (
             SELECT narrative_lead_minutes as lead FROM trending_comparisons
               WHERE detected_by_narrative = 1 AND narrative_lead_minutes IS NOT NULL
             UNION ALL
             SELECT pipeline_lead_minutes FROM trending_comparisons
               WHERE detected_by_pipeline = 1 AND pipeline_lead_minutes IS NOT NULL
             UNION ALL
             SELECT chains_lead_minutes FROM trending_comparisons
               WHERE detected_by_chains = 1 AND chains_lead_minutes IS NOT NULL
           )"""
    )
    lead_row = await cursor.fetchone()
    avg_lead = round(lead_row[0], 1) if lead_row and lead_row[0] is not None else None
    best_lead = round(lead_row[1], 1) if lead_row and lead_row[1] is not None else None

    # Counts by detection method
    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM trending_comparisons WHERE detected_by_narrative = 1"
    )
    by_narrative = (await cursor.fetchone())[0]

    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM trending_comparisons WHERE detected_by_pipeline = 1"
    )
    by_pipeline = (await cursor.fetchone())[0]

    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM trending_comparisons WHERE detected_by_chains = 1"
    )
    by_chains = (await cursor.fetchone())[0]

    hit_rate = round((caught / total * 100) if total > 0 else 0, 1)

    return TrendingStats(
        total_tracked=total,
        caught_before_trending=caught,
        missed=missed,
        hit_rate_pct=hit_rate,
        avg_lead_minutes=avg_lead,
        best_lead_minutes=best_lead,
        by_narrative=by_narrative,
        by_pipeline=by_pipeline,
        by_chains=by_chains,
    )


async def get_recent_snapshots(
    db: "Database", hours: int = 24, limit: int = 100
) -> list[dict]:
    """Get recent trending snapshots for the dashboard."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute(
        """SELECT coin_id, symbol, name, market_cap_rank, trending_score,
                  snapshot_at, created_at
           FROM trending_snapshots
           WHERE snapshot_at >= datetime('now', ?)
           ORDER BY snapshot_at DESC, trending_score ASC
           LIMIT ?""",
        (f"-{hours} hours", limit),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_recent_comparisons(
    db: "Database", limit: int = 100
) -> list[dict]:
    """Get recent trending comparisons for the dashboard."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute(
        """SELECT coin_id, symbol, name, appeared_on_trending_at,
                  detected_by_narrative, narrative_detected_at, narrative_lead_minutes,
                  detected_by_pipeline, pipeline_detected_at, pipeline_lead_minutes,
                  detected_by_chains, chains_detected_at, chains_lead_minutes,
                  is_gap, detected_price, peak_price, peak_gain_pct, created_at
           FROM trending_comparisons
           ORDER BY COALESCE(chains_detected_at, narrative_detected_at, pipeline_detected_at, appeared_on_trending_at) DESC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def update_trending_peaks(db: "Database") -> int:
    """Update peak prices for all trending comparisons using current price_cache data.

    Uses a single JOIN query instead of N+1 per-row lookups.
    Only uses prices updated within the last hour to avoid stale peaks.
    Returns the number of rows updated.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    conn = db._conn
    # Batch: JOIN comparisons with price_cache, filter fresh prices only
    cursor = await conn.execute(
        """SELECT tc.id, tc.coin_id, tc.detected_price, tc.peak_price,
                  pc.current_price, pc.updated_at
           FROM trending_comparisons tc
           JOIN price_cache pc ON tc.coin_id = pc.coin_id
           WHERE tc.detected_price IS NOT NULL
             AND tc.detected_price > 0
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
                "UPDATE trending_comparisons SET peak_price = ?, peak_gain_pct = ? WHERE id = ?",
                (current_price, peak_gain, row["id"]),
            )
            updated += 1

    if updated:
        await conn.commit()
        logger.info("trending_tracker.peaks_updated", count=updated)

    return updated
