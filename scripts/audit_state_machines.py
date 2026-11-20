#!/usr/bin/env python3
"""Read-only check of a gecko-alpha database against the state-machine registry.

Reports, for every machine stored in a table: how many rows are in each
declared state, any rows in a state nobody declared (an unknown status value,
or a combination of columns that matches no state — for example a lane that is
enabled yet carries a suspension timestamp), and any broken invariant (for
example a closed trade with no close time).

    python scripts/audit_state_machines.py --db scout.db
    python scripts/audit_state_machines.py --db scout.db --json
    python scripts/audit_state_machines.py --db backup.db --immutable

It opens the database read-only (SQLite ``mode=ro``) and runs only SELECT
statements. Each machine costs one or two full scans of its table, so on a
large live database prefer a quiet moment or a backup copy. ``--immutable``
is for a copy that nothing else is writing — a backup file without its
``-wal`` / ``-shm`` companions, or one on read-only media. Never use it on the
live database: SQLite then skips locking and change detection.

Exit status: 0 nothing to report; 1 findings; 2 the database, or part of it,
could not be read, or it holds none of the registered tables (findings take
precedence over unreadable parts).
The meaning of every state is in docs/state_machines.md.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scout.statemachines.audit import audit  # noqa: E402


def database_uri(path: Path, *, immutable: bool = False) -> str:
    """A read-only SQLite URI for ``path`` (safe for spaces, ``?`` and ``#``)."""
    uri = path.resolve().as_uri() + "?mode=ro"
    return uri + "&immutable=1" if immutable else uri


def _print_human(report: dict) -> None:
    for m in report["machines"]:
        status = m["status"]
        if status == "not_stored_in_db":
            continue
        print(f"\n{m['machine']}  ({m['entity']})  — {status}")
        if m.get("scope"):
            print(f"    rows considered: {m['scope']}")
        if status == "unreadable":
            print(f"  ? could not be read: {m.get('error', 'unknown error')}")
            continue
        if status == "table_absent":
            continue
        for state, n in m["counts"].items():
            print(f"    {state:<28} {n}")
        for value, n in m["foreign"].items():
            print(f"    {value:<28} {n}   (sentinel, not a state)")
        for value, n in m["undeclared"].items():
            print(f"  ! {value:<28} {n}   NOT A DECLARED STATE")
        for inv in m["invariants"]:
            if inv["status"] == "violated":
                print(
                    f"  ! invariant {inv['name']}: {inv['violations']} rows — "
                    f"{inv['description']}"
                )
            elif inv["status"] == "unreadable":
                print(
                    f"  ? invariant {inv['name']} could not be checked: "
                    f"{inv.get('error', 'unknown error')}"
                )
    findings = report["machines_with_findings"]
    unreadable = report["machines_unreadable"]
    print(
        f"\n{report['machines_checked']} machines checked, "
        f"{len(findings)} with findings"
        + (f": {', '.join(findings)}" if findings else "")
    )
    if unreadable:
        print(f"{len(unreadable)} could not be fully read: {', '.join(unreadable)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default="scout.db", help="path to the SQLite database")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--immutable",
        action="store_true",
        help="for a backup copy nothing else writes to; never the live database",
    )
    args = parser.parse_args(argv)

    path = Path(args.db)
    if not path.is_file():
        print(f"database not found: {path}", file=sys.stderr)
        return 2
    try:
        conn = sqlite3.connect(database_uri(path, immutable=args.immutable), uri=True)
    except sqlite3.Error as exc:
        print(f"could not open {path}: {exc}", file=sys.stderr)
        return 2
    try:
        report = audit(conn)
    except sqlite3.Error as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print_human(report)
    if report["machines_with_findings"]:
        return 1
    if report["machines_unreadable"]:
        return 2
    if report["machines_checked"] == 0:
        # Not one registered table is present: almost certainly the wrong
        # file. Saying "nothing to report" here would read as a clean bill.
        print(
            f"no state-machine tables found in {path}: is this the right database?",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
