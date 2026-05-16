"""Tests for scout.narrative.agent._run_extra_table_prune.

V1#1 + V1#2 + V1#8 review fold: structured logging replaces silent except
for the 6 narrative-pruned tables.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog


async def test_run_extra_table_prune_logs_per_table_error():
    """V1#8 fold: when EVERY table DELETE fails, expect EXACTLY 6 structured
    error events (one per remaining table after score/volume extraction).
    Fault isolation: errors don't break the loop.
    """
    db = MagicMock()
    db._conn = MagicMock()
    db._conn.execute = AsyncMock(side_effect=RuntimeError("simulated table missing"))
    db._conn.commit = AsyncMock()

    from scout.narrative.agent import _run_extra_table_prune

    with structlog.testing.capture_logs() as cap_logs:
        await _run_extra_table_prune(db)

    error_logs = [e for e in cap_logs if e.get("event") == "extra_prune_table_error"]
    assert len(error_logs) == 6, (
        f"Expected 6 (one per narrative-owned table), got {len(error_logs)}"
    )
    seen_tables = {e["table"] for e in error_logs}
    assert seen_tables == {
        "volume_spikes",
        "momentum_7d",
        "trending_snapshots",
        "learn_logs",
        "chain_matches",
        "holder_snapshots",
    }


async def test_run_extra_table_prune_does_not_include_score_volume():
    """Regression test: score_history and volume_snapshots must NOT be pruned
    here — they're owned by scout.main._run_hourly_maintenance now.
    """
    db = MagicMock()
    db._conn = MagicMock()
    db._conn.execute = AsyncMock()
    db._conn.commit = AsyncMock()

    from scout.narrative.agent import _run_extra_table_prune

    await _run_extra_table_prune(db)

    executed_sql = [
        call.args[0] for call in db._conn.execute.call_args_list if call.args
    ]
    for stmt in executed_sql:
        assert "score_history" not in stmt, (
            f"score_history must not be pruned here: {stmt}"
        )
        assert "volume_snapshots" not in stmt, (
            f"volume_snapshots must not be pruned here: {stmt}"
        )


async def test_run_extra_table_prune_commit_error_is_structured():
    """If commit() raises, the helper must logger.exception, not silent-swallow."""
    db = MagicMock()
    db._conn = MagicMock()
    db._conn.execute = AsyncMock()
    db._conn.commit = AsyncMock(side_effect=RuntimeError("commit failed"))

    from scout.narrative.agent import _run_extra_table_prune

    with structlog.testing.capture_logs() as cap_logs:
        await _run_extra_table_prune(db)

    commit_errors = [
        e for e in cap_logs if e.get("event") == "extra_prune_commit_error"
    ]
    assert len(commit_errors) == 1
