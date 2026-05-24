"""Regression tests for the dashboard production-hardening batch.

Covers five surfaces audited from the post-autodev review (PR #242):

1. /api/x_alerts limit bounded to le=500 (was unbounded)
2. /api/secondwave/candidates days/limit bounded (was bare defaults)
3. /api/secondwave/stats days bounded
4. /api/trading/close/{trade_id} wraps DB failures in 500-JSON, not stack trace
5. _table_stats() validates identifiers via regex (prevents future SQL injection)
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from dashboard import api as dashboard_api
from dashboard import db as dashboard_db


def test_x_alerts_limit_has_upper_bound():
    """`limit` Query must declare le=N to prevent unbounded SQL queries."""
    src = inspect.getsource(dashboard_api.build_app)
    assert "Query(80, ge=1, le=500)" in src, (
        "/api/x_alerts must declare le=500 on limit; without it an attacker "
        "can request ?limit=999999999 and trigger an unbounded SQL scan"
    )


def test_secondwave_candidates_has_bounds():
    src = inspect.getsource(dashboard_api.build_app)
    # Both days and limit on /api/secondwave/candidates
    assert "Query(7, ge=1, le=90)" in src, (
        "/api/secondwave/{candidates,stats} must bound days to ge=1 le=90"
    )
    assert "Query(50, ge=1, le=500)" in src, (
        "/api/secondwave/candidates must bound limit to ge=1 le=500"
    )


def test_close_trade_has_try_except():
    """Manual close endpoint must wrap DB calls so failures don't 500-stack to UI."""
    src = inspect.getsource(dashboard_api.build_app)
    # The close_trade body must contain a try/except returning JSONResponse 500.
    # Use a fingerprint that survives benign reformatting.
    assert "close_trade_failed" in src, (
        "close_trade endpoint must log exceptions via _log.exception("
        "'close_trade_failed', ...)"
    )
    assert '"error": "internal_error"' in src, (
        "close_trade endpoint must return structured 500 JSON on DB failure"
    )


def test_table_stats_rejects_non_identifier_table():
    """SQL identifier validation: reject anything that's not [a-zA-Z_][a-zA-Z0-9_]*."""

    async def _run():
        # Conn arg is irrelevant — ValueError is raised before DB access.
        for bad in [
            "users; DROP TABLE x",
            "candidates WHERE 1=1",
            "candidates--",
            "",
            " candidates",
            "1candidates",
        ]:
            with pytest.raises(ValueError, match="invalid table identifier"):
                await dashboard_db._table_stats(None, bad, "created_at")

    asyncio.get_event_loop().run_until_complete(_run()) if False else asyncio.run(_run())


def test_table_stats_rejects_non_identifier_column():
    async def _run():
        for bad in ["created_at; DROP", "MAX(*)", "1", "", " created_at"]:
            with pytest.raises(ValueError, match="invalid column identifier"):
                await dashboard_db._table_stats(None, "candidates", bad)

    asyncio.run(_run())


def test_table_stats_accepts_valid_identifiers():
    """A valid identifier passes the regex guard — actual DB error is separate."""

    async def _run():
        # Pass None conn; the guard runs FIRST, then it would crash on conn.execute
        # We catch the AttributeError that comes from conn=None.
        try:
            await dashboard_db._table_stats(None, "candidates", "first_seen_at")
            raise AssertionError("expected DB error, not validation error")
        except ValueError:
            raise  # would mean guard rejected valid identifier
        except (AttributeError, TypeError):
            pass  # expected: guard passed, then DB call failed on None conn

    asyncio.run(_run())
