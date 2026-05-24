"""Round 7 static lint: load-bearing asserts in critical paths must be
replaced with explicit raises.

`python -O` strips `assert` statements. Two sites previously used asserts
to enforce invariants whose violation would cause harmful behavior
under -O:

  scout/db.py: SQL identifier allowlist guard — without it, an f-string
  SELECT smuggled by a future caller would execute unchecked.

  scout/live/binance_adapter.py: post-retry-loop "unreachable" guard —
  without it, a control-flow bug emptying _BACKOFFS would result in
  `raise None` (TypeError) instead of a meaningful error.

This test asserts both sites now use explicit raises.

Type-narrowing asserts (`assert self._db._conn is not None`) are NOT
flagged: under -O, the next-line attribute access on the now-None object
would AttributeError, so they're equivalent failure modes — just earlier
diagnostic on debug runs.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PY = REPO_ROOT / "scout" / "db.py"
BINANCE_PY = REPO_ROOT / "scout" / "live" / "binance_adapter.py"


def test_db_coin_id_resolves_uses_explicit_raise_for_sql_allowlist():
    src = DB_PY.read_text(encoding="utf-8")
    # The original load-bearing assert had this exact start.
    assert "assert table in _ALLOWED_TABLES" not in src, (
        "scout/db.py reintroduced `assert table in _ALLOWED_TABLES`. "
        "This is a SQL-injection guard for f-string SELECT; under "
        "`python -O` it is stripped, silently admitting any future-added "
        "table name. Replace with `if table not in _ALLOWED_TABLES: "
        "raise ValueError(...)` so the guard survives optimisation."
    )
    # New shape MUST be present.
    assert "if table not in _ALLOWED_TABLES:" in src, (
        "scout/db.py SQL identifier guard missing — expected `if table "
        "not in _ALLOWED_TABLES: raise ValueError(...)`"
    )


def test_binance_adapter_retry_loop_uses_explicit_raise():
    src = BINANCE_PY.read_text(encoding="utf-8")
    assert "assert last_exc is not None" not in src, (
        "scout/live/binance_adapter.py reintroduced `assert last_exc is "
        "not None`. Under `python -O` this is stripped and the next "
        "`raise last_exc` becomes `raise None` (TypeError), masking the "
        "real bug. Replace with an explicit `if last_exc is None: raise "
        "RuntimeError(...)`."
    )
    assert "if last_exc is None:" in src, (
        "binance_adapter retry-loop post-exit guard missing"
    )


def test_update_narrative_strategy_wraps_in_try_except():
    import inspect
    from dashboard import api as dashboard_api

    src = inspect.getsource(dashboard_api.create_app)
    assert "update_narrative_strategy_failed" in src, (
        "PUT /api/narrative/strategy/{key} must catch DB errors via "
        "`_log.exception('update_narrative_strategy_failed', ...)` + "
        "return 500 JSON. Bare DB calls leak SQLite stack traces."
    )
