"""Tests for dashboard.db.get_outcomes_by_token_ids — the read-only join
surface that lets Top Gainers / candidate views show "linked paper_trade"
badges without re-implementing the actionability classifier.

Pure read-only helper; no behavior change in scope.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from dashboard import db as dashdb
from scout.db import Database


@pytest.fixture
async def scout_db(tmp_path):
    db_path = tmp_path / "scout.db"
    d = Database(db_path)
    await d.initialize()
    yield d, str(db_path)
    await d.close()


async def _insert_trade(
    conn,
    token_id: str,
    *,
    opened_at: str,
    status: str = "open",
    actionable: int | None = None,
    actionability_reason: str | None = None,
    actionability_version: str | None = "v1",
    pnl_usd: float | None = None,
    signal_type: str = "narrative_prediction",
) -> int:
    cur = await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, pnl_usd, pnl_pct, opened_at, closed_at,
            actionable, actionability_reason, actionability_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            token_id.upper()[:8],
            token_id.title(),
            "coingecko",
            signal_type,
            json.dumps({}),
            100.0,
            1000.0,
            10.0,
            20.0,
            10.0,
            120.0,
            90.0,
            status,
            pnl_usd,
            None,
            opened_at,
            None if status == "open" else opened_at,
            actionable,
            actionability_reason,
            actionability_version if actionable is not None else None,
        ),
    )
    await conn.commit()
    return cur.lastrowid


async def test_empty_input_returns_empty_dict(scout_db):
    """Empty token_ids → empty dict (no SQL executed, no errors)."""
    _, path = scout_db
    result = await dashdb.get_outcomes_by_token_ids(path, [])
    assert result == {}


async def test_unlinked_token_absent_from_result(scout_db):
    """Token with no paper_trade row is absent from the result dict.

    Caller defaults to None and renders an 'unlinked / no outcome yet' badge.
    """
    _, path = scout_db
    result = await dashdb.get_outcomes_by_token_ids(path, ["bitcoin", "dogecoin"])
    assert result == {}


async def test_linked_token_returns_outcome_shape(scout_db):
    """A linked token returns the full outcome shape."""
    d, path = scout_db
    opened = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    trade_id = await _insert_trade(
        d._conn,
        "ethereum",
        opened_at=opened,
        status="open",
        actionable=1,
        actionability_reason="v1_pass_core_signal_mcap_10_50m",
    )
    result = await dashdb.get_outcomes_by_token_ids(path, ["ethereum"])
    assert "ethereum" in result
    row = result["ethereum"]
    assert row["paper_trade_id"] == trade_id
    assert row["status"] == "open"
    assert row["actionable"] == 1
    assert row["actionability_reason"] == "v1_pass_core_signal_mcap_10_50m"
    assert row["actionability_version"] == "v1"
    assert row["pnl_usd"] is None  # still open
    assert row["opened_at"] == opened


async def test_returns_most_recent_trade_per_token(scout_db):
    """When a token has multiple paper_trades, the most recent (opened_at DESC)
    wins. This matters for re-traded tokens where the freshest event is what
    the dashboard should surface.
    """
    d, path = scout_db
    older = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    newer = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _insert_trade(
        d._conn,
        "ethereum",
        opened_at=older,
        status="closed_tp",
        actionable=0,
        actionability_reason="v1_block_gainers_early_confluence_3",
        pnl_usd=-50.0,
    )
    newest_id = await _insert_trade(
        d._conn,
        "ethereum",
        opened_at=newer,
        status="open",
        actionable=1,
        actionability_reason="v1_pass_core_signal_mcap_10_50m",
    )
    result = await dashdb.get_outcomes_by_token_ids(path, ["ethereum"])
    assert result["ethereum"]["paper_trade_id"] == newest_id
    assert result["ethereum"]["actionable"] == 1
    assert result["ethereum"]["actionability_reason"] == "v1_pass_core_signal_mcap_10_50m"


async def test_closed_trade_returns_pnl(scout_db):
    """A closed trade returns its pnl_usd."""
    d, path = scout_db
    opened = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    await _insert_trade(
        d._conn,
        "solana",
        opened_at=opened,
        status="closed_tp",
        actionable=0,
        actionability_reason="v1_block_losers_contrarian_exploratory",
        pnl_usd=125.50,
    )
    result = await dashdb.get_outcomes_by_token_ids(path, ["solana"])
    assert result["solana"]["status"] == "closed_tp"
    assert result["solana"]["pnl_usd"] == 125.50
    assert result["solana"]["actionable"] == 0


async def test_mixed_linked_and_unlinked(scout_db):
    """A mix of linked + unlinked token_ids — only linked appear in result."""
    d, path = scout_db
    opened = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    await _insert_trade(
        d._conn,
        "ethereum",
        opened_at=opened,
        actionable=1,
        actionability_reason="v1_pass_core_signal_mcap_10_50m",
    )
    await _insert_trade(
        d._conn,
        "solana",
        opened_at=opened,
        actionable=None,  # unstamped pre-cutover
        actionability_reason=None,
        actionability_version=None,
    )
    result = await dashdb.get_outcomes_by_token_ids(
        path, ["ethereum", "solana", "dogecoin", "bitcoin"]
    )
    assert set(result.keys()) == {"ethereum", "solana"}
    assert result["ethereum"]["actionable"] == 1
    assert result["solana"]["actionable"] is None  # unknown cohort
    assert result["solana"]["actionability_reason"] is None
