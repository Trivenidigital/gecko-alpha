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

Read-only, and read-only in the way that MATTERS: `mode=ro` still creates
`-wal`/`-shm` sidecars beside whatever it opens. That is correct for the live
database, which has a running writer and already has them, and destructive
for anything else -- on 2026-08-15 sidecars left beside backups caused the
integrity checker to delete every real one, and on 2026-08-16 the identical
bug was found live in three more readers, one of which carried a comment
claiming parity with the other two. This is the script an operator reaches for
while DIAGNOSING, which is exactly when a tool gets pointed at a copy, so
anything that is not the live path is opened `immutable=1`.

    check_recompute_coverage.py --db /root/gecko-alpha/scout.db

Exit codes: 0 healthy (or nothing to recover) / 1 recovering nothing / 2 error.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

SURFACES = (
    ("gainers_comparisons", "appeared_on_gainers_at"),
    ("losers_comparisons", "appeared_on_losers_at"),
    ("trending_comparisons", "appeared_on_trending_at"),
)
CREDIT_BEARING = ("verified_canonical",)
LIVE_DB = "/root/gecko-alpha/scout.db"
#: Default gate. Overridable, because the readers compare `canonical_lead`
#: against CONVICTION_EARLY_LEAD_MINUTES at scoring time -- a status alone is
#: a frozen decision about the gate as it stood when the backfill ran.
DEFAULT_GATE_MINUTES = 1440.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=LIVE_DB)
    ap.add_argument("--gate-minutes", type=float, default=DEFAULT_GATE_MINUTES)
    args = ap.parse_args()

    live = Path(args.db) == Path(LIVE_DB)
    # Check existence explicitly. `immutable=1` on a MISSING file does not
    # error -- SQLite opens an empty database, so every table looks absent and
    # this script reported "schema not deployed yet" with exit 0. A missing or
    # unreadable database is the loudest condition there is, and switching the
    # snapshot path to immutable turned it into a silent all-clear.
    if not Path(args.db).exists():
        print(f"cannot open {args.db}: file does not exist")
        return 2

    try:
        # mode=ro ONLY for the live database, which has a running writer and
        # its sidecars already present -- immutable there would hide
        # uncheckpointed rows and report a stale all-clear. Anything else gets
        # immutable=1 so no sidecar is created beside it.
        uri = f"file:{args.db}?mode=ro" if live else f"file:{args.db}?immutable=1"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        print(f"cannot open {args.db}: {exc}")
        return 2

    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='chain_identity_recompute_v1'"
        ).fetchone()
        if not row:
            # Absent means two different things with opposite consequences.
            # Before the deploy it is normal. After it, the table was DROPPED
            # -- and then the trackers' overlay subqueries raise
            # `no such table` straight out of `get_gainers_comparisons`, so
            # every consumer is hard-broken while this said all-clear.
            #
            # Keyed on the POPULATION, not on whether the archives happen to
            # exist: archives can be dropped too, and the question that
            # actually matters is whether any row depends on the overlay.
            pop = 0
            for table, _anchor in SURFACES:
                try:
                    pop += conn.execute(
                        f"SELECT COUNT(*) FROM {table} AS c WHERE "
                        "COALESCE(c.chains_identity_semantics, 'legacy_prefix') "
                        "!= 'canonical_v1' AND c.detected_by_chains = 1"
                    ).fetchone()[0]
                except sqlite3.Error:
                    continue
            if pop:
                print(
                    "chain_identity_recompute_v1 is MISSING while "
                    f"{pop} rows depend on it -- the overlay was dropped "
                    "after deploy; the tracker subqueries will raise"
                )
                return 1
            print("chain_identity_recompute_v1 absent (schema not deployed yet)")
            return 0

        population = 0
        recovered = 0
        detail = []
        dark: list[str] = []
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
                f"  AND r.evidence_status IN ({placeholders}) "
                "  AND r.canonical_lead IS NOT NULL AND r.canonical_lead >= ?)",
                (table, *CREDIT_BEARING, args.gate_minutes),
            ).fetchone()[0]
            population += pop
            recovered += rec
            detail.append(f"{table}={rec}/{pop}")
            # PER SURFACE. A global "recovered nothing" is satisfied by one
            # healthy surface while the others sit stripped -- reachable
            # because the replay commits per surface, so a mid-run failure
            # leaves earlier surfaces durable. This script was printing
            # `losers=0/5 trending=0/5` in its own alert text and exiting 0.
            if pop > 0 and rec == 0:
                dark.append(table)
    except sqlite3.Error as exc:
        print(f"query failed: {exc}")
        return 2
    finally:
        conn.close()

    summary = " ".join(detail)
    if population == 0:
        print(f"no pre-cutover chains credit to recover ({summary})")
        return 0
    if dark:
        print(
            f"overlay recovering NOTHING on {', '.join(dark)}: "
            f"{recovered} of {population} pre-cutover chains detections keep "
            f"their credit ({summary}). Run "
            "scripts/backfill_chain_identity_recompute.py --apply"
        )
        return 1
    print(f"recovered {recovered} of {population} ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
