#!/usr/bin/env python3
"""gecko-query: python3 query.py "SELECT ..." -> JSON lines. Stdlib only.
ONE read-only SELECT over the nightly snapshot. Exit 0 ok, 1 error, 2 refused."""

import itertools
import json
import re
import sqlite3
import sys
from contextlib import closing

DB_URI = "file:/var/lib/gecko/gecko-snapshot.db?mode=ro&immutable=1"
ALLOWED = frozenset(
    "tokens observations watchlist forward_returns research_notes runs source_calls".split()
)
MAX_ROWS = 200
MAX_VM_STEPS = 200_000_000  # progress-handler ticks x 1000: a CPU bound
FORBIDDEN = re.compile(
    r"\b(ATTACH|DETACH|PRAGMA|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|VACUUM)\b",
    re.I,
)
# the comma-separated source list after FROM/JOIN, up to the next keyword or paren
SOURCES = re.compile(
    r"\b(?:FROM|JOIN)\b(.*?)(?=\b(?:SELECT|FROM|WHERE|GROUP|ORDER|LIMIT|HAVING|WINDOW"
    r"|UNION|EXCEPT|INTERSECT|ON|USING|LEFT|RIGHT|FULL|INNER|OUTER|CROSS|NATURAL"
    r"|JOIN)\b|[()]|$)",
    re.I | re.S,
)


def refusal(sql: str, other_tables: frozenset[str] = frozenset()) -> str | None:
    """Why ``sql`` is refused, or None. Naming any of ``other_tables`` anywhere refuses."""
    s = sql.strip()
    if not re.match(r"SELECT\b", s, re.I):
        return "must be a single statement starting with SELECT"
    if ";" in s or "--" in s or "/*" in s:
        return "no ';' and no comments"
    if m := FORBIDDEN.search(s):
        return f"forbidden token {m.group(1).upper()}"
    for src in SOURCES.findall(s):
        for item in src.split(","):
            if words := item.strip().split():
                name = words[0].strip('"`[]').lower()
                if name not in ALLOWED:
                    return f"table {name!r} not in the allow-list {sorted(ALLOWED)}"
    idents = {w.lower() for w in re.findall(r"[A-Za-z_]\w*", s)}
    if bad := sorted(
        w for w in idents if w in other_tables or w.startswith(("sqlite_", "pragma_"))
    ):
        return f"not in the allow-list: {bad}"
    return None


def run(sql: str, uri: str = DB_URI) -> int:
    try:
        if why := refusal(sql):  # before any open: refusals need no snapshot
            raise PermissionError(why)
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            q = "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            others = frozenset(r[0].lower() for r in conn.execute(q)) - ALLOWED
            if why := refusal(sql, others):
                raise PermissionError(why)
            if not re.search(r"\bLIMIT\b", sql, re.I):
                sql = f"{sql.strip()} LIMIT {MAX_ROWS}"
            ticks = itertools.count()  # a runaway query aborts "interrupted" (rc 1)
            conn.set_progress_handler(lambda: next(ticks) * 1000 > MAX_VM_STEPS, 1000)
            conn.row_factory = sqlite3.Row
            for row in conn.execute(sql).fetchmany(MAX_ROWS):
                print(json.dumps(dict(row), default=str))
        return 0
    except PermissionError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"query error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1]) if len(sys.argv) == 2 else 2)
