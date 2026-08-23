"""Top Gainers tracking -- store snapshots and compare with pipeline signals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog

from scout.identity import (
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
        logger.info(
            "gainers_snapshots_pruned", deleted=deleted, retention_days=retention_days
        )
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

    gainers.sort(key=lambda c: c.get("price_change_percentage_24h") or 0, reverse=True)

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


# CG-slug-keyed surfaces that all share the same (coin_id, detected_at) shape:
# the gap-fill acceleration detector plus the markets-watcher detectors whose
# detections the tracker previously did not credit. Table names are fixed
# constants (never user input), so the f-string is injection-safe.
_COIN_ID_SURFACES = (
    ("acceleration", "gainer_acceleration"),
    ("momentum", "momentum_7d"),
    ("slow_burn", "slow_burn_candidates"),
    ("velocity", "velocity_alerts"),
)


async def _coin_id_surface_lead(
    db: "Database",
    table: str,
    coin_id: str,
    first_gainer_at,
    first_gainer_at_str: str,
) -> "float | None":
    """Earliest detection lead (minutes) for a coin_id-keyed surface, or None.

    Mirrors the tolerance + clamp of the inline surface checks: a detection
    strictly before ``first_gainer_at + 5min`` counts; leads inside the
    tolerance window (detected just after) clamp to 0.
    """
    # Both sides wrapped in datetime() so the comparison is on normalized
    # space-format timestamps: detectors write detected_at via Python
    # isoformat() (`...T..+00:00`), and a bare `<` against datetime()'s
    # space-format output would mis-compare on the 'T' (0x54 > 0x20 space).
    cursor = await db._conn.execute(
        f"SELECT MIN(detected_at) FROM {table} "
        f"WHERE coin_id = ? AND datetime(detected_at) < datetime(?, '+5 minutes')",
        (coin_id, first_gainer_at_str),
    )
    row = await cursor.fetchone()
    if row and row[0]:
        detected_at = _parse_dt(row[0])
        lead = (first_gainer_at - detected_at).total_seconds() / 60.0
        return max(lead, 0.0)
    return None


async def compare_gainers_with_signals(db: "Database") -> list[dict]:
    """For each top gainer in last 24h, check if our system detected it earlier.

    Same pattern as trending compare_with_signals.
    Returns list of comparison dicts.

    Reading `gainers_tracker.compare_started` in prod logs
    -----------------------------------------------------
    The marker below distinguishes "this run is still grinding" from "this run
    never started". It does NOT, on its own, mean this function never ran —
    and reading it that way has produced two wrong diagnoses already.

    Its ABSENCE is evidence of "never ran" only after both of these are
    confirmed, in this order:

    1. The narrative agent's EVALUATE loop is alive. This function is called
       only from that loop (`scout/narrative/agent.py`); if the loop is wedged
       or the process is down, nothing here emits regardless of state.
    2. `GAINERS_TRACKER_ENABLED` is on for the running process. The call site
       is gated by it, and a disabled lane is silent by construction — see the
       `gainers_tracker_disabled_by_flag` boot line in `scout/main.py`, which
       exists precisely so this check is a grep rather than an inference.

    With both confirmed, absence is still ambiguous in one benign direction:
    the marker sits AFTER the empty-set early return, so a window with no
    gainers logs `compare_no_data` and no `compare_started`. Check for that
    event before concluding anything is broken.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    # Get distinct gainers from last 24h
    cursor = await db._conn.execute(
        """SELECT coin_id, symbol, name,
                  MAX(price_change_24h) as price_change_24h,
                  MIN(snapshot_at) as first_gainer_at
           FROM gainers_snapshots
           WHERE datetime(snapshot_at) >= datetime('now', '-24 hours')
           GROUP BY coin_id""",
    )
    gainer_rows = await cursor.fetchall()
    if not gainer_rows:
        logger.info("gainers_tracker.compare_no_data")
        return []

    # In-flight marker. The loop below runs several per-coin subqueries against
    # predictions/candidates/chain_matches for every gainer, so a real run can
    # take minutes; until this existed the function emitted nothing between
    # entry and `comparisons_stored`, which made "still grinding" and "never
    # ran" identical in the logs. Deliberately AFTER the empty-set return, so a
    # no-op run does not open a started/complete pair it never closes.
    # `min`/`max` are over the already-materialized rows — no extra query. Note
    # both bounds are over per-coin FIRST-APPEARANCE times (the `MIN(snapshot_at)`
    # this query groups by coin), NOT over every snapshot in the window: so
    # `newest_gainer_at` is the most recent coin to first appear, not the most
    # recent snapshot taken.
    #
    # Before reading a MISSING `compare_started` as "this never ran", see the
    # function docstring: two prerequisites (EVALUATE loop alive,
    # GAINERS_TRACKER_ENABLED on) have to be confirmed first, and skipping them
    # has already produced two wrong diagnoses.
    snapshot_times = [row[4] for row in gainer_rows if row[4] is not None]
    logger.info(
        "gainers_tracker.compare_started",
        coins=len(gainer_rows),
        oldest_gainer_at=min(snapshot_times) if snapshot_times else None,
        newest_gainer_at=max(snapshot_times) if snapshot_times else None,
    )

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
            "detected_by_acceleration": 0,
            "acceleration_lead_minutes": None,
            "detected_by_momentum": 0,
            "momentum_lead_minutes": None,
            "detected_by_slow_burn": 0,
            "slow_burn_lead_minutes": None,
            "detected_by_velocity": 0,
            "velocity_lead_minutes": None,
            "is_gap": 1,
        }

        # Check predictions table (narrative agent)
        cursor = await db._conn.execute(
            """SELECT MIN(predicted_at) FROM predictions
               WHERE (coin_id = ? OR LOWER(symbol) = LOWER(?))
                 AND datetime(predicted_at) < datetime(?, '+5 minutes')""",
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
                 AND datetime(first_seen_at) < datetime(?, '+5 minutes')""",
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

        # Chain-signal detection, resolved by ASSET IDENTITY (ruling C).
        #
        # Option F moved this off unbounded signal_events history; ruling C
        # stops prefix similarity deciding who it belongs to. Prefix matching
        # survives only as a diagnostic -- `re` must never again be credited as
        # `real-world-apparel` merely because it is a prefix and older.
        #
        # Symbol length no longer branches the truth path. That split was itself
        # a defect: it derived first-seen from two different historical
        # boundaries depending on how many characters a ticker had.
        res = await resolve_chain_first_seen(
            db._conn,
            coin_id,
            symbol,
            first_gainer_at_str,
            prefix_diagnostic=len(symbol) >= 4,
        )
        comp["chains_identity_semantics"] = CANONICAL_SEMANTICS
        comp["chains_identity_tier"] = res.tier
        if res.detected:
            sig_at = _parse_dt(res.first_seen_at)
            lead = (first_gainer_at - sig_at).total_seconds() / 60.0
            if lead < 0:
                lead = 0  # detected after, but within tolerance window
            comp["detected_by_chains"] = 1
            comp["chains_lead_minutes"] = round(lead, 1)
            comp["is_gap"] = 0
        if res.prefix_would_have_credited_more:
            # The fabricated lead the old semantics handed out, measured rather
            # than asserted. Not written to any truth column.
            logger.info(
                "chain_identity_prefix_discarded",
                surface="gainers",
                coin_id=coin_id,
                symbol=symbol,
                resolved_tier=res.tier,
                resolved_token=res.token_id,
                discarded_prefix_token=res.prefix_token_id,
                discarded_prefix_first_seen=res.prefix_first_seen_at,
            )

        # Check volume_spikes table
        cursor = await db._conn.execute(
            """SELECT MIN(detected_at) FROM volume_spikes
               WHERE coin_id = ? AND datetime(detected_at) < datetime(?, '+5 minutes')""",
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

        # Net-new CG-slug-keyed surfaces: gap-fill acceleration + the
        # markets-watcher detectors (momentum_7d / slow_burn / velocity) whose
        # early detections the tracker previously did not credit.
        for surface, table in _COIN_ID_SURFACES:
            lead = await _coin_id_surface_lead(
                db, table, coin_id, first_gainer_at, first_gainer_at_str
            )
            if lead is not None:
                comp[f"detected_by_{surface}"] = 1
                comp[f"{surface}_lead_minutes"] = round(lead, 1)
                comp["is_gap"] = 0

        comparisons.append(comp)

    # Look up detected_price from price_cache and preserve existing peaks
    for comp in comparisons:
        cid = comp["coin_id"]
        old_cursor = await db._conn.execute(
            "SELECT detected_price, peak_price, peak_gain_pct, "
            "entry_basis_price, entry_basis_at "
            "FROM gainers_comparisons WHERE coin_id = ?",
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
            comp["detected_price"] = (
                price_row[0] if price_row and price_row[0] else None
            )
            comp["peak_price"] = None
            comp["peak_gain_pct"] = None

        # ANCHOR THE ENTRY BASIS — establish once, never overwrite.
        #
        # An already-established basis is carried forward verbatim, so neither
        # this rewrite (DELETE + INSERT on every run), nor re-enrollment, nor
        # retention pruning the snapshot it came from can move it. Recomputing
        # it here would rebase the entry onto the next surviving snapshot the
        # moment the original aged out — silently, with no event.
        #
        # FAIL CLOSED on a partial pair. If EITHER persisted component exists
        # the pair is carried forward verbatim — including when it is
        # incomplete. Re-establishing from a later surviving snapshot would
        # "repair" the row by silently rebasing it onto a different
        # observation, which is the very move this anchoring prevents. An
        # incomplete pair reads as unavailable downstream; that is correct and
        # recoverable, whereas a wrong anchor is neither.
        if old_row is not None and (old_row[3] is not None or old_row[4] is not None):
            comp["entry_basis_price"] = old_row[3]
            comp["entry_basis_at"] = old_row[4]
        else:
            # First establishment: the earliest snapshot row that still exists
            # AND carries a usable price. Price and timestamp are taken from
            # the SAME row so they describe one observation.
            basis_cursor = await db._conn.execute(
                """SELECT price_at_snapshot, snapshot_at
                     FROM gainers_snapshots
                    WHERE coin_id = ?
                      AND price_at_snapshot IS NOT NULL
                      AND price_at_snapshot > 0
                    ORDER BY snapshot_at ASC
                    LIMIT 1""",
                (cid,),
            )
            basis_row = await basis_cursor.fetchone()
            comp["entry_basis_price"] = basis_row[0] if basis_row else None
            comp["entry_basis_at"] = basis_row[1] if basis_row else None

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
                detected_by_acceleration, acceleration_lead_minutes,
                detected_by_momentum, momentum_lead_minutes,
                detected_by_slow_burn, slow_burn_lead_minutes,
                detected_by_velocity, velocity_lead_minutes,
                is_gap, detected_price, peak_price, peak_gain_pct,
                entry_basis_price, entry_basis_at,
                chains_identity_semantics, chains_identity_tier)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                comp["detected_by_acceleration"],
                comp["acceleration_lead_minutes"],
                comp["detected_by_momentum"],
                comp["momentum_lead_minutes"],
                comp["detected_by_slow_burn"],
                comp["slow_burn_lead_minutes"],
                comp["detected_by_velocity"],
                comp["velocity_lead_minutes"],
                comp["is_gap"],
                comp["detected_price"],
                comp["peak_price"],
                comp["peak_gain_pct"],
                comp["entry_basis_price"],
                comp["entry_basis_at"],
                # Ruling C: which semantics produced the chain columns on THIS
                # row. Historical rows carry `legacy_prefix` and are never
                # recomputed in place.
                comp.get("chains_identity_semantics"),
                comp.get("chains_identity_tier"),
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


async def get_recent_gainers(db: "Database", limit: int = 20) -> list[dict]:
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


async def get_gainers_comparisons(db: "Database", limit: int = 50) -> list[dict]:
    """Get gainers comparisons for the dashboard."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await db._conn.execute(
        """SELECT gainers_comparisons.coin_id, gainers_comparisons.symbol, name, price_change_24h,
                  appeared_on_gainers_at,
                  detected_by_narrative, narrative_lead_minutes,
                  detected_by_pipeline, pipeline_lead_minutes,
                  detected_by_chains, chains_lead_minutes,
                  -- Ruling C: consumers MUST be able to tell prefix-derived
                  -- rows from identity-derived ones. Without these the
                  -- columns are write-only and the conviction gate in
                  -- scout/conviction/cross_surface.py cannot see them.
                  chains_identity_semantics, chains_identity_tier,
                  detected_by_spikes, spikes_lead_minutes,
                  detected_by_acceleration, acceleration_lead_minutes,
                  detected_by_momentum, momentum_lead_minutes,
                  detected_by_slow_burn, slow_burn_lead_minutes,
                  detected_by_velocity, velocity_lead_minutes,
                  is_gap, detected_price, peak_price, peak_gain_pct, created_at,
                  -- Ruling C: the recomputed provenance overlay for LEGACY rows.
                  -- Joined on coin_id, not row id: the tracker deletes and
                  -- re-inserts by coin_id on every recompute, so ids do not
                  -- survive. Only legacy rows consult it; canonical_v1 rows
                  -- carry their own tier.
                  (SELECT cir.evidence_status
                     FROM chain_identity_recompute_v1 cir
                    WHERE cir.source_table = 'gainers_comparisons'
                      AND cir.coin_id = gainers_comparisons.coin_id
                      AND cir.historical_anchor = gainers_comparisons.appeared_on_gainers_at
                      -- ONLY legacy rows consult the overlay. Without this
                      -- the comment above was false: a `canonical_v1` row,
                      -- resolved correctly at write time, had its own lead
                      -- overwritten by an offline replay of a DIFFERENT
                      -- (archived) row sharing its coin_id and anchor --
                      -- and cross_surface applies chains_canonical_lead
                      -- unconditionally, so that silently stripped its
                      -- chains credit. The anchor is MIN(snapshot_at) over
                      -- a trailing 24h, so a live canonical row and an
                      -- archived legacy row share one for about a day
                      -- after the backfill: exactly the window in which
                      -- the tier_high question is being judged.
                      AND COALESCE(gainers_comparisons.chains_identity_semantics,
                                   'legacy_prefix') != 'canonical_v1'
                    -- canonical FIRST: this ORDER BY is ascending, so the
                    -- credit-bearing status must sort LOWEST. Written the
                    -- other way round it preferred the non-credit-bearing
                    -- row wherever both exist at one (coin_id, anchor) --
                    -- silently discarding verified credit in favour of a
                    -- prefix-only sibling.
                    ORDER BY CASE cir.evidence_status
                             WHEN 'verified_canonical' THEN 0 ELSE 1 END
                    LIMIT 1) AS chains_recompute_status,
                  -- The VERIFIED lead, not the legacy prefix-derived one.
                  -- Scoring chains_lead_minutes after verifying canonical_lead
                  -- would decouple the claim from the value.
                  (SELECT cir.canonical_lead
                     FROM chain_identity_recompute_v1 cir
                    WHERE cir.source_table = 'gainers_comparisons'
                      AND cir.coin_id = gainers_comparisons.coin_id
                      AND cir.historical_anchor = gainers_comparisons.appeared_on_gainers_at
                      -- ONLY legacy rows consult the overlay. Without this
                      -- the comment above was false: a `canonical_v1` row,
                      -- resolved correctly at write time, had its own lead
                      -- overwritten by an offline replay of a DIFFERENT
                      -- (archived) row sharing its coin_id and anchor --
                      -- and cross_surface applies chains_canonical_lead
                      -- unconditionally, so that silently stripped its
                      -- chains credit. The anchor is MIN(snapshot_at) over
                      -- a trailing 24h, so a live canonical row and an
                      -- archived legacy row share one for about a day
                      -- after the backfill: exactly the window in which
                      -- the tier_high question is being judged.
                      AND COALESCE(gainers_comparisons.chains_identity_semantics,
                                   'legacy_prefix') != 'canonical_v1'
                    -- canonical FIRST: this ORDER BY is ascending, so the
                    -- credit-bearing status must sort LOWEST. Written the
                    -- other way round it preferred the non-credit-bearing
                    -- row wherever both exist at one (coin_id, anchor) --
                    -- silently discarding verified credit in favour of a
                    -- prefix-only sibling.
                    ORDER BY CASE cir.evidence_status
                             WHEN 'verified_canonical' THEN 0 ELSE 1 END
                    LIMIT 1) AS chains_canonical_lead
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

    cursor = await db._conn.execute("SELECT COUNT(*) FROM gainers_comparisons")
    total = (await cursor.fetchone())[0]

    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM gainers_comparisons WHERE is_gap = 0"
    )
    caught = (await cursor.fetchone())[0]

    missed = total - caught

    # Average lead time across all detection methods
    cursor = await db._conn.execute("""SELECT AVG(lead) FROM (
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
             UNION ALL
             SELECT acceleration_lead_minutes FROM gainers_comparisons
               WHERE detected_by_acceleration = 1
                 AND acceleration_lead_minutes IS NOT NULL
             UNION ALL
             SELECT momentum_lead_minutes FROM gainers_comparisons
               WHERE detected_by_momentum = 1 AND momentum_lead_minutes IS NOT NULL
             UNION ALL
             SELECT slow_burn_lead_minutes FROM gainers_comparisons
               WHERE detected_by_slow_burn = 1 AND slow_burn_lead_minutes IS NOT NULL
             UNION ALL
             SELECT velocity_lead_minutes FROM gainers_comparisons
               WHERE detected_by_velocity = 1 AND velocity_lead_minutes IS NOT NULL
           )""")
    lead_row = await cursor.fetchone()
    avg_lead = round(lead_row[0], 1) if lead_row and lead_row[0] is not None else None

    hit_rate = round((caught / total * 100) if total > 0 else 0, 1)

    # SPLIT, do not exclude.
    #
    # `avg_lead_minutes` UNIONs eight surfaces into one scalar and
    # `hit_rate_pct` counts any non-gap row, so both currently mix
    # prefix-derived chains credit with identity-derived credit. Dropping the
    # legacy rows would move the headline for a compositional reason no reader
    # can see -- and with CG ingest quota-stalled there are ~0 canonical rows
    # yet, so the average would silently become a seven-surface number with
    # nothing saying so. That is the "every headline needs its selection axis"
    # failure, not a fix for it.
    #
    # So the aggregates stay as-recorded and the MIX becomes visible instead.
    # Note the deliberate asymmetry with scout/conviction/cross_surface.py,
    # which DOES refuse prefix-derived chains credit: conviction is a
    # forward-looking score being computed now, while these are the historical
    # record of what the system actually did. Rewriting the record is the
    # evidence destruction the ruling forbids; scoring off unverified
    # provenance is what it forbids. Both sites are governed by that one
    # distinction -- if it ever stops holding, change them together.
    cursor = await db._conn.execute("""
        SELECT
            SUM(chains_identity_semantics = 'canonical_v1'),
            SUM(chains_identity_semantics = 'legacy_prefix'),
            SUM(chains_identity_semantics IS NULL)
        FROM gainers_comparisons WHERE detected_by_chains = 1""")
    mix = await cursor.fetchone() or (0, 0, 0)

    return {
        "total_tracked": total,
        "caught": caught,
        "missed": missed,
        "hit_rate_pct": hit_rate,
        "avg_lead_minutes": avg_lead,
        # Provenance mix of the chains contribution to the two figures above.
        "n_chains_canonical": mix[0] or 0,
        "n_chains_legacy_prefix": mix[1] or 0,
        "n_chains_unstamped": mix[2] or 0,
    }


async def update_gainers_peaks(db: "Database", *, caller: str = "unattributed") -> int:
    """Update peak prices for all gainers comparisons using current price_cache data.

    Uses a single JOIN query instead of N+1 per-row lookups.
    Only uses prices updated within the last hour to avoid stale peaks.
    Returns the number of rows updated.

    ``caller`` labels the call site on the emitted ``peaks_updated`` event. Two
    loops drive this function at different cadences — the per-cycle pipeline and
    the narrative agent's EVALUATE interval — and one shared event name cannot
    say which of them ran. That ambiguity produced two wrong production
    diagnoses during #512 verification. Same convention (and same
    ``"unattributed"`` default) as ``alerter.send_telegram_message(source=...)``.
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
             AND datetime(pc.updated_at) >= datetime('now', '-1 hour')"""
    )
    rows = await cursor.fetchall()
    updated = 0

    for row in rows:
        current_price = row["current_price"]
        old_peak = row["peak_price"] or row["detected_price"] or 0

        if current_price > old_peak:
            peak_gain = (
                (current_price - row["detected_price"]) / row["detected_price"]
            ) * 100
            await conn.execute(
                "UPDATE gainers_comparisons SET peak_price = ?, peak_gain_pct = ? WHERE id = ?",
                (current_price, peak_gain, row["id"]),
            )
            updated += 1

    if updated:
        await conn.commit()
        logger.info(
            "gainers_tracker.peaks_updated",
            tracker="gainers",
            count=updated,
            caller=caller,
        )
    else:
        # A run that updated nothing used to emit NOTHING, so "the peak updater
        # ran and no price beat its peak" and "the peak updater never ran" were
        # the same absence in journald. debug, not info: this is the common case
        # on a quiet cycle and promoting it would multiply the log volume of
        # both loops. `examined` separates the two no-op shapes — zero rows
        # joined price_cache (stale or empty cache) vs rows joined but none
        # higher than their stored peak.
        logger.debug(
            "gainers_tracker.peaks_noop",
            tracker="gainers",
            count=0,
            caller=caller,
            examined=len(rows),
        )

    return updated
