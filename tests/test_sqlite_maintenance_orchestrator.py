"""Orchestrator + stale-reader alert (P0 Part B, Task 5)."""

import sys
import types
from unittest.mock import AsyncMock

import structlog

import scout.observability.sqlite_maintenance as m
from scout.observability.sqlite_holder_watchdog import DbHolder


class FakeDB:
    def __init__(self, probe, ckpt=None, iv=None):
        self._probe = probe
        self._ckpt = ckpt or {"busy": 0, "log_frames": 0, "checkpointed_frames": 0}
        self._iv = iv or {
            "auto_vacuum": 2,
            "freelist_before": 0,
            "freelist_after": 0,
            "pages_reclaimed": 0,
        }
        self.iv_calls = []

    async def probe_wal_state(self):
        return self._probe

    async def checkpoint_wal_truncate(self):
        return self._ckpt

    async def run_incremental_vacuum(self, max_pages=0):
        self.iv_calls.append(max_pages)
        return self._iv


def _events(logs):
    return [e["event"] for e in logs]


async def test_busy_checkpoint_logs_warning_not_success(settings_factory):
    db = FakeDB(
        probe={"wal_size_bytes": 999_000_000, "freelist_count": 0},
        ckpt={"busy": 1, "log_frames": 10, "checkpointed_frames": 0},
    )
    s = settings_factory(
        SQLITE_STALE_READER_WATCHDOG_ENABLED=False,
        SQLITE_INCREMENTAL_VACUUM_ENABLED=False,
    )
    with structlog.testing.capture_logs() as logs:
        await m.run_sqlite_maintenance(db, object(), s, structlog.get_logger())
    ev = _events(logs)
    assert "sqlite_wal_checkpoint_busy" in ev
    assert "sqlite_wal_checkpoint_succeeded" not in ev


async def test_checkpoint_succeeded_logged_when_not_busy(settings_factory):
    db = FakeDB(
        probe={"wal_size_bytes": 999_000_000, "freelist_count": 0},
        ckpt={"busy": 0, "log_frames": 5, "checkpointed_frames": 5},
    )
    s = settings_factory(
        SQLITE_STALE_READER_WATCHDOG_ENABLED=False,
        SQLITE_INCREMENTAL_VACUUM_ENABLED=False,
    )
    with structlog.testing.capture_logs() as logs:
        await m.run_sqlite_maintenance(db, object(), s, structlog.get_logger())
    ev = _events(logs)
    assert "sqlite_wal_checkpoint_succeeded" in ev
    assert "sqlite_wal_checkpoint_busy" not in ev


async def test_incremental_vacuum_runs_when_freelist_high(settings_factory):
    db = FakeDB(
        probe={"wal_size_bytes": 0, "freelist_count": 60_000},
        iv={
            "auto_vacuum": 2,
            "freelist_before": 60_000,
            "freelist_after": 0,
            "pages_reclaimed": 60_000,
        },
    )
    s = settings_factory(
        SQLITE_STALE_READER_WATCHDOG_ENABLED=False,
        SQLITE_INCREMENTAL_VACUUM_FREELIST_THRESHOLD=50_000,
        SQLITE_INCREMENTAL_VACUUM_MAX_PAGES=200_000,
    )
    with structlog.testing.capture_logs() as logs:
        await m.run_sqlite_maintenance(db, object(), s, structlog.get_logger())
    ev = _events(logs)
    assert "sqlite_incremental_vacuum_attempted" in ev
    assert "sqlite_incremental_vacuum_completed" in ev
    assert db.iv_calls == [200_000]  # max_pages cap threaded through
    # ran_iv -> a checkpoint is forced even though wal_size is 0
    assert "sqlite_wal_checkpoint_attempted" in ev


async def test_incremental_vacuum_skipped_when_freelist_low(settings_factory):
    db = FakeDB(probe={"wal_size_bytes": 0, "freelist_count": 10})
    s = settings_factory(
        SQLITE_STALE_READER_WATCHDOG_ENABLED=False,
        SQLITE_INCREMENTAL_VACUUM_FREELIST_THRESHOLD=50_000,
    )
    with structlog.testing.capture_logs() as logs:
        await m.run_sqlite_maintenance(db, object(), s, structlog.get_logger())
    ev = _events(logs)
    assert "sqlite_incremental_vacuum_attempted" not in ev
    assert db.iv_calls == []


