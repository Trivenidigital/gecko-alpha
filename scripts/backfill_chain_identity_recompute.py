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


def _is_live(path: str) -> bool:
    """Only the real production database counts as live.

    Compared against the LIVE_DB constant, deliberately never against `--db`.
    Keying off the argument meant ANY target was treated as live and opened
    `mode=ro`, so rehearsing the backfill against a BACKUP -- the cautious
    thing `--dry-run` exists to allow -- planted `-wal`/`-shm` sidecars beside
    that backup. That precise bug destroyed the real backups on 2026-08-15 and
    had shipped in three separate readers before it was found.
    """
    return Path(path) == Path(LIVE_DB)


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
            live = _is_live(path)
            conn = _open_read_only(path, live=live)
            # Each source's interval must describe the SAME table that source's
            # history was read from, or the two disagree: a token whose
            # first-seen came from `signal_first_seen` could fall outside an
            # interval derived from `signal_events` and be marked uncovered on
            # evidence that never applied to it.
            row = conn.execute(
                "SELECT MIN(first_seen_at), MAX(first_seen_at) FROM signal_first_seen"
                if live
                else "SELECT MIN(created_at), MAX(created_at) FROM signal_events"
            ).fetchone()
            conn.close()
            if row and row[0] and row[1]:
                spans.append((row[0], row[1]))
        except sqlite3.Error:
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
    # `test_signal_first_seen_sole_writer.py` enforces this repo-wide; it
    # caught this exact mistake here.
    queries = [
        (
            live_db,
            "SELECT token_id, first_seen_at FROM signal_first_seen "
            "WHERE token_id IS NOT NULL AND token_id != ''",
        ),
    ]
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
    if args.gate_minutes is None:
        args.gate_minutes = float(get_settings().CONVICTION_EARLY_LEAD_MINUTES)

    from scout.identity_recompute import recompute_legacy_provenance

    print("Collecting history from every readable source (read-only):")
    history = collect_history(SNAPSHOT_SOURCES, live_db=args.db)
    intervals = collect_intervals(SNAPSHOT_SOURCES, live_db=args.db)
    floor = min(history.values()) if history else None
    print(f"union: {len(history)} tokens, earliest = {floor}")
    print("coverage intervals (anchors OUTSIDE these stay indeterminate):")
    for start, end in intervals:
        print(f"  {start}  ..  {end}")

    if args.apply and any(Path(args.db) == Path(src) for src in SNAPSHOT_SOURCES):
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
    conn = await aiosqlite.connect(args.db)
    # Match the pipeline's busy_timeout. aiosqlite defaults to 5s while the
    # pipeline sets 90s, and that asymmetry decides who loses: if the pipeline
    # holds the write lock when this script's first INSERT fires, the BACKFILL
    # dies -- after it has already scanned several GB of snapshots, with no
    # retry. Nothing is corrupted (uncommitted work is discarded), but the
    # operator's remediation for a page fails on a lock and starts over.
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
        counts = await recompute_legacy_provenance(
            conn,
            gate_minutes=args.gate_minutes,
            extra_history=history,
            coverage_intervals=intervals,
        )
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
