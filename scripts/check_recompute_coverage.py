#!/usr/bin/env python3
"""Exit nonzero when the legacy-provenance overlay is recovering NO credit.

§12a: a pipeline table ships with a freshness SLO and a watchdog in the same
PR. `chain_identity_recompute_v1` is worse than the usual case -- it has no
runtime writer at all, so "never populated" is a normal operator error rather
than an exotic failure, and the consequence is invisible: every pre-cutover
chains detection quietly loses its credit and tier_high collapses under green
logs.

The condition checked is RECOVERED CREDIT, not row count. An overlay can be
fully populated and recover nothing -- only `verified_canonical` earns credit,
and reconstruction depends on preserved snapshots that are being deleted over
time. Counting rows reports healthy through exactly that state.

Read-only. Never opens the database for write, and never touches WAL.

    check_recompute_coverage.py --db /root/gecko-alpha/scout.db

Exit codes: 0 healthy (or nothing to recover) / 1 recovering nothing / 2 error.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

SURFACES = (
    ("gainers_comparisons", "appeared_on_gainers_at"),
    ("losers_comparisons", "appeared_on_losers_at"),
    ("trending_comparisons", "appeared_on_trending_at"),
)
CREDIT_BEARING = ("verified_canonical",)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="/root/gecko-alpha/scout.db")
    args = ap.parse_args()

    try:
        # mode=ro, not immutable=1: this IS the live database, with a running
        # writer and its sidecars already present. immutable would hide
        # uncheckpointed rows and could report a stale all-clear.
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"cannot open {args.db}: {exc}")
        return 2

    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='chain_identity_recompute_v1'"
        ).fetchone()
        if not row:
            print("chain_identity_recompute_v1 absent (schema not deployed yet)")
            return 0

        population = 0
        recovered = 0
        detail = []
        placeholders = ",".join("?" * len(CREDIT_BEARING))
        for table, anchor in SURFACES:
            untrusted = (
                "COALESCE(c.chains_identity_semantics, 'legacy_prefix') "
                "!= 'canonical_v1'"
            )
            pop = conn.execute(
                f"SELECT COUNT(*) FROM {table} AS c "
                f"WHERE {untrusted} AND c.detected_by_chains = 1"
            ).fetchone()[0]
            rec = conn.execute(
                f"SELECT COUNT(*) FROM {table} AS c "
                f"WHERE {untrusted} AND c.detected_by_chains = 1 "
                "AND EXISTS (SELECT 1 FROM chain_identity_recompute_v1 AS r "
                "  WHERE r.source_table = ? AND r.coin_id = c.coin_id "
                f"  AND r.historical_anchor = c.{anchor} "
                f"  AND r.evidence_status IN ({placeholders}))",
                (table, *CREDIT_BEARING),
            ).fetchone()[0]
            population += pop
            recovered += rec
            detail.append(f"{table}={rec}/{pop}")
    except sqlite3.Error as exc:
        print(f"query failed: {exc}")
        return 2
    finally:
        conn.close()

    summary = " ".join(detail)
    if population == 0:
        print(f"no pre-cutover chains credit to recover ({summary})")
        return 0
    if recovered == 0:
        print(
            f"overlay recovering NOTHING: 0 of {population} pre-cutover chains "
            f"detections keep their credit ({summary}). Run "
            "scripts/backfill_chain_identity_recompute.py --apply"
        )
        return 1
    print(f"recovered {recovered} of {population} ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
