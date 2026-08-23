"""Top Losers tracking -- store snapshots and compare with pipeline signals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog

from scout.identity import (
    BOUND_RAW_LT,
    CANONICAL_SEMANTICS,
    resolve_chain_first_seen,
)

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
    """Delete losers_snapshots older than retention_days. Returns rows deleted."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    cursor = await db._conn.execute(
        "DELETE FROM losers_snapshots WHERE snapshot_at < ?", (cutoff,)
    )
    await db._conn.commit()
    deleted = cursor.rowcount
    if deleted:
        logger.info(
            "losers_snapshots_pruned", deleted=deleted, retention_days=retention_days
        )
    return deleted


async def store_top_losers(
    db: "Database",
    raw_coins: list[dict],
    max_drop: float = -15.0,
    max_mcap: float = 500_000_000,
) -> int:
    """Store top losers from /coins/markets response.

    Filters for tokens with <max_drop% 24h loss and <max_mcap market cap.
    Returns the number of rows stored.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    now = datetime.now(timezone.utc).isoformat()
    count = 0

    # Filter: negative change worse than max_drop, small/mid cap
    losers = []
    for coin in raw_coins:
        coin_id = coin.get("id")
        if not coin_id:
            continue
        change = coin.get("price_change_percentage_24h") or 0
        mcap = coin.get("market_cap") or 0
        if change <= max_drop and 0 < mcap < max_mcap:
            losers.append(coin)

    # Sort by price change ascending (biggest drops first)
    losers.sort(key=lambda c: c.get("price_change_percentage_24h") or 0)

    # Take top 20
    for coin in losers[:20]:
        await db._conn.execute(
            """INSERT INTO losers_snapshots
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
        logger.info("losers_snapshots_stored", count=count)

    return count


