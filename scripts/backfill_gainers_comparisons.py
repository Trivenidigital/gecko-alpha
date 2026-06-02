#!/usr/bin/env python3
"""One-time backfill: recompute is_gap on the HISTORICAL gainers_comparisons.

The live `compare_gainers_with_signals` only reprocesses the last-24h gainers
(and `gainers_snapshots` is pruned at 7 days), so the historical comparison rows
were frozen with the pre-fix isoformat-T-vs-space-format timestamp bug that
silently dropped same-day early detections. This recomputes every stored
comparison row against the surface tables using the SAME datetime()-normalized
predicates as the fixed tracker (and the audit script), crediting the
acceleration/momentum/slow_burn/velocity surfaces too.

Only ADDS credit (the fix never removes a detection): sets detected_by_* + the
lead column where a surface saw the coin before appeared_on_gainers_at + 5min,
then flips is_gap to 0 for any newly-credited row. Preserves detected_price /
peak_price / peak_gain_pct. Reversible: --apply first copies the table to a
timestamped backup.

Usage:
    python scripts/backfill_gainers_comparisons.py --db scout.db          # dry-run
    python scripts/backfill_gainers_comparisons.py --db scout.db --apply  # execute
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sqlite3

_BOUND = "datetime(gainers_comparisons.appeared_on_gainers_at,'+5 minutes')"

# (flag_col, lead_col, table, alias, match-expr, ts-col)
SURFACES = [
    ("detected_by_narrative", "narrative_lead_minutes", "predictions", "p",
     "(p.coin_id=gainers_comparisons.coin_id OR LOWER(p.symbol)=LOWER(gainers_comparisons.symbol))",
     "p.predicted_at"),
    ("detected_by_pipeline", "pipeline_lead_minutes", "candidates", "c",
     "(c.contract_address=gainers_comparisons.coin_id OR LOWER(c.ticker)=LOWER(gainers_comparisons.symbol))",
     "c.first_seen_at"),
    ("detected_by_chains", "chains_lead_minutes", "signal_events", "e",
     "(e.token_id=gainers_comparisons.coin_id)", "e.created_at"),
    ("detected_by_spikes", "spikes_lead_minutes", "volume_spikes", "v",
     "(v.coin_id=gainers_comparisons.coin_id)", "v.detected_at"),
    ("detected_by_acceleration", "acceleration_lead_minutes", "gainer_acceleration", "a",
     "(a.coin_id=gainers_comparisons.coin_id)", "a.detected_at"),
    ("detected_by_momentum", "momentum_lead_minutes", "momentum_7d", "m",
     "(m.coin_id=gainers_comparisons.coin_id)", "m.detected_at"),
    ("detected_by_slow_burn", "slow_burn_lead_minutes", "slow_burn_candidates", "s",
     "(s.coin_id=gainers_comparisons.coin_id)", "s.detected_at"),
    ("detected_by_velocity", "velocity_lead_minutes", "velocity_alerts", "vl",
     "(vl.coin_id=gainers_comparisons.coin_id)", "vl.detected_at"),
]

_FLAGS = [s[0] for s in SURFACES]


def _exists(flag, lead, table, alias, match, ts):
    return (
        f"EXISTS(SELECT 1 FROM {table} {alias} WHERE {match} "
        f"AND datetime({ts}) < {_BOUND})"
    )


def _update_sql(flag, lead, table, alias, match, ts):
    sub = (
        f"SELECT MIN({ts}) FROM {table} {alias} WHERE {match} "
        f"AND datetime({ts}) < {_BOUND}"
    )
    return (
        f"UPDATE gainers_comparisons SET {flag}=1, "
        f"{lead}=MAX(0,(julianday(gainers_comparisons.appeared_on_gainers_at)"
        f"-julianday(({sub})))*1440.0) "
        f"WHERE {_exists(flag, lead, table, alias, match, ts)}"
    )


def _table_exists(cur, table):
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def _stats(cur):
    cur.execute("SELECT COUNT(*) FROM gainers_comparisons")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM gainers_comparisons WHERE is_gap=0")
    caught = cur.fetchone()[0]
    return total, caught, total - caught


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="scout.db")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    surfaces = [s for s in SURFACES if _table_exists(cur, s[2])]
    absent = [s[2] for s in SURFACES if not _table_exists(cur, s[2])]

    total, caught_before, gaps_before = _stats(cur)
    union = " OR ".join(_exists(*s) for s in surfaces)
    cur.execute(
        f"SELECT COUNT(*) FROM gainers_comparisons WHERE is_gap=1 AND ({union})"
    )
    would_recover = cur.fetchone()[0]

    print(f"db={args.db} apply={args.apply}")
    print(f"  before: total={total} caught={caught_before} gaps={gaps_before} "
          f"hit_rate={round(caught_before/total*100,1) if total else 0}%")
    print(f"  would recover (is_gap 1->0): {would_recover}")
    print(f"  projected: caught={caught_before + would_recover} "
          f"gaps={gaps_before - would_recover} "
          f"hit_rate={round((caught_before+would_recover)/total*100,1) if total else 0}%")
    if absent:
        print(f"  (absent surface tables, skipped: {absent})")

    if not args.apply:
        print("  DRY-RUN: no changes written. Re-run with --apply to execute.")
        con.close()
        return

    bak = "gainers_comparisons_bak_" + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    try:
        cur.execute("BEGIN")
        cur.execute(f"CREATE TABLE {bak} AS SELECT * FROM gainers_comparisons")
        for s in surfaces:
            cur.execute(_update_sql(*s))
        cur.execute(
            "UPDATE gainers_comparisons SET is_gap=0 WHERE ("
            + " OR ".join(f"{f}=1" for f in _FLAGS)
            + ")"
        )
        con.commit()
    except Exception:
        con.rollback()
        raise

    total, caught_after, gaps_after = _stats(cur)
    print(f"  APPLIED. backup table: {bak}")
    print(f"  after: total={total} caught={caught_after} gaps={gaps_after} "
          f"hit_rate={round(caught_after/total*100,1) if total else 0}%")
    con.close()


if __name__ == "__main__":
    main()
