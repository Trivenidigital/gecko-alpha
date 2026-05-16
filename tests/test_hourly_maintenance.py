"""Integration tests for _run_hourly_maintenance (V1#7 fold).

Isolated from tests/test_main.py to avoid OPENSSL_Uplink trigger on Windows
local dev (memory reference_windows_openssl_workaround.md). Imports
_run_hourly_maintenance directly rather than going through the full
run_pipeline path.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from scout.config import Settings
from scout.main import _run_hourly_maintenance


def _make_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        DB_PATH=tmp_path / "scout.db",
    )


def _make_db_mock(score_pruned: int = 0, volume_pruned: int = 0) -> MagicMock:
    db = MagicMock()
    db.prune_old_candidates = AsyncMock(return_value=0)
    db.prune_perp_anomalies = AsyncMock(return_value=0)
    db.prune_cryptopanic_posts = AsyncMock(return_value=0)
    db.prune_score_history = AsyncMock(return_value=score_pruned)
    db.prune_volume_snapshots = AsyncMock(return_value=volume_pruned)
    return db


async def test_run_hourly_maintenance_calls_score_history_prune(tmp_path):
    """V1#7 fold: _run_hourly_maintenance must call prune_score_history with
    the configured retention setting."""
    settings = _make_settings(tmp_path)
    db = _make_db_mock()
    session = MagicMock()
    logger = MagicMock()

    # check_outcomes will fail without real DB — caught by outer try in helper
    await _run_hourly_maintenance(db, session, settings, logger)

    db.prune_score_history.assert_awaited_once_with(
        keep_days=settings.SCORE_HISTORY_RETENTION_DAYS
    )


async def test_run_hourly_maintenance_calls_volume_snapshots_prune(tmp_path):
    settings = _make_settings(tmp_path)
    db = _make_db_mock()
    session = MagicMock()
    logger = MagicMock()

    await _run_hourly_maintenance(db, session, settings, logger)

    db.prune_volume_snapshots.assert_awaited_once_with(
        keep_days=settings.VOLUME_SNAPSHOTS_RETENTION_DAYS
    )


async def test_run_hourly_maintenance_logs_info_when_rows_pruned(tmp_path):
    """V4#4 fold: info-when-rows>0 pattern matches cryptopanic at main.py:1747."""
    settings = _make_settings(tmp_path)
    db = _make_db_mock(score_pruned=42, volume_pruned=7)
    session = MagicMock()
    logger = MagicMock()

    await _run_hourly_maintenance(db, session, settings, logger)

    info_events = [
        call.args[0] for call in logger.info.call_args_list if call.args
    ]
    assert "score_history_pruned" in info_events
    assert "volume_snapshots_pruned" in info_events


async def test_run_hourly_maintenance_silent_when_zero_rows(tmp_path):
    """V4#4 fold: silent when rows_deleted == 0 (no info OR debug emit)."""
    settings = _make_settings(tmp_path)
    db = _make_db_mock(score_pruned=0, volume_pruned=0)
    session = MagicMock()
    logger = MagicMock()

    await _run_hourly_maintenance(db, session, settings, logger)

    info_events = [
        call.args[0] for call in logger.info.call_args_list if call.args
    ]
    assert "score_history_pruned" not in info_events
    assert "volume_snapshots_pruned" not in info_events
    # No debug call either (no level filter at startup)
    debug_events = [
        call.args[0] for call in logger.debug.call_args_list if call.args
    ]
    assert "score_history_pruned" not in debug_events
    assert "volume_snapshots_pruned" not in debug_events


async def test_run_hourly_maintenance_exception_path_logs_structured(tmp_path):
    """logger.exception('score_history_prune_failed') on exception, not silent."""
    settings = _make_settings(tmp_path)
    db = _make_db_mock()
    db.prune_score_history = AsyncMock(side_effect=RuntimeError("simulated"))
    session = MagicMock()
    logger = MagicMock()

    # Must not raise out of helper — exception is swallowed + logged
    await _run_hourly_maintenance(db, session, settings, logger)

    exception_events = [
        call.args[0] for call in logger.exception.call_args_list if call.args
    ]
    assert "score_history_prune_failed" in exception_events