async def compare_losers_with_signals(db: "Database") -> list[dict]:
    """For each top loser in last 24h, check if our system detected it earlier.

    Same pattern as gainers compare_with_signals.
    Returns list of comparison dicts.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    # Get distinct losers from last 24h
    cursor = await db._conn.execute(
        """SELECT coin_id, symbol, name,
                  MIN(price_change_24h) as price_change_24h,
                  MIN(snapshot_at) as first_loser_at
           FROM losers_snapshots
           WHERE datetime(snapshot_at) >= datetime('now', '-24 hours')
           GROUP BY coin_id""",
    )
    loser_rows = await cursor.fetchall()
    if not loser_rows:
        logger.info("losers_tracker.compare_no_data")
        return []

    comparisons: list[dict] = []

    for row in loser_rows:
        coin_id = row[0]
        symbol = row[1]
        name = row[2]
        price_change_24h = row[3]
        first_loser_at_str = row[4]
        first_loser_at = _parse_dt(first_loser_at_str)

        comp: dict = {
            "coin_id": coin_id,
            "symbol": symbol,
            "name": name,
            "price_change_24h": price_change_24h,
            "appeared_on_losers_at": first_loser_at.isoformat(),
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
                 AND predicted_at < ?""",
            (coin_id, symbol, first_loser_at_str),
        )
        pred_row = await cursor.fetchone()
        if pred_row and pred_row[0]:
            pred_at = _parse_dt(pred_row[0])
            lead = (first_loser_at - pred_at).total_seconds() / 60.0
            comp["detected_by_narrative"] = 1
            comp["narrative_lead_minutes"] = round(lead, 1)
            comp["is_gap"] = 0

        # Check candidates table (pipeline)
        cursor = await db._conn.execute(
            """SELECT MIN(first_seen_at) FROM candidates
               WHERE (contract_address = ? OR LOWER(ticker) = LOWER(?))
                 AND first_seen_at < ?""",
            (coin_id, symbol, first_loser_at_str),
        )
        cand_row = await cursor.fetchone()
        if cand_row and cand_row[0]:
            cand_at = _parse_dt(cand_row[0])
            lead = (first_loser_at - cand_at).total_seconds() / 60.0
            comp["detected_by_pipeline"] = 1
            comp["pipeline_lead_minutes"] = round(lead, 1)
            comp["is_gap"] = 0

        # Check the DERIVED first-seen substrate (chain signals).
        # Option F: consumers must stop depending on unbounded
        # signal_events history, so retention stops silently moving
        # the derived minimum forward.
        # Ruling C: resolved by ASSET IDENTITY, not prefix similarity.
        # BOUND_RAW_LT preserves this surface's bare `first_seen_at < ?`
        # deliberately -- it compares in the same BINARY order the stored value
        # was minimised under, so aggregation and filter agree by construction.
        # Wrapping it in datetime() to match the other surfaces would be a
        # silent behaviour change, not a cleanup.
        res = await resolve_chain_first_seen(
            db._conn,
            coin_id,
            symbol,
            first_loser_at_str,
            prefix_diagnostic=len(symbol) >= 4,
            bound=BOUND_RAW_LT,
        )
        comp["chains_identity_semantics"] = CANONICAL_SEMANTICS
        comp["chains_identity_tier"] = res.tier
        if res.prefix_would_have_credited_more:
            logger.info(
                "chain_identity_prefix_discarded",
                surface="losers",
                coin_id=coin_id,
                symbol=symbol,
                resolved_tier=res.tier,
                resolved_token=res.token_id,
                discarded_prefix_token=res.prefix_token_id,
                discarded_prefix_first_seen=res.prefix_first_seen_at,
            )
        if res.detected:
            sig_at = _parse_dt(res.first_seen_at)
            lead = (first_loser_at - sig_at).total_seconds() / 60.0
            comp["detected_by_chains"] = 1
            comp["chains_lead_minutes"] = round(lead, 1)
            comp["is_gap"] = 0

        # Check volume_spikes table
        cursor = await db._conn.execute(
            """SELECT MIN(detected_at) FROM volume_spikes
               WHERE coin_id = ? AND detected_at < ?""",
            (coin_id, first_loser_at_str),
        )
        spike_row = await cursor.fetchone()
        if spike_row and spike_row[0]:
            spike_at = _parse_dt(spike_row[0])
            lead = (first_loser_at - spike_at).total_seconds() / 60.0
            comp["detected_by_spikes"] = 1
            comp["spikes_lead_minutes"] = round(lead, 1)
            comp["is_gap"] = 0

        comparisons.append(comp)

    # Store comparisons (delete old for same coin_id then insert)
    for comp in comparisons:
        await db._conn.execute(
            "DELETE FROM losers_comparisons WHERE coin_id = ?",
            (comp["coin_id"],),
        )
        await db._conn.execute(
            """INSERT INTO losers_comparisons
               (coin_id, symbol, name, price_change_24h,
                appeared_on_losers_at,
                detected_by_narrative, narrative_lead_minutes,
                detected_by_pipeline, pipeline_lead_minutes,
                detected_by_chains, chains_lead_minutes,
                detected_by_spikes, spikes_lead_minutes,
                is_gap,
                chains_identity_semantics, chains_identity_tier)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                comp["coin_id"],
                comp["symbol"],
                comp["name"],
                comp["price_change_24h"],
                comp["appeared_on_losers_at"],
                comp["detected_by_narrative"],
                comp["narrative_lead_minutes"],
                comp["detected_by_pipeline"],
                comp["pipeline_lead_minutes"],
                comp["detected_by_chains"],
                comp["chains_lead_minutes"],
                comp["detected_by_spikes"],
                comp["spikes_lead_minutes"],
                comp["is_gap"],
                comp.get("chains_identity_semantics"),
                comp.get("chains_identity_tier"),
            ),
        )
    await db._conn.commit()

    caught = sum(1 for c in comparisons if not c["is_gap"])
    logger.info(
        "losers_tracker.comparisons_stored",
        total=len(comparisons),
        caught=caught,
        gaps=len(comparisons) - caught,
    )
    return comparisons


async def get_recent_losers(db: "Database", limit: int = 20) -> list[dict]:
    """Get recent losers snapshots for the dashboard."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute(
        """SELECT coin_id, symbol, name, price_change_24h,
                  market_cap, volume_24h, snapshot_at, created_at
           FROM losers_snapshots
           ORDER BY snapshot_at DESC, price_change_24h ASC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_losers_comparisons(db: "Database", limit: int = 50) -> list[dict]:
    """Get losers comparisons for the dashboard."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute(
        """SELECT losers_comparisons.coin_id, losers_comparisons.symbol, name, price_change_24h,
                  appeared_on_losers_at,
                  detected_by_narrative, narrative_lead_minutes,
                  detected_by_pipeline, pipeline_lead_minutes,
                  detected_by_chains, chains_lead_minutes,
                  -- Ruling C: consumers MUST be able to tell prefix-derived
                  -- rows from identity-derived ones. Without these the
                  -- columns are write-only and the conviction gate in
                  -- scout/conviction/cross_surface.py cannot see them.
                  chains_identity_semantics, chains_identity_tier,
                  detected_by_spikes, spikes_lead_minutes,
                  is_gap, created_at,
                  -- Ruling C: the recomputed provenance overlay for LEGACY rows.
                  -- Joined on coin_id, not row id: the tracker deletes and
                  -- re-inserts by coin_id on every recompute, so ids do not
                  -- survive. Only legacy rows consult it; canonical_v1 rows
                  -- carry their own tier.
                  cir.evidence_status AS chains_recompute_status
           FROM losers_comparisons
           LEFT JOIN chain_identity_recompute_v1 cir
                  ON cir.source_table = 'losers_comparisons'
                 AND cir.coin_id = losers_comparisons.coin_id
           ORDER BY appeared_on_losers_at DESC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_losers_stats(db: "Database") -> dict:
    """Compute aggregate hit rate for losers tracking."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute("SELECT COUNT(*) FROM losers_comparisons")
    total = (await cursor.fetchone())[0]

    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM losers_comparisons WHERE is_gap = 0"
    )
    caught = (await cursor.fetchone())[0]

    missed = total - caught

    # Average lead time across all detection methods
    cursor = await db._conn.execute("""SELECT AVG(lead) FROM (
             SELECT narrative_lead_minutes as lead FROM losers_comparisons
               WHERE detected_by_narrative = 1 AND narrative_lead_minutes IS NOT NULL
             UNION ALL
             SELECT pipeline_lead_minutes FROM losers_comparisons
               WHERE detected_by_pipeline = 1 AND pipeline_lead_minutes IS NOT NULL
             UNION ALL
             SELECT chains_lead_minutes FROM losers_comparisons
               WHERE detected_by_chains = 1 AND chains_lead_minutes IS NOT NULL
             UNION ALL
             SELECT spikes_lead_minutes FROM losers_comparisons
               WHERE detected_by_spikes = 1 AND spikes_lead_minutes IS NOT NULL
           )""")
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