async def test_probe_failure_returns_early(settings_factory):
    class BadDB(FakeDB):
        async def probe_wal_state(self):
            raise RuntimeError("boom")

    db = BadDB(probe={})
    s = settings_factory()
    with structlog.testing.capture_logs() as logs:
        await m.run_sqlite_maintenance(db, object(), s, structlog.get_logger())
    assert "sqlite_maintenance_probe_failed" in _events(logs)


def _install_fake_alerter(monkeypatch, send_mock):
    fake = types.ModuleType("scout.alerter")
    fake.send_telegram_message = send_mock
    monkeypatch.setitem(sys.modules, "scout.alerter", fake)


async def test_stale_reader_alert_dispatched_and_delivered(monkeypatch, settings_factory):
    monkeypatch.setattr(
        m,
        "scan_db_holders",
        lambda *a, **k: [
            DbHolder(999, "python3 _report.py", "session-7.scope", 7 * 3600, False)
        ],
    )
    send = AsyncMock()
    _install_fake_alerter(monkeypatch, send)
    db = FakeDB(probe={"wal_size_bytes": 0, "freelist_count": 0})
    s = settings_factory(
        SQLITE_WAL_CHECKPOINT_ENABLED=False,
        SQLITE_INCREMENTAL_VACUUM_ENABLED=False,
    )
    m._reset_alert_dedup_for_tests()
    with structlog.testing.capture_logs() as logs:
        await m.run_sqlite_maintenance(db, object(), s, structlog.get_logger())
    ev = _events(logs)
    assert "sqlite_stale_reader_detected" in ev
    assert "sqlite_stale_reader_alert_dispatched" in ev
    assert "sqlite_stale_reader_alert_delivered" in ev
    send.assert_awaited_once()
    assert send.await_args.kwargs.get("parse_mode") is None
    assert send.await_args.kwargs.get("raise_on_failure") is True


async def test_stale_reader_alert_failure_not_marked_delivered(monkeypatch, settings_factory):
    """Fold 2: a non-raising send is required before logging delivered/deduping."""
    monkeypatch.setattr(
        m,
        "scan_db_holders",
        lambda *a, **k: [
            DbHolder(888, "python3 _report.py", "session-7.scope", 7 * 3600, False)
        ],
    )
    send = AsyncMock(side_effect=RuntimeError("telegram 500"))
    _install_fake_alerter(monkeypatch, send)
    db = FakeDB(probe={"wal_size_bytes": 0, "freelist_count": 0})
    s = settings_factory(
        SQLITE_WAL_CHECKPOINT_ENABLED=False,
        SQLITE_INCREMENTAL_VACUUM_ENABLED=False,
    )
    m._reset_alert_dedup_for_tests()
    with structlog.testing.capture_logs() as logs:
        await m.run_sqlite_maintenance(db, object(), s, structlog.get_logger())
    ev = _events(logs)
    assert "sqlite_stale_reader_alert_dispatched" in ev
    assert "sqlite_stale_reader_alert_delivered" not in ev
    assert 888 not in m._ALERTED_PIDS  # not deduped -> retries next run


async def test_stale_reader_alert_deduped_across_runs(monkeypatch, settings_factory):
    monkeypatch.setattr(
        m,
        "scan_db_holders",
        lambda *a, **k: [
            DbHolder(777, "python3 _report.py", "session-7.scope", 7 * 3600, False)
        ],
    )
    send = AsyncMock()
    _install_fake_alerter(monkeypatch, send)
    db = FakeDB(probe={"wal_size_bytes": 0, "freelist_count": 0})
    s = settings_factory(
        SQLITE_WAL_CHECKPOINT_ENABLED=False,
        SQLITE_INCREMENTAL_VACUUM_ENABLED=False,
    )
    m._reset_alert_dedup_for_tests()
    await m.run_sqlite_maintenance(db, object(), s, structlog.get_logger())
    await m.run_sqlite_maintenance(db, object(), s, structlog.get_logger())
    send.assert_awaited_once()  # second run deduped
