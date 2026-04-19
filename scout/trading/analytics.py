"""On-demand analytics for paper-trading feedback loop (spec §5.1, §7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from scout.config import Settings
from scout.db import Database

log = structlog.get_logger()


async def combo_leaderboard(
    db: Database, window: str, min_trades: int = 10
) -> list[dict]:
    """Return combos sorted by WR desc. Deterministic tie-break."""
    cur = await db._conn.execute(
        "SELECT combo_key, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        "       win_rate_pct, suppressed, suppressed_at "
        "FROM combo_performance "
        "WHERE window = ? AND trades >= ? "
        "ORDER BY win_rate_pct DESC, trades DESC, combo_key ASC",
        (window, min_trades),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def audit_missed_winners(
    db: Database,
    start: datetime,
    end: datetime,
    settings: Settings,
) -> dict:
    """CG winners we did not paper-trade. LEFT JOIN per spec §7."""
    min_pct = settings.FEEDBACK_MISSED_WINNER_MIN_PCT
    min_mcap = settings.FEEDBACK_MISSED_WINNER_MIN_MCAP
    catch_min = settings.FEEDBACK_MISSED_WINNER_WINDOW_MIN

    start_iso, end_iso = start.isoformat(), end.isoformat()

    # Denominator slice: winners regardless of mcap filter (for warning only)
    cur = await db._conn.execute(
        "SELECT COUNT(DISTINCT coin_id) FROM gainers_snapshots "
        "WHERE snapshot_at BETWEEN ? AND ? AND price_change_24h >= ?",
        (start_iso, end_iso, min_pct),
    )
    winners_total_unfiltered = (await cur.fetchone())[0] or 0

    # Filter boundary for denominator: count coins removed by mcap floor
    cur = await db._conn.execute(
        "SELECT coin_id, MAX(market_cap) AS m FROM gainers_snapshots "
        "WHERE snapshot_at BETWEEN ? AND ? AND price_change_24h >= ? "
        "GROUP BY coin_id",
        (start_iso, end_iso, min_pct),
    )
    rows = await cur.fetchall()
    filtered_by_mcap = sum(1 for r in rows if (r["m"] or 0) < min_mcap)

    # Main missed-winner query — LEFT JOIN per spec §7.
    # crossed_at = MIN(snapshot_at) so the catch-window aligns with
    # the FIRST moment this coin crossed the winner threshold.
    cur = await db._conn.execute(
        """
        WITH winners AS (
            SELECT coin_id,
                   MIN(symbol) AS symbol,
                   MIN(name)   AS name,
                   MIN(snapshot_at) AS crossed_at,
                   MAX(price_change_24h) AS peak_change,
                   MAX(market_cap) AS mcap
            FROM gainers_snapshots
            WHERE snapshot_at BETWEEN ? AND ?
              AND price_change_24h >= ?
            GROUP BY coin_id
            HAVING mcap >= ?
        )
        SELECT w.coin_id, w.symbol, w.name, w.crossed_at, w.peak_change,
               w.mcap,
               CASE
                 WHEN w.peak_change >= 1000 THEN 'disaster_miss'
                 WHEN w.peak_change >= 200  THEN 'major_miss'
                 ELSE 'partial_miss'
               END AS tier
        FROM winners w
        LEFT JOIN paper_trades pt
               ON pt.token_id = w.coin_id
              AND strftime('%Y-%m-%dT%H:%M:%f', pt.opened_at)
                  BETWEEN strftime('%Y-%m-%dT%H:%M:%f', w.crossed_at, ?)
                      AND strftime('%Y-%m-%dT%H:%M:%f', w.crossed_at, ?)
        WHERE pt.id IS NULL
        """,
        (
            start_iso,
            end_iso,
            min_pct,
            min_mcap,
            f"-{catch_min} minutes",
            f"+{catch_min} minutes",
        ),
    )
    missed_rows = await cur.fetchall()

    # Qualifying-winners total (post mcap filter) used for caught count
    cur = await db._conn.execute(
        """SELECT COUNT(*) FROM (
             SELECT coin_id
             FROM gainers_snapshots
             WHERE snapshot_at BETWEEN ? AND ? AND price_change_24h >= ?
             GROUP BY coin_id
             HAVING MAX(market_cap) >= ?
        )""",
        (start_iso, end_iso, min_pct, min_mcap),
    )
    winners_qualifying = (await cur.fetchone())[0] or 0
    winners_missed = len(missed_rows)
    winners_caught = winners_qualifying - winners_missed

    # Pipeline-gap partitioning
    gaps = await detect_pipeline_gaps(
        db, start, end, settings.FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN
    )
    gap_ranges = [
        (datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in gaps
    ]

    tiers = {"partial_miss": [], "major_miss": [], "disaster_miss": []}
    uncovered_window: list[dict] = []
    for r in missed_rows:
        row_dict = dict(r)
        try:
            crossed_dt = datetime.fromisoformat(row_dict["crossed_at"])
        except ValueError:
            log.warning(
                "audit_crossed_at_parse_error",
                coin_id=row_dict.get("coin_id"),
                raw=row_dict.get("crossed_at"),
            )
            continue
        if crossed_dt.tzinfo is None:
            crossed_dt = crossed_dt.replace(tzinfo=timezone.utc)
        is_uncovered = any(a <= crossed_dt <= b for a, b in gap_ranges)
        if is_uncovered:
            uncovered_window.append(row_dict)
        else:
            tiers[row_dict["tier"]].append(row_dict)

    if winners_qualifying == 0:
        log.warning(
            "audit_query_empty_warning",
            start=start_iso,
            end=end_iso,
            unfiltered=winners_total_unfiltered,
        )

    pipeline_gap_hours = sum((b - a).total_seconds() / 3600.0 for a, b in gap_ranges)

    return {
        "tiers": tiers,
        "uncovered_window": uncovered_window,
        "denominator": {
            "winners_total": winners_qualifying,
            "winners_caught": winners_caught,
            "winners_missed": winners_missed,
            "winners_filtered_by_mcap": filtered_by_mcap,
            "pipeline_gap_hours": round(pipeline_gap_hours, 2),
        },
    }


async def lead_time_breakdown(db: Database, window: str) -> dict[str, dict]:
    """Per-signal-type lead-time stats. Percentiles in Python per D21."""
    days = 7 if window == "7d" else 30
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cur = await db._conn.execute(
        "SELECT signal_type, lead_time_vs_trending_min, lead_time_vs_trending_status "
        "FROM paper_trades WHERE opened_at >= ?",
        (cutoff,),
    )
    rows = await cur.fetchall()
    groups: dict[str, dict] = {}
    for r in rows:
        sig = r["signal_type"]
        bucket = groups.setdefault(sig, {"ok": [], "no_reference": 0, "error": 0})
        status = r["lead_time_vs_trending_status"]
        if status == "ok" and r["lead_time_vs_trending_min"] is not None:
            bucket["ok"].append(float(r["lead_time_vs_trending_min"]))
        elif status == "no_reference":
            bucket["no_reference"] += 1
        elif status == "error":
            bucket["error"] += 1

    result: dict[str, dict] = {}
    for sig, bucket in groups.items():
        values = sorted(bucket["ok"])
        n = len(values)
        if n == 0:
            median = p25 = p75 = None
        else:
            median = values[n // 2]
            p25 = values[max(n // 4, 0)]
            p75 = values[min((3 * n) // 4, n - 1)]
        result[sig] = {
            "median_min": median,
            "p25_min": p25,
            "p75_min": p75,
            "count_ok": n,
            "count_no_reference": bucket["no_reference"],
            "count_error": bucket["error"],
        }
    return result


async def suppression_log(db: Database, start: datetime, end: datetime) -> list[dict]:
    cur = await db._conn.execute(
        "SELECT combo_key, suppressed_at, parole_at, parole_trades_remaining, "
        "       win_rate_pct, trades "
        "FROM combo_performance "
        "WHERE window = '30d' "
        "  AND suppressed_at IS NOT NULL "
        "  AND suppressed_at BETWEEN ? AND ? "
        "ORDER BY suppressed_at DESC",
        (start.isoformat(), end.isoformat()),
    )
    return [dict(r) for r in await cur.fetchall()]


async def detect_pipeline_gaps(
    db: Database, start: datetime, end: datetime, max_gap_minutes: int = 60
) -> list[tuple[str, str]]:
    cur = await db._conn.execute(
        "SELECT DISTINCT snapshot_at FROM gainers_snapshots "
        "WHERE snapshot_at BETWEEN ? AND ? "
        "ORDER BY snapshot_at ASC",
        (start.isoformat(), end.isoformat()),
    )
    rows = await cur.fetchall()
    gaps: list[tuple[str, str]] = []
    prev = None
    for r in rows:
        try:
            cur_ts = datetime.fromisoformat(r[0])
        except ValueError:
            log.warning("pipeline_gap_parse_error", raw=r[0])
            continue
        if cur_ts.tzinfo is None:
            cur_ts = cur_ts.replace(tzinfo=timezone.utc)
        if prev is not None:
            delta_min = (cur_ts - prev).total_seconds() / 60.0
            if delta_min > max_gap_minutes:
                gaps.append((prev.isoformat(), cur_ts.isoformat()))
        prev = cur_ts
    return gaps
