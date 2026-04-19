"""Nightly combo refresh (spec §5.3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from scout.db import Database

log = structlog.get_logger()


async def refresh_combo(db: Database, combo_key: str, settings) -> bool:
    """Recompute 7d + 30d rows for `combo_key`. Apply suppression rule to 30d.
    Returns True on success, False otherwise.
    """
    try:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        stats = {}
        for window, days in (("7d", 7), ("30d", 30)):
            cur = await db._conn.execute(
                """SELECT
                     COUNT(*) AS trades,
                     SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                     SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
                     COALESCE(SUM(pnl_usd), 0) AS total_pnl_usd,
                     COALESCE(AVG(pnl_pct), 0) AS avg_pnl_pct
                   FROM paper_trades
                   WHERE signal_combo = ?
                     AND status != 'open'
                     AND closed_at >= ?""",
                (combo_key, (now - timedelta(days=days)).isoformat()),
            )
            row = await cur.fetchone()
            trades = row["trades"] or 0
            wins = row["wins"] or 0
            losses = row["losses"] or 0
            total_pnl = float(row["total_pnl_usd"] or 0)
            avg_pct = float(row["avg_pnl_pct"] or 0)
            wr = (100.0 * wins / trades) if trades else 0.0
            stats[window] = dict(
                trades=trades,
                wins=wins,
                losses=losses,
                total_pnl=total_pnl,
                avg_pct=avg_pct,
                wr=wr,
            )

        # 7d row: plain UPSERT.
        w7 = stats["7d"]
        await db._conn.execute(
            "INSERT INTO combo_performance "
            "(combo_key, window, trades, wins, losses, total_pnl_usd, "
            " avg_pnl_pct, win_rate_pct, suppressed, last_refreshed) "
            "VALUES (?, '7d', ?, ?, ?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(combo_key, window) DO UPDATE SET "
            " trades=excluded.trades, wins=excluded.wins, losses=excluded.losses, "
            " total_pnl_usd=excluded.total_pnl_usd, avg_pnl_pct=excluded.avg_pnl_pct, "
            " win_rate_pct=excluded.win_rate_pct, last_refreshed=excluded.last_refreshed, "
            " refresh_failures=0",
            (
                combo_key,
                w7["trades"],
                w7["wins"],
                w7["losses"],
                w7["total_pnl"],
                w7["avg_pct"],
                w7["wr"],
                now_iso,
            ),
        )

        # 30d row: apply suppression rule.
        w30 = stats["30d"]
        cur = await db._conn.execute(
            "SELECT suppressed, parole_trades_remaining, suppressed_at "
            "FROM combo_performance WHERE combo_key = ? AND window = '30d'",
            (combo_key,),
        )
        existing = await cur.fetchone()

        min_trades = settings.FEEDBACK_SUPPRESSION_MIN_TRADES
        wr_thresh = settings.FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT
        parole_days = settings.FEEDBACK_PAROLE_DAYS
        retest = settings.FEEDBACK_PAROLE_RETEST_TRADES

        new_suppressed = 0
        new_suppressed_at = None
        new_parole_at = None
        new_parole_remaining = None

        if existing is None:
            # First write — maybe suppress immediately if bad enough.
            if w30["trades"] >= min_trades and w30["wr"] < wr_thresh:
                new_suppressed = 1
                new_suppressed_at = now_iso
                new_parole_at = (now + timedelta(days=parole_days)).isoformat()
                new_parole_remaining = retest
        else:
            was_suppressed = bool(existing["suppressed"])
            remaining = existing["parole_trades_remaining"]
            if not was_suppressed:
                if w30["trades"] >= min_trades and w30["wr"] < wr_thresh:
                    new_suppressed = 1
                    new_suppressed_at = now_iso
                    new_parole_at = (now + timedelta(days=parole_days)).isoformat()
                    new_parole_remaining = retest
            else:
                if remaining is not None and remaining <= 0:
                    if w30["wr"] >= wr_thresh:
                        new_suppressed = 0
                        new_suppressed_at = None
                        new_parole_at = None
                        new_parole_remaining = None
                    else:
                        new_suppressed = 1
                        new_suppressed_at = now_iso
                        new_parole_at = (now + timedelta(days=parole_days)).isoformat()
                        new_parole_remaining = retest
                else:
                    # Still serving suppression/parole — preserve existing state.
                    new_suppressed = 1
                    new_suppressed_at = existing["suppressed_at"]
                    cur2 = await db._conn.execute(
                        "SELECT parole_at FROM combo_performance "
                        "WHERE combo_key = ? AND window = '30d'",
                        (combo_key,),
                    )
                    new_parole_at = (await cur2.fetchone())[0]
                    new_parole_remaining = remaining

        await db._conn.execute(
            "INSERT INTO combo_performance "
            "(combo_key, window, trades, wins, losses, total_pnl_usd, "
            " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, parole_at, "
            " parole_trades_remaining, refresh_failures, last_refreshed) "
            "VALUES (?, '30d', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(combo_key, window) DO UPDATE SET "
            " trades=excluded.trades, wins=excluded.wins, losses=excluded.losses, "
            " total_pnl_usd=excluded.total_pnl_usd, avg_pnl_pct=excluded.avg_pnl_pct, "
            " win_rate_pct=excluded.win_rate_pct, "
            " suppressed=excluded.suppressed, suppressed_at=excluded.suppressed_at, "
            " parole_at=excluded.parole_at, "
            " parole_trades_remaining=excluded.parole_trades_remaining, "
            " refresh_failures=0, last_refreshed=excluded.last_refreshed",
            (
                combo_key,
                w30["trades"],
                w30["wins"],
                w30["losses"],
                w30["total_pnl"],
                w30["avg_pct"],
                w30["wr"],
                new_suppressed,
                new_suppressed_at,
                new_parole_at,
                new_parole_remaining,
                now_iso,
            ),
        )
        await db._conn.commit()
        return True
    except Exception as e:
        log.error(
            "combo_refresh_error",
            combo_key=combo_key,
            err=str(e),
            err_id="COMBO_REFRESH",
        )
        try:
            await db._conn.execute(
                "UPDATE combo_performance SET refresh_failures = refresh_failures + 1 "
                "WHERE combo_key = ?",
                (combo_key,),
            )
            await db._conn.commit()
        except Exception as counter_err:
            # The chronic-failure surfacing in weekly_digest depends on this
            # counter incrementing — if the counter write itself fails, log
            # loudly so the operator notices the counter is blind, not silent.
            log.exception(
                "combo_refresh_failure_counter_update_failed",
                combo_key=combo_key,
                err=str(counter_err),
                err_id="COMBO_REFRESH_COUNTER",
            )
        return False


async def refresh_all(db: Database, settings) -> dict:
    """Rebuild `combo_performance` for every combo seen in last 30d.

    Returns {"refreshed": N, "failed": M, "chronic_failures": [keys]}.
    """
    cur = await db._conn.execute(
        "SELECT DISTINCT signal_combo FROM paper_trades "
        "WHERE signal_combo IS NOT NULL "
        "  AND opened_at >= datetime('now', '-30 days')"
    )
    rows = await cur.fetchall()
    combos = [r[0] for r in rows if r[0]]

    refreshed = 0
    failed = 0
    for combo in combos:
        ok = await refresh_combo(db, combo, settings)
        if ok:
            refreshed += 1
        else:
            failed += 1

    cur = await db._conn.execute(
        "SELECT combo_key FROM combo_performance " "WHERE refresh_failures >= ?",
        (settings.FEEDBACK_CHRONIC_FAILURE_THRESHOLD,),
    )
    chronic = [r[0] for r in await cur.fetchall()]
    for key in chronic:
        log.warning(
            "combo_refresh_chronic_failure",
            combo_key=key,
        )

    log.info(
        "combo_refresh_summary",
        refreshed=refreshed,
        failed=failed,
        chronic=len(chronic),
    )
    return {"refreshed": refreshed, "failed": failed, "chronic_failures": chronic}
