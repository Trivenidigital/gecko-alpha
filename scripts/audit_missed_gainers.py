#!/usr/bin/env python3
"""Reproducible audit of the Top Gainers Tracker recall decomposition.

Quantifies, with datetime()-NORMALIZED comparisons, how the current is_gap=1
"missed" gainers decompose:
  (a) false gaps already detected early by an EXISTING surface but dropped by the
      isoformat-T-vs-space-format timestamp-comparison bug (now fixed),
  (b) additionally recoverable by the wired acceleration / momentum / slow_burn /
      velocity surfaces,
  (c) true residual coverage gaps (genuinely unobserved pre-pump).

Makes the "caught 575 -> ~620, hit-rate 88.2% -> ~95%" metric-shift claim
re-checkable from the repo instead of only via a live recompute. Read-only;
uses synchronous sqlite3 so it runs anywhere (no aiohttp/async deps). Degrades
gracefully when a surface table is absent (e.g. run on prod before the migration
deploys -> the new surfaces report null and are excluded from the union).

Usage:
    uv run python scripts/audit_missed_gainers.py --db scout.db [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3

_TOL = "datetime(g.appeared_on_gainers_at,'+5 minutes')"

# name -> (table, EXISTS-predicate)
EXISTING = {
    "spikes": (
        "volume_spikes",
        f"EXISTS(SELECT 1 FROM volume_spikes v WHERE v.coin_id=g.coin_id "
        f"AND datetime(v.detected_at)<{_TOL})",
    ),
    "narrative": (
        "predictions",
        f"EXISTS(SELECT 1 FROM predictions p WHERE (p.coin_id=g.coin_id "
        f"OR LOWER(p.symbol)=LOWER(g.symbol)) AND datetime(p.predicted_at)<{_TOL})",
    ),
    "pipeline": (
        "candidates",
        f"EXISTS(SELECT 1 FROM candidates c WHERE (c.contract_address=g.coin_id "
        f"OR LOWER(c.ticker)=LOWER(g.symbol)) AND datetime(c.first_seen_at)<{_TOL})",
    ),
    "chains": (
        "signal_events",
        f"EXISTS(SELECT 1 FROM signal_events e WHERE e.token_id=g.coin_id "
        f"AND datetime(e.created_at)<{_TOL})",
    ),
}
NEW = {
    "acceleration": (
        "gainer_acceleration",
        f"EXISTS(SELECT 1 FROM gainer_acceleration a WHERE a.coin_id=g.coin_id "
        f"AND datetime(a.detected_at)<{_TOL})",
    ),
    "momentum": (
        "momentum_7d",
        f"EXISTS(SELECT 1 FROM momentum_7d m WHERE m.coin_id=g.coin_id "
        f"AND datetime(m.detected_at)<{_TOL})",
    ),
    "slow_burn": (
        "slow_burn_candidates",
        f"EXISTS(SELECT 1 FROM slow_burn_candidates s WHERE s.coin_id=g.coin_id "
        f"AND datetime(s.detected_at)<{_TOL})",
    ),
    "velocity": (
        "velocity_alerts",
        f"EXISTS(SELECT 1 FROM velocity_alerts vl WHERE vl.coin_id=g.coin_id "
        f"AND datetime(vl.detected_at)<{_TOL})",
    ),
}


def _scalar(cur: sqlite3.Cursor, sql: str) -> int:
    cur.execute(sql)
    return int(cur.fetchone()[0])


def _table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def _gap_count(cur: sqlite3.Cursor, predicate: str) -> int:
    return _scalar(
        cur,
        "SELECT COUNT(DISTINCT g.coin_id) FROM gainers_comparisons g "
        f"WHERE g.is_gap=1 AND {predicate}",
    )


def audit(db_path: str) -> dict:
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        total = _scalar(cur, "SELECT COUNT(*) FROM gainers_comparisons")
        gaps = _scalar(cur, "SELECT COUNT(*) FROM gainers_comparisons WHERE is_gap=1")
        caught_now = total - gaps

        absent: list[str] = []
        fg: dict[str, int | None] = {}
        existing_preds: list[str] = []
        for name, (table, pred) in EXISTING.items():
            if _table_exists(cur, table):
                fg[name] = _gap_count(cur, pred)
                existing_preds.append(pred)
            else:
                fg[name] = None
                absent.append(table)

        gc: dict[str, int | None] = {}
        new_preds: list[str] = []
        for name, (table, pred) in NEW.items():
            if _table_exists(cur, table):
                gc[name] = _gap_count(cur, pred)
                new_preds.append(pred)
            else:
                gc[name] = None
                absent.append(table)

        rec_existing = (
            _gap_count(cur, "(" + " OR ".join(existing_preds) + ")")
            if existing_preds
            else 0
        )
        all_preds = existing_preds + new_preds
        rec_all = (
            _gap_count(cur, "(" + " OR ".join(all_preds) + ")") if all_preds else 0
        )
        residual = gaps - rec_all
        corrected_caught = caught_now + rec_all

        def pct(n: int) -> float:
            return round(n / total * 100, 1) if total else 0.0

        return {
            "total_tracked": total,
            "caught_now": caught_now,
            "gaps_now": gaps,
            "hit_rate_now_pct": pct(caught_now),
            "false_gaps_per_existing_surface": fg,
            "recoverable_existing_surfaces": rec_existing,
            "gap_catch_per_new_surface": gc,
            "recoverable_all_surfaces": rec_all,
            "true_residual_gaps": residual,
            "corrected_caught": corrected_caught,
            "corrected_hit_rate_pct": pct(corrected_caught),
            "absent_tables": sorted(set(absent)),
        }
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="scout.db", help="path to scout.db")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    out = audit(args.db)
    if args.json:
        print(json.dumps(out, indent=2))
        return
    print("Top Gainers Tracker recall audit (datetime-normalized)")
    print(
        f"  tracked={out['total_tracked']} caught_now={out['caught_now']} "
        f"gaps_now={out['gaps_now']} hit_rate_now={out['hit_rate_now_pct']}%"
    )
    print(f"  false gaps by existing surface: {out['false_gaps_per_existing_surface']}")
    print(
        f"  recoverable by EXISTING surfaces (timestamp-fix): "
        f"{out['recoverable_existing_surfaces']}"
    )
    print(f"  gap-catch by NEW surface: {out['gap_catch_per_new_surface']}")
    print(
        f"  recoverable by ALL surfaces: {out['recoverable_all_surfaces']}  "
        f"true residual: {out['true_residual_gaps']}"
    )
    print(
        f"  corrected: caught {out['caught_now']}->{out['corrected_caught']}, "
        f"hit_rate {out['hit_rate_now_pct']}%->{out['corrected_hit_rate_pct']}%"
    )
    if out["absent_tables"]:
        print(f"  (absent tables, excluded: {out['absent_tables']})")


if __name__ == "__main__":
    main()
