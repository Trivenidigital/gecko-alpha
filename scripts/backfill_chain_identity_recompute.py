#!/usr/bin/env python3
"""Offline backfill of `chain_identity_recompute_v1` using preserved history.

WHY THIS IS A SCRIPT AND NOT PART OF THE PIPELINE
-------------------------------------------------
The live substrate's floor is 2026-08-09 while legacy anchors reach back to
April, so substrate history alone resolves only 155 of 826 credited production
rows (18.8%) and leaves 913 indeterminate. The preserved `/root` snapshots reach
back to 2026-06-19 and take that to 538 (65.1%).

But those snapshots are ad-hoc operator files outside the application's data
model, and the pipeline must not depend on them: a runtime that reads `/root`
paths breaks the moment they are (rightly) deleted. So the extra history enters
through a one-off ops step, writes the same versioned table, and the runtime
reads one source of truth either way.

READ-ONLY on every history source. Snapshots are opened `immutable=1`, which is
also what prevents the WAL-sidecar creation that destroyed the real backups on
2026-08-15.

    uv run python -m scripts.backfill_chain_identity_recompute --dry-run
    uv run python -m scripts.backfill_chain_identity_recompute --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from datetime import datetime

import aiosqlite

from scout.config import get_settings
from scout.identity_recompute import CREDIT_BEARING
from pathlib import Path

#: Preserved snapshots, newest first. Absent files are skipped, not fatal --
#: `bak-before-state-fix` was deleted under an authorized ruling on 2026-08-23
#: and others may follow, which must degrade coverage rather than crash.
SNAPSHOT_SOURCES = (
    "/root/scout.db.pre500.20260802202122",
    "/root/kraken_rehearsal/scout_copy.db",
    "/root/scout.db.pre-deploy2-20260703",
)

LIVE_DB = "/root/gecko-alpha/scout.db"


def _same_file(a: str, b: str) -> bool:
    """Path identity that survives `..`, `//` and relative spellings."""
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _is_live(path: str) -> bool:
    """Decides ONE thing: whether to open this path `mode=ro` or `immutable=1`.

    Compared against the LIVE_DB constant, deliberately never against `--db`.
    Keying off the argument meant ANY target was treated as live and opened
    `mode=ro`, so rehearsing the backfill against a BACKUP -- the cautious
    thing `--dry-run` exists to allow -- planted `-wal`/`-shm` sidecars beside
    that backup. That precise bug destroyed the real backups on 2026-08-15 and
    had shipped in three separate readers before it was found.

    `.resolve()` on both sides because plain `Path` equality is a string
    comparison with a little normalisation: it handles `.` and `//` but not
    `..`, and misses a relative spelling entirely. Running the documented
    `cd /root/gecko-alpha && ... --db scout.db` compared unequal, so the LIVE
    database was opened `immutable=1` -- which hides uncheckpointed WAL rows
    and silently under-resolves exactly the most recent anchors. Fewer
    resolutions look identical to less history.
    """
    return _same_file(path, LIVE_DB)


def _history_table(path: str) -> tuple[str, str]:
    """(column, table) this source's history is read from -- ONE decision.

    Keyed on what the FILE contains, not on whether it is the live database.
    An earlier version used `_is_live` for both the open mode and the history
    table, which quietly made the two disagree for any path that is not
    literally LIVE_DB: `collect_history` read `signal_first_seen` while
    `collect_intervals` read `signal_events`, so a token first seen in June
    fell outside an August-derived interval and was marked uncovered on
    evidence that never applied to it. That is verbatim the failure the
    comment in `collect_intervals` says this exists to prevent -- and the
    configuration that triggers it, a scratch copy of the live database, is
    the one the refusal message recommends and the one the acceptance replay
    is measured on.

    `signal_first_seen` where it exists: it is the retention-decoupled answer.
    `signal_events` otherwise: the preserved snapshots predate that table
    (migration 20260823) and it is the only history they carry.
    """
    try:
        conn = _open_read_only(path, live=_is_live(path))
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='signal_first_seen'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return ("created_at", "signal_events")
    return (
        ("first_seen_at", "signal_first_seen")
        if row
        else ("created_at", "signal_events")
    )


def _open_read_only(path: str, *, live: bool) -> sqlite3.Connection:
    """Open a history source read-only, picking the mode by what the file IS.

    The distinction matters in both directions.

    ``immutable=1`` promises SQLite the file cannot change, so it skips locking
    AND ignores any ``-wal``. For the frozen snapshots that is exactly right:
    it is also what stops a read-only open from CREATING ``-wal``/``-shm``
    sidecars beside a backup, which is how the real backups were destroyed on
    2026-08-15.

    For the LIVE database it is wrong. A pipeline is writing to it and the WAL
    may hold thousands of uncheckpointed rows. Opened immutable those rows are
    invisible, so the backfill would compute against stale history and
    under-resolve exactly the most recent anchors -- silently, since fewer
    resolutions look identical to less history. ``mode=ro`` reads the WAL
    properly, and the sidecar hazard does not apply: a live database already
    has them.
    """
    if live:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return sqlite3.connect(f"file:{path}?immutable=1", uri=True)


def collect_intervals(sources, *, live_db: str) -> list[tuple[str, str]]:
    """The windows each source actually spans, merged where they overlap.

    Declared rather than inferred: a single global minimum would mark an anchor
    inside a GAP as covered, and the union here has real gaps
    (2026-07-03..07-17 and 2026-08-02..08-08 are in no source). Inside those,
    absence of a canonical match is not evidence of absence.
    """
    spans: list[tuple[str, str]] = []
    for path in [live_db, *sources]:
        if not Path(path).exists():
            continue
        try:
            conn = _open_read_only(path, live=_is_live(path))
            # The SAME decision collect_history made for this path, via
            # the same function. Both were previously keyed on `_is_live`,
            # which answers a DIFFERENT question -- so for any path except
            # the literal LIVE_DB, history came from `signal_first_seen`
            # while the interval came from `signal_events`, and a token
            # first seen in June fell outside an August-derived interval,
            # marked uncovered on evidence that never applied to it. That
            # is verbatim the failure this comment used to claim it
            # prevented, and the configuration that triggers it -- a
            # scratch copy -- is what the refusal message recommends and
            # what the acceptance replay is measured on.
            col, tbl = _history_table(path)
            row = conn.execute(f"SELECT MIN({col}), MAX({col}) FROM {tbl}").fetchone()
            conn.close()
            if row and row[0] and row[1]:
                spans.append((row[0], row[1]))
        except sqlite3.Error as exc:
            # Printed, not swallowed. Intervals decide `verified` vs
            # `indeterminate`, so a source dropping out silently narrows
            # coverage in a way that looks identical to history genuinely
            # running out -- and `collect_history` prints per source, so the
            # two halves disagreed about how loud a failure is.
            print(f"  {path}: INTERVAL UNREADABLE ({exc}) -- coverage narrowed")
            continue
    spans.sort()
    merged: list[tuple[str, str]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def collect_history(sources, *, live_db: str) -> dict[str, str]:
    """Earliest known event per token across every readable source.

    Only ever keeps the EARLIER value. That direction is safe: a first-seen
    moving earlier can only lengthen a lead, so nothing that already qualified
    can stop qualifying.
    """
    earliest: dict[str, str] = {}

    # The LIVE database reads `signal_first_seen`, never `signal_events`.
    # Deriving a first-seen from the events table re-couples it to RETENTION:
    # the age prune keeps roughly 14 days, so `MIN(created_at)` there is not
    # "when the token was first seen", it is "the oldest row retention has not
    # deleted yet" -- a floor that walks forward every night. Using it would
    # have quietly shortened every reconstructed lead on the live side.
    # NOTE ON THE GUARD: `test_signal_first_seen_sole_writer.py` caught this
    # mistake originally, but it cannot catch a REGRESSION here. Its
    # allowlist is FILE-level, and this file is allowlisted for the snapshot
    # queries below -- so restoring `MIN(created_at) FROM signal_events` on
    # the live path passes the entire suite. Review demonstrated that
    # mutant surviving all 57 tests. The behavioural test in
    # tests/test_backfill_history_source.py is what actually pins it.
    live_col, live_tbl = _history_table(live_db)
    if live_tbl == "signal_events":
        live_query = (
            f"SELECT token_id, MIN({live_col}) FROM {live_tbl} "
            "WHERE token_id IS NOT NULL AND token_id != '' GROUP BY token_id"
        )
    else:
        live_query = (
            f"SELECT token_id, {live_col} FROM {live_tbl} "
            "WHERE token_id IS NOT NULL AND token_id != ''"
        )
    queries = [(live_db, live_query)]
    # The SNAPSHOTS are the documented exception, and the retention argument
    # does not apply to them: each is a frozen file whose contents can never
    # change, so nothing can move its derived minimum. They also PREDATE the
    # `signal_first_seen` table entirely (migration 20260823), so the events
    # table is the only history they carry. Falling back is not a shortcut --
    # it is the sole source, and refusing it would discard the June/July
    # coverage that resolves most of the population.
    queries += [
        (
            src,
            "SELECT token_id, MIN(created_at) FROM signal_events "
            "WHERE token_id IS NOT NULL AND token_id != '' GROUP BY token_id",
        )
        for src in sources
    ]
    for path, query in queries:
        if not Path(path).exists():
            print(f"  {path}: MISSING (skipped — coverage degrades, not an error)")
            continue
        try:
            conn = _open_read_only(path, live=_is_live(path))
            n = 0
            for token_id, first in conn.execute(query):
                if not token_id or not first:
                    continue
                if token_id not in earliest or first < earliest[token_id]:
                    earliest[token_id] = first
                n += 1
            conn.close()
            print(f"  {path}: {n} tokens")
        except sqlite3.Error as exc:
            print(f"  {path}: UNREADABLE ({exc}) — skipped")
    return earliest


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=LIVE_DB)
    # --dry-run is accepted explicitly rather than merely implied by omitting
    # --apply. It is the invocation the module docstring documents, and a
    # script that rejects its own documented flag with "unrecognized
    # arguments" reads as broken rather than as safe-by-default -- which
    # invites reaching for --apply just to make it run.
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write the results")
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="report coverage and change nothing (the default)",
    )
    # Default from the SETTING the consumer reads, never from a second literal
    # 1440. The overlay classifies a row as credit-bearing using this gate and
    # `cross_surface` re-checks the same threshold; if the operator moves the
    # setting and the backfill keeps its own copy, rows are stamped
    # `verified_canonical` that the consumer then refuses -- a disagreement
    # visible nowhere, because both halves look internally consistent.
    ap.add_argument(
        "--gate-minutes",
        type=float,
        # Resolved AFTER parsing (below). Calling get_settings() here runs at
        # argparse-construction time -- unconditionally, before any argument is
        # read -- so on a box without a complete .env the script died with a raw
        # pydantic ValidationError even for --help, --dry-run, and the refusal
        # path that exists to print guidance.
        default=None,
        help="early-detection gate; defaults to CONVICTION_EARLY_LEAD_MINUTES",
    )
    args = ap.parse_args()

    from scout.identity_recompute import (
        recompute_legacy_provenance,
        reconciliation_report,
    )

    print("Collecting history from every readable source (read-only):")
    history = collect_history(SNAPSHOT_SOURCES, live_db=args.db)
    intervals = collect_intervals(SNAPSHOT_SOURCES, live_db=args.db)
    floor = min(history.values()) if history else None
    print(f"union: {len(history)} tokens, earliest = {floor}")
    print("coverage intervals (anchors OUTSIDE these stay indeterminate):")
    for start, end in intervals:
        print(f"  {start}  ..  {end}")
    if not intervals:
        # Loud, because the run still "succeeds": it writes a full overlay of
        # indeterminate rows, which is a populated table that grants credit to
        # nothing.
        print("  *** NO COVERAGE INTERVALS -- every row will be indeterminate ***")

    # Per-source spans, printed so they can be eyeballed against the retention
    # window rather than trusted through the merge. A span is not coverage: it
    # asserts "this file holds events between T0 and T1", not "every token in
    # that window is recorded here". The specific failure it cannot rule out is
    # ONE stale surviving row stretching a source's span back to its date and
    # fabricating a wide interval. A span materially wider than the retention
    # window is that tell. (Checked 2026-08-23: all four sources span 14.0
    # days, exactly the prune window.)
    print("per-source spans (each should be ~= the retention window):")
    for path in [args.db, *SNAPSHOT_SOURCES]:
        if not Path(path).exists():
            continue
        try:
            conn = _open_read_only(path, live=_is_live(path))
            col, tbl = _history_table(path)
            row = conn.execute(f"SELECT MIN({col}), MAX({col}) FROM {tbl}").fetchone()
            conn.close()
            if row and row[0] and row[1]:
                days = (
                    datetime.fromisoformat(row[1]) - datetime.fromisoformat(row[0])
                ).total_seconds() / 86400.0
                flag = "  <-- WIDER THAN RETENTION" if days > 21 else ""
                print(f"  {Path(path).name:44s} {days:5.1f}d{flag}")
        except (sqlite3.Error, ValueError):
            continue

    # `.resolve()`, matching `_is_live`. Plain Path equality misses a `..`
    # spelling, and past that refusal `aiosqlite.connect` opens the snapshot
    # READ-WRITE and plants sidecars beside a forensic file before the schema
    # check declines. Two comparisons in this file; only one was hardened.
    if args.apply and any(_same_file(args.db, src) for src in SNAPSHOT_SOURCES):
        print(
            f"REFUSING: {args.db} is a preserved forensic snapshot. --apply "
            "would write overlay rows into it. Point --db at the live "
            "database or at a scratch copy."
        )
        return 2

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    # Deliberately NOT Database.initialize(): that applies every pending
    # migration, and this script is pointed at a database with a live pipeline
    # attached. An ops backfill must not be the thing that decides to ALTER
    # production tables -- the deploy does that, in its own window, once.
    # Attach to what is already there, and refuse if the schema has not landed.
    # Resolved HERE, not after parse_args(). Settings needs the production
    # .env, and resolving it earlier meant --dry-run and the refusal path both
    # died with a raw pydantic ValidationError on any box without one -- the
    # rehearsal path that --dry-run exists to provide. Only --help worked.
    if args.gate_minutes is None:
        args.gate_minutes = float(get_settings().CONVICTION_EARLY_LEAD_MINUTES)

    conn = await aiosqlite.connect(args.db)
    # Match the pipeline's busy_timeout. aiosqlite defaults to 5s while the
    # pipeline sets 90s, and that asymmetry decides who loses: if the pipeline
    # holds the write lock when this script's first INSERT fires, the BACKFILL
    # dies -- after it has already scanned several GB of snapshots, with no
    # retry. Nothing is corrupted (uncommitted work is discarded), but the
    # operator's remediation for a page fails on a lock and starts over.
    # Verified: pipeline holding the lock 12s, backfill waited 11.6s and
    # proceeded, where at the inherited 5s default it died after 5.6s.
    #
    # The failure has MOVED, not gone: with 90s on both sides, whoever arrives
    # second fails if the other exceeds 90s. The replay runs ~4.5s at current
    # scale, so roughly 20x headroom -- but that is now the number that
    # matters, and nothing measures it. It shrinks as the archived population
    # grows.
    await conn.execute("PRAGMA busy_timeout = 90000")
    try:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='chain_identity_recompute_v1'"
        )
        if not await cur.fetchone():
            print(
                "REFUSING: chain_identity_recompute_v1 does not exist. "
                "Deploy the schema first, then re-run this backfill."
            )
            return 2
        # The reconciliation is printed even when the replay RAISES. Per-surface
        # commits mean a crash leaves earlier surfaces durable, so the operator
        # needs to know what landed -- and the status breakdown that names the
        # shortfall lives only in the `counts` dict, which the exception
        # discards. Without this they get a traceback in exactly the run where
        # knowing what committed matters most.
        counts = await recompute_legacy_provenance(
            conn,
            gate_minutes=args.gate_minutes,
            extra_history=history,
            coverage_intervals=intervals,
        )
    except Exception:
        try:
            rep = await reconciliation_report(conn)
            print(f"\nREPLAY FAILED -- what is durable on disk:")
            print(
                f"  population {rep['population']}  replayed {rep['replayed']}  "
                f"stored {rep['stored']}"
            )
            if rep["surfaces_never_written"]:
                print(
                    "  never written: "
                    + ", ".join(rep["surfaces_never_written"])
                    + "  <-- the replay did not reach these; re-run --apply"
                )
        except Exception:
            print("\nREPLAY FAILED and the reconciliation could not be read.")
        raise
    finally:
        await conn.close()

    print("\nEvidence status:")
    for status, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {status:42s} {n}")

    recovered = sum(counts.get(st, 0) for st in CREDIT_BEARING)
    print(f"\n  credit-bearing results: {recovered} of {sum(counts.values())}")
    if not recovered:
        # Exit NONZERO. This run is the operator's remediation for a
        # NOT_RECOVERING page; reporting success while resolving nothing would
        # let them tick the box and walk away from an unfixed collapse.
        print(
            "  NOTHING RECOVERED -- this overlay will not restore any chains "
            "credit. Check that the history snapshots are still present."
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
