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


def collect_history(sources, *, live_db: str) -> dict[str, str]:
    """Earliest known event per token across every readable source.

    Only ever keeps the EARLIER value. That direction is safe: a first-seen
    moving earlier can only lengthen a lead, so nothing that already qualified
    can stop qualifying.
    """
    earliest: dict[str, str] = {}
    queries = [
        (
            live_db,
            "SELECT token_id, MIN(created_at) FROM signal_events "
            "WHERE token_id IS NOT NULL AND token_id != '' GROUP BY token_id",
        ),
    ]
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
            conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
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
    ap.add_argument(
        "--apply",
        action="store_true",
        help="write the results; otherwise report and change nothing",
    )
    ap.add_argument("--gate-minutes", type=float, default=1440.0)
    args = ap.parse_args()

    from scout.db import Database
    from scout.identity_recompute import recompute_legacy_provenance

    print("Collecting history from every readable source (read-only):")
    history = collect_history(SNAPSHOT_SOURCES, live_db=args.db)
    floor = min(history.values()) if history else None
    print(f"union: {len(history)} tokens, earliest = {floor}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    db = Database(Path(args.db))
    await db.initialize()
    try:
        counts = await recompute_legacy_provenance(
            db._conn, gate_minutes=args.gate_minutes, extra_history=history
        )
    finally:
        await db.close()

    print("\nEvidence status:")
    for status, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {status:42s} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
