"""Read-only audit of a database against the registry.

For every machine stored in a table it answers three questions:

- how many rows sit in each declared state;
- are there rows in a state the registry does not know (a value nobody
  declared, a column combination that matches no derived state, or one that
  matches more than one);
- do any rows break a declared invariant.

It only ever runs SELECTs, and callers open the database read-only. A table
that does not exist (an older database) is reported as absent, not as an
error. Each machine costs one or two full scans of its table.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from scout.statemachines.model import Machine
from scout.statemachines.registry import MACHINES

NO_STATE = "(matches no declared state)"
MANY_STATES = "(matches more than one declared state)"
NULL_VALUE = "(NULL)"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _where(m: Machine) -> str:
    return f" WHERE ({m.scope})" if m.scope else ""


def _audit_column(conn: sqlite3.Connection, m: Machine) -> dict[str, Any]:
    rows = conn.execute(
        f"SELECT {m.column}, COUNT(*) FROM {m.table}{_where(m)} "  # noqa: S608
        f"GROUP BY {m.column}"
    ).fetchall()
    counts = {name: 0 for name in m.state_names}
    foreign: dict[str, int] = {}
    undeclared: dict[str, int] = {}
    for value, n in rows:
        if value is None:
            if m.null_state is not None:
                counts[m.null_state] += n
            else:
                undeclared[NULL_VALUE] = n
        elif not isinstance(value, str):
            # A number or blob in a status column is never a declared state,
            # even if its text form happens to spell one.
            key = repr(value)
            undeclared[key] = undeclared.get(key, 0) + n
        elif value in counts:
            counts[value] += n
        elif value in m.foreign_values:
            foreign[value] = n
        else:
            undeclared[value] = n
    return {"counts": counts, "foreign": foreign, "undeclared": undeclared}


def _audit_derived(conn: sqlite3.Connection, m: Machine) -> dict[str, Any]:
    # A predicate that evaluates to NULL for a row has not matched it, so
    # every predicate is folded to 0/1 before anything is counted.
    flags = [f"CASE WHEN ({s.predicate}) THEN 1 ELSE 0 END" for s in m.states]
    per_state = ", ".join(f"COALESCE(SUM({f}), 0)" for f in flags)
    matched = " + ".join(flags)
    row = conn.execute(
        f"SELECT {per_state}, "  # noqa: S608
        f"COALESCE(SUM(CASE WHEN ({matched}) = 0 THEN 1 ELSE 0 END), 0), "
        f"COALESCE(SUM(CASE WHEN ({matched}) > 1 THEN 1 ELSE 0 END), 0) "
        f"FROM {m.table}{_where(m)}"
    ).fetchone()
    counts = {s.name: row[i] for i, s in enumerate(m.states)}
    unmatched, ambiguous = row[len(m.states)], row[len(m.states) + 1]
    undeclared: dict[str, int] = {}
    if unmatched:
        undeclared[NO_STATE] = unmatched
    if ambiguous:
        undeclared[MANY_STATES] = ambiguous
    return {"counts": counts, "foreign": {}, "undeclared": undeclared}


def audit_machine(conn: sqlite3.Connection, m: Machine) -> dict[str, Any]:
    result: dict[str, Any] = {
        "machine": m.name,
        "entity": m.entity,
        "scope": m.scope,
        "status": "ok",
        "counts": {},
        "foreign": {},
        "undeclared": {},
        "invariants": [],
    }
    if m.storage not in ("column", "derived"):
        result["status"] = "not_stored_in_db"
        return result
    if not _table_exists(conn, m.table or ""):
        result["status"] = "table_absent"
        return result
    try:
        found = (
            _audit_column(conn, m) if m.storage == "column" else _audit_derived(conn, m)
        )
    except sqlite3.Error as exc:  # an older schema missing a column
        result["status"] = "unreadable"
        result["error"] = str(exc)
        return result
    result.update(found)
    for inv in m.invariants:
        if not all(_table_exists(conn, t) for t in inv.tables):
            result["invariants"].append(
                {"name": inv.name, "status": "table_absent", "violations": None}
            )
            continue
        try:
            row = conn.execute(inv.sql).fetchone()
        except sqlite3.Error as exc:
            result["invariants"].append(
                {"name": inv.name, "status": "unreadable", "error": str(exc)}
            )
            continue
        n = (row[0] if row else 0) or 0
        result["invariants"].append(
            {
                "name": inv.name,
                "description": inv.description,
                "status": "ok" if n == 0 else "violated",
                "violations": n,
            }
        )
    if result["undeclared"] or any(
        i["status"] == "violated" for i in result["invariants"]
    ):
        result["status"] = "findings"
    return result


def audit(conn: sqlite3.Connection) -> dict[str, Any]:
    """Audit every registered machine. Returns plain data."""
    machines = [audit_machine(conn, m) for m in MACHINES]
    findings = [m["machine"] for m in machines if m["status"] == "findings"]
    unreadable = [
        m["machine"]
        for m in machines
        if m["status"] == "unreadable"
        or any(i["status"] == "unreadable" for i in m["invariants"])
    ]
    return {
        "machines_checked": sum(
            1 for m in machines if m["status"] in ("ok", "findings")
        ),
        "machines_with_findings": findings,
        "machines_unreadable": unreadable,
        "machines": machines,
    }
