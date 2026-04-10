"""Backtest CLI — analyze historical predictions and alerts.

Usage:
    uv run python -m scout.backtest
    uv run python -m scout.backtest --days 30
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from scout.db import Database


async def run_backtest(db_path: str, days: int) -> None:
    db = Database(db_path)
    await db.initialize()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")

    print(f"\n=== Backtest Analysis (last {days} days) ===\n")

    # --- Narrative agent stats --------------------------------------------
    async with conn.execute(
        """SELECT
             COUNT(*) as total,
             SUM(CASE WHEN outcome_class='HIT' AND is_control=0 THEN 1 ELSE 0 END) as agent_hits,
             SUM(CASE WHEN is_control=0 AND outcome_class IS NOT NULL AND outcome_class != 'UNRESOLVED' THEN 1 ELSE 0 END) as agent_eval,
             SUM(CASE WHEN outcome_class='HIT' AND is_control=1 THEN 1 ELSE 0 END) as ctrl_hits,
             SUM(CASE WHEN is_control=1 AND outcome_class IS NOT NULL AND outcome_class != 'UNRESOLVED' THEN 1 ELSE 0 END) as ctrl_eval
           FROM predictions WHERE predicted_at > ?""",
        (cutoff,),
    ) as cur:
        row = await cur.fetchone()

    if row:
        agent_eval = row["agent_eval"] or 0
        ctrl_eval = row["ctrl_eval"] or 0
        agent_hits = row["agent_hits"] or 0
        ctrl_hits = row["ctrl_hits"] or 0
        agent_rate = (agent_hits / agent_eval * 100) if agent_eval else 0.0
        ctrl_rate = (ctrl_hits / ctrl_eval * 100) if ctrl_eval else 0.0
        print(f"Narrative Agent Predictions: {row['total'] or 0}")
        print(f"  Agent hit rate: {agent_rate:.1f}% ({agent_hits}/{agent_eval})")
        print(f"  Control hit rate: {ctrl_rate:.1f}% ({ctrl_hits}/{ctrl_eval})")
        print(f"  TRUE ALPHA: {agent_rate - ctrl_rate:+.1f}pp")

    # --- Hit rate by category ---------------------------------------------
    async with conn.execute(
        """SELECT category_name, COUNT(*) as total,
              SUM(CASE WHEN outcome_class='HIT' THEN 1 ELSE 0 END) as hits
           FROM predictions
           WHERE is_control=0 AND predicted_at > ?
                 AND outcome_class IS NOT NULL AND outcome_class != 'UNRESOLVED'
           GROUP BY category_name
           ORDER BY total DESC""",
        (cutoff,),
    ) as cur:
        cat_rows = await cur.fetchall()
    if cat_rows:
        print("\nHit rate by category:")
        for r in cat_rows:
            if r["total"]:
                rate = (r["hits"] or 0) / r["total"] * 100
                print(f"  {r['category_name']}: {rate:.0f}% ({r['hits']}/{r['total']})")

    # --- Hit rate by market regime ----------------------------------------
    async with conn.execute(
        """SELECT market_regime, COUNT(*) as total,
              SUM(CASE WHEN outcome_class='HIT' THEN 1 ELSE 0 END) as hits
           FROM predictions
           WHERE is_control=0 AND predicted_at > ?
                 AND outcome_class IS NOT NULL AND outcome_class != 'UNRESOLVED'
           GROUP BY market_regime""",
        (cutoff,),
    ) as cur:
        regime_rows = await cur.fetchall()
    if regime_rows:
        print("\nHit rate by market regime:")
        for r in regime_rows:
            if r["total"]:
                rate = (r["hits"] or 0) / r["total"] * 100
                print(f"  {r['market_regime']}: {rate:.0f}% ({r['hits']}/{r['total']})")

    # --- Counter-score correlation ----------------------------------------
    async with conn.execute(
        """SELECT
             CASE
               WHEN counter_risk_score < 30 THEN 'low'
               WHEN counter_risk_score < 60 THEN 'mid'
               ELSE 'high'
             END as risk_band,
             COUNT(*) as total,
             SUM(CASE WHEN outcome_class='HIT' THEN 1 ELSE 0 END) as hits
           FROM predictions
           WHERE counter_risk_score IS NOT NULL AND is_control=0
                 AND outcome_class IS NOT NULL AND outcome_class != 'UNRESOLVED'
                 AND predicted_at > ?
           GROUP BY risk_band""",
        (cutoff,),
    ) as cur:
        risk_rows = await cur.fetchall()
    if risk_rows:
        print("\nCounter-score correlation:")
        for r in risk_rows:
            if r["total"]:
                rate = (r["hits"] or 0) / r["total"] * 100
                print(f"  {r['risk_band']} risk: {rate:.0f}% hit rate ({r['hits']}/{r['total']})")

    # --- Existing pipeline alerts -----------------------------------------
    try:
        async with conn.execute(
            "SELECT COUNT(*) as total FROM alerts WHERE alerted_at > ?",
            (cutoff,),
        ) as cur:
            row = await cur.fetchone()
        if row:
            print(f"\nExisting Pipeline Alerts: {row['total'] or 0}")
    except Exception as e:
        print(f"  (error querying alerts: {e})")

    # --- Alert-to-outcome analysis (existing pipeline) --------------------
    try:
        async with conn.execute(
            """SELECT outcome_class, COUNT(*) as total
               FROM outcomes WHERE recorded_at > ?
               GROUP BY outcome_class""",
            (cutoff,),
        ) as cur:
            outcome_rows = await cur.fetchall()
        if outcome_rows:
            print("\nAlert outcome distribution:")
            for r in outcome_rows:
                print(f"  {r['outcome_class']}: {r['total']}")
    except Exception as e:
        print(f"  (error querying outcomes: {e})")

    # --- Second-wave candidates -------------------------------------------
    try:
        async with conn.execute(
            "SELECT COUNT(*) as total FROM second_wave_candidates WHERE detected_at > ?",
            (cutoff,),
        ) as cur:
            row = await cur.fetchone()
        if row and row["total"]:
            print(f"\nSecond-Wave Detections: {row['total']}")
    except Exception as e:
        print(f"  (error querying second_wave_candidates: {e})")

    # --- Conviction chains ------------------------------------------------
    try:
        async with conn.execute(
            """SELECT pattern_id, COUNT(*) as total
               FROM chain_matches WHERE completed_at > ?
               GROUP BY pattern_id""",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
        if rows:
            print("\nConviction Chains:")
            for r in rows:
                print(f"  {r['pattern_id']}: {r['total']}")
    except Exception as e:
        print(f"  (error querying chain_matches: {e})")

    await db.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest analysis CLI")
    parser.add_argument("--days", type=int, default=30, help="Days to analyze")
    parser.add_argument("--db", default="scout.db", help="Path to SQLite DB")
    args = parser.parse_args()

    await run_backtest(args.db, args.days)


if __name__ == "__main__":
    asyncio.run(main())
