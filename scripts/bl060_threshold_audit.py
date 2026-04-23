#!/usr/bin/env python3
"""
BL-060 threshold calibration audit.

Reads the last 7 days of first_signal paper trades, prints a quant_score
histogram, and for each candidate threshold T prints:
  - a steady-state ratio projection (current_concurrent * admits_T / admits_0)
  - a direct current-open survival count at that threshold

Usage:
    uv run python scripts/bl060_threshold_audit.py [--db path/to/gecko.db]
"""

import argparse
import asyncio
import sys
from collections import Counter

import aiosqlite


WINDOW_DAYS = 7
THRESHOLDS = [10, 20, 25, 30, 35, 40, 45, 50, 60]


async def run(db_path: str) -> int:
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            """
            SELECT
              json_extract(signal_data, '$.quant_score') AS qscore,
              status,
              opened_at
            FROM paper_trades
            WHERE signal_type = 'first_signal'
              AND opened_at >= datetime('now', ?)
            """,
            (f"-{WINDOW_DAYS} days",),
        )
        rows = await cur.fetchall()

        cur2 = await conn.execute(
            "SELECT COUNT(*) FROM paper_trades "
            "WHERE signal_type='first_signal' AND status='open'"
        )
        current_concurrent_row = await cur2.fetchone()
        current_concurrent = current_concurrent_row[0] if current_concurrent_row else 0

    if not rows:
        print("No first_signal trades in window. Cannot calibrate threshold.")
        return 2

    scores = [r[0] for r in rows if r[0] is not None]
    if not scores:
        print("All rows have NULL quant_score in signal_data. Cannot calibrate.")
        return 2

    bucket: Counter[int] = Counter()
    for s in scores:
        b = int(s) // 10 * 10
        bucket[b] += 1

    print(f"# BL-060 threshold audit - last {WINDOW_DAYS} days")
    print(f"Total first_signal admits: {len(rows)}")
    print(f"Current concurrent open: {current_concurrent}")
    print()
    print("Score histogram (10-pt buckets):")
    for key in sorted(bucket):
        print(f"  {key:3d}-{key+9:3d}: {bucket[key]:4d}  {'#' * bucket[key]}")
    print()

    admits_at_zero = len(scores)
    print("Projection per threshold:")
    print("  (ratio = steady-state; direct = current-open survival)")
    for t in THRESHOLDS:
        admits_at_t = sum(1 for s in scores if s >= t)
        ratio = admits_at_t / admits_at_zero if admits_at_zero > 0 else 0.0
        steady_state = int(current_concurrent * ratio)

        async with aiosqlite.connect(db_path) as conn:
            c = await conn.execute(
                "SELECT COUNT(*) FROM paper_trades "
                "WHERE signal_type='first_signal' AND status='open' "
                "AND json_extract(signal_data, '$.quant_score') >= ?",
                (t,),
            )
            direct_row = await c.fetchone()
            direct = direct_row[0] if direct_row else 0

        print(f"  T={t:2d} -> {steady_state:3d} projected steady-state (ratio)")
        print(f"  T={t:2d} -> {direct:3d} current open survives (direct)")

    print()
    print("Caveats:")
    print(
        "- Projection assumes trade-duration distribution is independent of "
        "quant_score."
    )
    print(
        f"- {WINDOW_DAYS}-day window chosen because it post-dates the BL-059 "
        "junk-filter deploy (2026-04-22); longer windows mix regimes."
    )
    print(
        "- Script does not predict would_be_live=1 stamp rate (depends on "
        "arrival ordering, not threshold)."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/gecko.db")
    args = parser.parse_args()
    return asyncio.run(run(args.db))


if __name__ == "__main__":
    sys.exit(main())
