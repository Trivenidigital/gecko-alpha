#!/usr/bin/env python3
"""Arm the overlay coverage ratchet without waiting an hour for the scheduler.

WHY THIS EXISTS AS A SCRIPT
===========================

The runbook used to give this as a two-line fragment:

    cov = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

An independent reviewer pointed out that the fragment has no safe way to be
run, and that both of its implicit choices are wrong in the same direction --
they look harmless and are not:

1. **`db` has to come from somewhere.** The probe needs `self._conn`, so the
   obvious route is `Database(...).initialize()` -- which applies ~40 migrations
   against a database the pipeline is concurrently writing. The runbook warns
   two sections earlier that readers must open read-only BEFORE `initialize()`,
   and `scripts/backfill_chain_identity_recompute.py` repeats it, both citing
   the 2026-08-15 incident where a 2-minute cron did exactly that and produced
   74 `database is locked` errors. An instruction that silently requires
   `initialize()` contradicts its own runbook.

2. **`1440.0` was hardcoded and called "the same call the scheduler makes".**
   The scheduler reads `settings.CONVICTION_EARLY_LEAD_MINUTES`. The default is
   1440, so the two agree today and diverge the moment a `.env` overrides it.
   That divergence is not symmetric: the mark is a MAX ratchet, so arming under
   a LOOSER gate records a rate that is too high and manufactures a false
   collapse page on a later pass. The same runbook's watchdog section already
   forbids writing a threshold that can drift from the readers' -- this was that
   mistake, one section away from the rule against it.

So: connect directly, run no migrations, take the gate from Settings.

WHAT IT DOES NOT DO
===================

This does not open read-only. It cannot: arming the ratchet is a WRITE, and
that is the whole point of running it. What it avoids is the migration sweep.
The write itself goes through `_record_coverage_baseline`, which uses its own
connection and a short busy-timeout by design -- see its docstring for why that
write is deliberately not on the shared connection.

Exit codes: 0 armed -- 1 the probe reported a surface it could not judge --
2 could not connect or the schema is not deployed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def _arm() -> int:
    import aiosqlite

    from scout.config import Settings
    from scout.db import Database

    settings = Settings()
    db_path = str(settings.DB_PATH)

    db = Database(db_path)
    # Deliberately NOT `Database.initialize()`. See the module docstring.
    try:
        db._conn = await aiosqlite.connect(db_path)
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"could not connect to {db_path}: {exc}")
        return 2

    db._conn.row_factory = aiosqlite.Row
    try:
        await db._conn.execute(
            f"PRAGMA busy_timeout = {int(settings.SQLITE_BUSY_TIMEOUT_MS)}"
        )

        cur = await db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='chain_identity_recompute_v1'"
        )
        if not await cur.fetchone():
            print(
                "chain_identity_recompute_v1 does not exist -- deploy first, "
                "then run the backfill, then arm."
            )
            return 2

        gate = float(settings.CONVICTION_EARLY_LEAD_MINUTES)
        cov = await db.chain_identity_recompute_coverage_probe(gate_minutes=gate)
        print(json.dumps(cov, indent=2, default=str))

        unjudged = [
            table
            for table, v in cov.get("per_surface", {}).items()
            if not v.get("rate_judged")
        ]
        if unjudged:
            print(
                "\nNOT fully armed -- these surfaces were not judged: "
                + ", ".join(sorted(unjudged))
            )
            return 1

        print(f"\narmed at gate_minutes={gate} (from Settings, not hardcoded)")
        return 0
    finally:
        await db._conn.close()


def main() -> int:
    return asyncio.run(_arm())


if __name__ == "__main__":
    sys.exit(main())
