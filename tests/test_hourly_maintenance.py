"""Integration tests for _run_hourly_maintenance (V1#7 fold).

Isolated from tests/test_main.py to avoid OPENSSL_Uplink trigger on Windows
local dev (memory reference_windows_openssl_workaround.md). Imports
_run_hourly_maintenance directly rather than going through the full
run_pipeline path.
"""

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
    # BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION cycle 2: 6 new prune methods
    db.prune_volume_spikes = AsyncMock(return_value=0)
    db.prune_momentum_7d = AsyncMock(return_value=0)
    db.prune_trending_snapshots = AsyncMock(return_value=0)
    db.prune_learn_logs = AsyncMock(return_value=0)
    db.prune_chain_matches = AsyncMock(return_value=0)
    db.prune_holder_snapshots = AsyncMock(return_value=0)
    # BL-NEW-SQLITE-WAL-PROFILE cycle 4: probe_wal_state hook
    db.probe_wal_state = AsyncMock(return_value={
        "wal_size_bytes": 1024,
        "wal_pages": 0,
        "shm_size_bytes": 32768,
        "db_size_bytes": 4096,
        "page_count": 1,
        "page_size": 4096,
        "freelist_count": 0,
        "journal_mode": "wal",
        "wal_autocheckpoint": 1000,
    })
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


@pytest.mark.parametrize(
    "prune_method,retention_attr",
    [
        ("prune_volume_spikes", "VOLUME_SPIKES_RETENTION_DAYS"),
        ("prune_momentum_7d", "MOMENTUM_7D_RETENTION_DAYS"),
        ("prune_trending_snapshots", "TRENDING_SNAPSHOTS_RETENTION_DAYS"),
        ("prune_learn_logs", "LEARN_LOGS_RETENTION_DAYS"),
        ("prune_chain_matches", "CHAIN_MATCHES_RETENTION_DAYS"),
        ("prune_holder_snapshots", "HOLDER_SNAPSHOTS_RETENTION_DAYS"),
    ],
)
async def test_run_hourly_maintenance_calls_narrative_table_prune(
    tmp_path, prune_method, retention_attr
):
    """BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION cycle 2: _run_hourly_maintenance
    must call each of the 6 new prune methods with the configured Settings
    retention.
    """
    settings = _make_settings(tmp_path)
    db = _make_db_mock()
    session = MagicMock()
    logger = MagicMock()

    await _run_hourly_maintenance(db, session, settings, logger)

    expected_keep_days = getattr(settings, retention_attr)
    getattr(db, prune_method).assert_awaited_once_with(keep_days=expected_keep_days)


@pytest.mark.parametrize(
    "failing_method,event_base,subsequent_methods",
    [
        (
            "prune_volume_spikes",
            "volume_spikes",
            [
                "prune_momentum_7d",
                "prune_trending_snapshots",
                "prune_learn_logs",
                "prune_chain_matches",
                "prune_holder_snapshots",
            ],
        ),
        (
            "prune_momentum_7d",
            "momentum_7d",
            [
                "prune_trending_snapshots",
                "prune_learn_logs",
                "prune_chain_matches",
                "prune_holder_snapshots",
            ],
        ),
        (
            "prune_trending_snapshots",
            "trending_snapshots",
            ["prune_learn_logs", "prune_chain_matches", "prune_holder_snapshots"],
        ),
        (
            "prune_learn_logs",
            "learn_logs",
            ["prune_chain_matches", "prune_holder_snapshots"],
        ),
        ("prune_chain_matches", "chain_matches", ["prune_holder_snapshots"]),
        ("prune_holder_snapshots", "holder_snapshots", []),
    ],
)
async def test_narrative_prune_loop_fault_isolation(
    tmp_path, failing_method, event_base, subsequent_methods
):
    """V11 PR-review MUST-FIX: cycle 2's tight-loop pattern in
    _run_hourly_maintenance is NEW; one prune raising must NOT halt the
    subsequent prunes in the loop.

    For each of the 6 new prune methods, inject side_effect=RuntimeError
    and verify:
      (a) logger.exception emits f'{event_base}_prune_failed' for the failing one
      (b) every subsequent method in the loop is still awaited
    """
    settings = _make_settings(tmp_path)
    db = _make_db_mock()
    setattr(db, failing_method, AsyncMock(side_effect=RuntimeError("simulated")))
    session = MagicMock()
    logger = MagicMock()

    await _run_hourly_maintenance(db, session, settings, logger)

    # (a) failure is logged structurally
    exception_events = [
        call.args[0] for call in logger.exception.call_args_list if call.args
    ]
    assert f"{event_base}_prune_failed" in exception_events, (
        f"Expected '{event_base}_prune_failed' in {exception_events}"
    )

    # (b) subsequent methods in the loop were still awaited
    for method_name in subsequent_methods:
        getattr(db, method_name).assert_awaited(), (
            f"Loop halted after {failing_method} raised; {method_name} not called"
        )


async def test_run_hourly_maintenance_emits_sqlite_wal_probe_when_enabled(tmp_path):
    """BL-NEW-SQLITE-WAL-PROFILE cycle 4: probe fires at DEBUG once per hour."""
    settings = _make_settings(tmp_path)
    db = _make_db_mock()
    session = MagicMock()
    logger = MagicMock()

    await _run_hourly_maintenance(db, session, settings, logger)

    db.probe_wal_state.assert_awaited_once()
    debug_events = [
        (call.args[0], call.kwargs)
        for call in logger.debug.call_args_list
        if call.args
    ]
    probe_calls = [(evt, kw) for evt, kw in debug_events if evt == "sqlite_wal_probe"]
    assert len(probe_calls) == 1
    assert probe_calls[0][1]["wal_size_bytes"] == 1024
    assert probe_calls[0][1]["journal_mode"] == "wal"


async def test_run_hourly_maintenance_emits_bloat_above_threshold(tmp_path):
    """Bloat event fires only when wal_size_bytes > SQLITE_WAL_BLOAT_BYTES."""
    settings = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        DB_PATH=tmp_path / "scout.db",
        SQLITE_WAL_BLOAT_BYTES=1000,  # tiny threshold for test
    )
    db = _make_db_mock()
    db.probe_wal_state = AsyncMock(return_value={
        "wal_size_bytes": 1_000_000,
        "wal_pages": 244,
        "shm_size_bytes": 32768,
        "db_size_bytes": 4096,
        "page_count": 1,
        "page_size": 4096,
        "freelist_count": 0,
        "journal_mode": "wal",
        "wal_autocheckpoint": 1000,
    })
    session = MagicMock()
    logger = MagicMock()

    await _run_hourly_maintenance(db, session, settings, logger)

    warning_events = [
        (call.args[0], call.kwargs)
        for call in logger.warning.call_args_list
        if call.args
    ]
    bloat = [
        (evt, kw) for evt, kw in warning_events if evt == "sqlite_wal_bloat_observed"
    ]
    assert len(bloat) == 1
    assert bloat[0][1]["wal_size_bytes"] == 1_000_000
    assert bloat[0][1]["threshold_bytes"] == 1000


async def test_run_hourly_maintenance_skips_wal_probe_when_disabled(tmp_path):
    """SQLITE_WAL_PROFILE_ENABLED=False — probe is not called."""
    settings = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        DB_PATH=tmp_path / "scout.db",
        SQLITE_WAL_PROFILE_ENABLED=False,
    )
    db = _make_db_mock()
    db.probe_wal_state = AsyncMock()
    session = MagicMock()
    await _run_hourly_maintenance(db, session, settings, MagicMock())
    db.probe_wal_state.assert_not_called()


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
