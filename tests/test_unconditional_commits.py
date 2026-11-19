"""Commits on the shared connection must not be conditional.

This pins STRUCTURE, deliberately, because behaviour cannot distinguish it
under the connection mode the app currently uses. That is the whole finding: a
guarded commit is safe only while a bare SELECT opens no transaction, which is
a property of `isolation_level=''` (legacy) rather than of the code. Under
`autocommit=False` -- PEP 249 strict, available since 3.12, and this repo runs
3.14 -- the guard SELECT DOES open a transaction, and the nothing-to-do path
then leaves it dangling.

The failure that produces is quiet. A dangling READ transaction on the shared
connection pins the WAL snapshot, so the hourly `wal_checkpoint(TRUNCATE)`
returns BUSY and reclaims zero pages. It surfaces as unexplained WAL growth
nowhere near the function that caused it, on a box already at 90% disk.

A behavioural test would pass today with the guard restored, so it would pin
nothing. An AST assertion pins what actually matters: the commit is reached on
every path.
"""

import ast
from pathlib import Path

import pytest

DB_PY = Path(__file__).resolve().parents[1] / "scout" / "db.py"

#: The two functions that carried the hazardous shape. (Review named the
#: second `update_price_cache`; the function is actually `cache_prices` --
#: verified by AST, since the name in a report is not evidence the symbol
#: exists.)
FUNCTIONS = ["archive_legacy_prefix_comparisons", "cache_prices"]


def _functions(tree):
    return {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _is_commit(stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, (ast.Await, ast.Call))
        and isinstance(
            call := (
                stmt.value.value if isinstance(stmt.value, ast.Await) else stmt.value
            ),
            ast.Call,
        )
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "commit"
    )


def _trailing_guarded_commits(fn) -> list[int]:
    """The specific hazardous shape: `if <x>: commit()` as the last work done.

    Deliberately NOT "any commit inside an if". An AST sweep for that finds ten
    more sites in this file -- migrations that commit in one branch, functions
    that commit and return early -- and those are fine: they commit on the path
    that opened a transaction. The hazard is a function that may fall through
    to `return` having opened one and never committed, which is what a trailing
    guard produces.
    """
    out = []
    body = fn.body
    for i, stmt in enumerate(body):
        if not isinstance(stmt, ast.If) or stmt.orelse:
            continue
        if not (len(stmt.body) == 1 and _is_commit(stmt.body[0])):
            continue
        rest = body[i + 1 :]
        if all(isinstance(r, ast.Return) for r in rest):
            out.append(stmt.body[0].lineno)
    return out


@pytest.mark.parametrize("name", FUNCTIONS)
def test_the_commit_is_reached_on_every_path(name):
    tree = ast.parse(DB_PY.read_text(encoding="utf-8"))
    fn = _functions(tree)[name]

    calls = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "commit"
    ]
    assert calls, f"{name} no longer commits at all"

    guarded = _trailing_guarded_commits(fn)
    assert guarded == [], (
        f"{name} has a commit at line(s) {guarded} nested inside an `if`. "
        "A guarded commit leaves a transaction dangling on the shared "
        "connection under autocommit=False, which pins the WAL snapshot and "
        "makes the hourly checkpoint reclaim nothing."
    )


def test_no_new_trailing_guarded_commits_appear():
    """Keep the shape from coming back anywhere in db.py.

    Enumerated by AST, not by grepping for `if count:`. The grep that produced
    the original "closed set of two" only matched two spellings; the guard can
    be written any number of ways, and a third would not have been seen.
    """
    tree = ast.parse(DB_PY.read_text(encoding="utf-8"))
    offenders = {
        name: lines
        for name, fn in _functions(tree).items()
        if (lines := _trailing_guarded_commits(fn))
    }
    assert (
        offenders == {}
    ), f"new trailing guarded commit(s) on the shared connection: {offenders}"
