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
#: Current semantics generation. Mirrors `identity_recompute.RECOMPUTE_SEMANTICS`
#: (parity asserted by test). The version is part of the overlay's primary key,
#: so the table holds every generation at once and an unfiltered read would let
#: a superseded, more generous verdict keep counting as recovered.
RECOMPUTE_SEMANTICS = "chain_identity_recompute_v1"
#: Mirrors `Database._COLLAPSE_MIN_POPULATION` / `_COLLAPSE_FRACTION`
#: (parity asserted by test).
COLLAPSE_MIN_POPULATION = 20
COLLAPSE_FRACTION = 0.5
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

    # `.resolve()` on both sides: plain Path equality normalises `.` and `//`
    # but not `..`, and misses a relative spelling entirely -- so running from
    # the deploy directory would classify the LIVE database as non-live, open
    # it `immutable=1`, and hide uncheckpointed WAL rows. That is a stale
    # all-clear from the alarm, which is the direction that matters here.
    try:
        live = Path(args.db).resolve() == Path(LIVE_DB).resolve()
    except OSError:
        live = False
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
        if not live and Path(str(args.db) + "-wal").exists():
            wal_bytes = Path(str(args.db) + "-wal").stat().st_size
            if wal_bytes:
                # `immutable=1` IGNORES the -wal, so everything committed but
                # not yet checkpointed is invisible and this reads stale --
                # while printing the most reassuring message in the script.
                # A scratch copy of a live database is exactly this shape, and
                # a scratch copy is what the backfill's refusal recommends.
                print(
                    f"WARNING: {args.db} has a {wal_bytes}-byte -wal that "
                    "immutable=1 cannot see; results may be stale. Checkpoint "
                    "it, or point --db at the live database."
                )
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
        unarchivable = 0
        marks: dict[str, float] = {}
        per_surface: dict[str, tuple[int, int]] = {}
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
                "  AND r.semantics_version = ? "
                f"  AND r.historical_anchor = c.{anchor} "
                f"  AND r.evidence_status IN ({placeholders}) "
                "  AND r.canonical_lead IS NOT NULL AND r.canonical_lead >= ?)",
                (table, RECOMPUTE_SEMANTICS, *CREDIT_BEARING, args.gate_minutes),
            ).fetchone()[0]
            # Rows with no archived twin: written after the archives were
            # taken, so the backfill can never reach them. Surfaced IN THE
            # ALERT, not only in the runbook -- an unfixable page that reads
            # "run the backfill" sends the operator to a remedy that cannot
            # work, and that is how an alarm earns a mute.
            unarch = 0
            try:
                unarch = conn.execute(
                    f"SELECT COUNT(*) FROM {table} AS c WHERE "
                    "COALESCE(c.chains_identity_semantics, 'legacy_prefix') "
                    "!= 'canonical_v1' AND c.detected_by_chains = 1 "
                    f"AND NOT EXISTS (SELECT 1 FROM {table}_legacy_prefix_v1 AS a "
                    f"  WHERE a.coin_id = c.coin_id AND a.{anchor} = c.{anchor})"
                ).fetchone()[0]
            except sqlite3.Error:
                # Archive absent, so NO row has an archived twin -- every one
                # of them is unarchivable. The first version wrote `pass` under
                # a comment saying exactly this and then left the count at 0,
                # which would have told the operator to re-run a backfill that
                # had nothing to read.
                unarch = pop

            population += pop
            recovered += rec
            unarchivable += unarch
            per_surface[table] = (pop, rec)
            detail.append(f"{table}={rec}/{pop}")
            # PER SURFACE. A global "recovered nothing" is satisfied by one
            # healthy surface while the others sit stripped -- reachable
            # because the replay commits per surface, so a mid-run failure
            # leaves earlier surfaces durable. This script was printing
            # `losers=0/5 trending=0/5` in its own alert text and exiting 0.
            if pop > 0 and rec == 0:
                dark.append(table)

        # Collapse detection against the high-water mark the probe maintains.
        # Read-only: the probe owns the ratchet, this only compares. Must live
        # INSIDE the connection's lifetime -- placed after `conn.close()` it
        # raised ProgrammingError into a bare `except sqlite3.Error`, which
        # silently produced "no collapse" forever. A silent failure inside the
        # code added to fix silent failures.
        collapsed = []
        have_baseline = False
        for table, _anchor in SURFACES:
            try:
                row = conn.execute(
                    "SELECT best_rate, population FROM recompute_coverage_baseline "
                    "WHERE source_table = ?",
                    (table,),
                ).fetchone()
            except sqlite3.Error:
                break  # baseline table absent (pre-deploy): nothing to compare
            if row:
                have_baseline = True
                marks[table] = row[0]
            pop, rec = per_surface.get(table, (0, 0))
            if not row or pop < COLLAPSE_MIN_POPULATION:
                continue
            # The SAME population-comparability guard the probe applies. A mark
            # measured against a much smaller population is not comparable to
            # today's, so the return to a normal population reads as a
            # collapse. That false page was closed in the probe and left open
            # here -- in the layer that actually sends Telegram -- so the two
            # windows an operator has onto this system contradicted each other,
            # with journald quiet and Telegram screaming.
            if row[1] * 2 < pop:
                continue
            if rec / pop < row[0] * COLLAPSE_FRACTION:
                collapsed.append(table)
    except sqlite3.Error as exc:
        print(f"query failed: {exc}")
        return 2
    finally:
        conn.close()

    # Name the mark each surface was compared against. Without it an operator
    # reading a collapse page cannot tell a real collapse from a wrong mark
    # without opening the database, and an unarmed surface reads identically
    # to a healthy one.
    detail = [
        f"{d} mark={marks[t]:.4f}" if t in marks else f"{d} mark=none"
        for d, t in zip(detail, [tbl for tbl, _ in SURFACES])
    ]
    summary = " ".join(detail)
    if population == 0:
        print(f"no pre-cutover chains credit to recover ({summary})")
        return 0
    if collapsed and not dark:
        print(
            f"overlay recovery COLLAPSED on {', '.join(collapsed)}: "
            f"{recovered} of {population} keep their credit, far below the "
            f"recorded high-water rate ({summary}). History may have been "
            "deleted; re-run the backfill and check the /root snapshots."
        )
        return 1

    if dark:
        remedy = (
            "Run scripts/backfill_chain_identity_recompute.py --apply"
            if unarchivable < population
            else "THE BACKFILL CANNOT HELP: every row post-dates the archives"
        )
        print(
            f"overlay recovering NOTHING on {', '.join(dark)}: "
            f"{recovered} of {population} pre-cutover chains detections keep "
            f"their credit, {unarchivable} unarchivable ({summary}). {remedy}"
        )
        return 1
    # Name the un-armed state. The collapse half of this alarm is gated on the
    # in-process probe having recorded a mark -- this script is read-only by
    # design and cannot bootstrap one. Without saying so, "no baseline yet"
    # reads identically to "no collapse", which is the wrong reassurance for an
    # alarm built because the in-process log is operationally silence.
    armed = (
        ""
        if have_baseline
        else "  [collapse detection NOT ARMED: no baseline recorded yet]"
    )
    print(
        f"recovered {recovered} of {population}, "
        f"{unarchivable} unarchivable ({summary}){armed}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
