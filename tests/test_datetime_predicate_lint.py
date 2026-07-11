"""CI guard for INF-04 / BL-DATETIME-NORMALIZATION.

Regex-scans the source tree (scout/, dashboard/, scripts/) for the recurring
day-boundary anti-pattern: a bare, un-wrapped timestamp column compared with a
range operator directly against a SQLite ``datetime('now', ...)`` bound.

Stored timestamp columns are written as Python ``.isoformat()`` ('T'-separated,
``+00:00``); ``datetime('now', ...)`` renders space-separated and tz-less. SQLite
compares them as TEXT and at character 10 ``'T'`` (0x54) > ``' '`` (0x20), so the
predicate silently degrades to a whole-day ``DATE()`` comparison on the boundary
day (off-by-one #4, ``tasks/lessons.md``).

The fix at every call-site is EITHER wrap both operands in ``datetime()`` (or
``julianday()``) so the comparison is like-for-like, OR bind an ISO-8601 cutoff
from ``scout.timeutil.sql_utc_cutoff`` and compare the bare column against ``?``.
Both forms make the anti-pattern below not match.

Allowlist is intentionally seeded EMPTY: the sweep in this PR fixed every
occurrence. A new violation fails this test with its file:line so it is caught
in review, not months later via an unrelated audit.
"""

from __future__ import annotations

import re
from pathlib import Path

# Repo root = parent of tests/.
_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("scout", "dashboard", "scripts")

# A bare column (optionally ``alias.col``) NOT wrapped in a function call,
# compared with a range operator to a ``datetime('now', ...)`` bound. Wrapped
# forms like ``datetime(col) >= datetime('now', ...)`` do not match because the
# token immediately left of the operator is ``)``, not a bare identifier.
_ANTIPATTERN = re.compile(
    r"(?<![\w.])"
    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?"  # bare column, optionally aliased
    r"\s*(?:>=|<=|>|<)\s*"  # range operator (assignment '=' excluded on purpose)
    r"datetime\(\s*['\"]now['\"]"  # unmodified 'now' bound
)

# file:line strings that are known-acceptable. Seeded empty by INF-04.
_ALLOWLIST: set[str] = set()


def _iter_source_files():
    for d in _SCAN_DIRS:
        base = _ROOT / d
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            yield path


def test_no_unwrapped_isoformat_vs_datetime_now_predicates():
    violations: list[str] = []
    for path in _iter_source_files():
        rel = path.relative_to(_ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _ANTIPATTERN.search(line):
                key = f"{rel}:{lineno}"
                if key in _ALLOWLIST:
                    continue
                violations.append(f"{key}: {line.strip()}")

    assert not violations, (
        "Unwrapped isoformat-column vs datetime('now') predicate(s) found "
        "(INF-04 / off-by-one #4). Wrap both sides in datetime()/julianday(), "
        "or bind scout.timeutil.sql_utc_cutoff(...) and compare against ?:\n"
        + "\n".join(violations)
    )
