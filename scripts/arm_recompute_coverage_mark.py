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


def unarmed_surfaces(per_surface: dict) -> list[str]:
    """Surfaces the probe did NOT arm. The single source of truth for success.

    Extracted so the tests can call it instead of copying it. The first version
    of the test file held a hand-written copy of this comprehension, with a
    docstring claiming it was "deliberately not a re-implementation" -- it was
    one, and every behavioural test was therefore structurally incapable of
    seeing a change to this script. The only link back was two substring scans,
    and a reviewer showed both SURVIVE reverting the predicate: the string
    `v.get("comparison_skipped")` also appears in the reporting line below and
    in a comment above, so an unrelated live call site satisfied the guard
    while the decision it guarded was gone. A scan satisfiable by prose is
    equally satisfiable by an unrelated use.

    WHY THIS PREDICATE, in both directions:

    `rate_judged` alone is NOT enough.
    `chain_identity_recompute_coverage_probe` sets `rate_judged = True` BEFORE
    calling `_classify_coverage_mark` (scout/db.py:8524). The
    `incomparable_unresolved` arm then sets `mark_written = False`, sets
    `comparison_skipped`, and continues -- leaving `rate_judged` True. So the
    one arm meaning "nothing was armed" is invisible to a `rate_judged` filter,
    and the script printed "armed", exited 0, and left a surface with no usable
    mark: the exact collapse-unreachable state it exists to prevent.

    `mark_written` is NOT the alternative. The healthy `compare` arm (already
    armed, today does not improve it, no write needed) also sets it False, so
    keying on that would report a correctly-armed system as broken on every
    good re-run -- an alarm that cries wolf on the normal path, which is the
    failure mode this whole component is about.
    """
    return [
        table
        for table, v in per_surface.items()
        # "Was judged, and yet has no mark" -- a CONJUNCTION, and both halves
        # are load-bearing:
        #
        #   rate_judged=True + best_rate=None  -> `rearm` whose write STARVED.
        #       The probe asked for a mark, `_record_coverage_baseline` gave up
        #       after `_BASELINE_WRITE_TIMEOUT_MS`, swallowed the error and
        #       returned None. No `comparison_skipped` is set, so the previous
        #       predicate could not see it AT ALL: the script printed "armed",
        #       exited 0, and left zero marks on every surface. Worst possible
        #       timing -- this script exists for the first arm after a deploy,
        #       when no mark exists so EVERY surface takes `rearm`, against a
        #       live database the pipeline is writing.
        #
        #   rate_judged=False                  -> below `_COLLAPSE_MIN_POPULATION`.
        #       Also `best_rate=None`, but this is the DOCUMENTED steady state,
        #       not a fault -- production's trending surface drains through it.
        #       Keying on `best_rate is None` alone (or on `not rate_judged`)
        #       makes this a permanent exit 1 with a remedy that can never work,
        #       which is the cry-wolf property this function's own docstring
        #       rejects `mark_written` for. Excluded by the conjunction.
        if (v.get("rate_judged") and v.get("best_rate") is None)
        or v.get("comparison_skipped")
    ]


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

        # Both tables, not just the overlay. The probe reads the overlay AND
        # reads/writes `recompute_coverage_baseline`; guarding only the first
        # left an absent baseline table raising an uncaught OperationalError
        # past the advertised "2 = schema not deployed". Low reachability --
        # both arrive in the same `initialize()` -- but the guard was
        # incomplete on the axis it claimed to cover.
        for table in ("chain_identity_recompute_v1", "recompute_coverage_baseline"):
            cur = await db._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            if not await cur.fetchone():
                print(
                    f"{table} does not exist -- deploy first, then run the "
                    "backfill, then arm."
                )
                return 2

        gate = float(settings.CONVICTION_EARLY_LEAD_MINUTES)
        cov = await db.chain_identity_recompute_coverage_probe(gate_minutes=gate)
        print(json.dumps(cov, indent=2, default=str))

        # The payload-level verdict, which this script PRINTED and then ignored.
        # Arming while nothing is recovering records a mark of 0.0 on every
        # surface -- the frozen-at-zero mark that makes
        # `rate < best * _COLLAPSE_FRACTION` unsatisfiable for every rate, which
        # the runbook names as the hazard three paragraphs above the command
        # that does it. Not invisible (dark fires, and the mark ratchets up on a
        # later pass) but the operator must not be told "armed".
        if cov.get("not_recovering"):
            print(
                "\nNOT ARMED -- recovery is zero on: "
                + ", ".join(sorted(cov.get("dark_surfaces") or []))
                + "\nA mark of 0.0 makes collapse detection unsatisfiable for "
                "every rate. Run the backfill first, then arm."
            )
            return 1

        unarmed = unarmed_surfaces(cov.get("per_surface", {}))
        if unarmed:
            print("\nNOT fully armed:")
            for table in sorted(unarmed):
                v = cov["per_surface"][table]
                reason = v.get("comparison_skipped") or "rate could not be judged"
                print(f"  {table}: {reason}")
            print(
                "\nRe-run after the cause clears. A surface left here has no "
                "usable mark, so collapse detection cannot fire for it."
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
