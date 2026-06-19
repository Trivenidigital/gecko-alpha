"""Prospective-watchlist freshness watchdog (Task 5)."""

import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import structlog

import scout
import scout.conviction.watchlist_watchdog as w

NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


class FakeDB:
    def __init__(self, run_at, status="ok"):
        self._run = None if run_at is None else {"run_at": run_at, "status": status}

    async def latest_conviction_watchlist_run(self):
        return self._run


def _install_fake_alerter(monkeypatch, send_mock):
    fake = types.ModuleType("scout.alerter")
    fake.send_telegram_message = send_mock
    monkeypatch.setitem(sys.modules, "scout.alerter", fake)
    monkeypatch.setattr(scout, "alerter", fake, raising=False)


def _events(logs):
    return [e["event"] for e in logs]


async def test_fresh_run_is_ok_no_alert(monkeypatch, settings_factory):
    send = AsyncMock()
    _install_fake_alerter(monkeypatch, send)
    w._reset_for_tests()
    db = FakeDB((NOW - timedelta(minutes=10)).isoformat())
    status = await w.check_watchlist_freshness(
        db, object(), settings_factory(), structlog.get_logger(), now=NOW
    )
    assert status == "ok"
    send.assert_not_awaited()


async def test_stale_run_is_down_and_alerts(monkeypatch, settings_factory):
    send = AsyncMock()
    _install_fake_alerter(monkeypatch, send)
    w._reset_for_tests()
    db = FakeDB((NOW - timedelta(hours=10)).isoformat())  # > 180m SLO
    with structlog.testing.capture_logs() as logs:
        status = await w.check_watchlist_freshness(
            db, object(), settings_factory(), structlog.get_logger(), now=NOW
        )
    assert status == "down"
    ev = _events(logs)
    assert "conviction_watchlist_snapshot_stale" in ev
    assert "conviction_watchlist_alert_dispatched" in ev
    assert "conviction_watchlist_alert_delivered" in ev
    send.assert_awaited_once()
    assert send.await_args.kwargs.get("parse_mode") is None
    assert send.await_args.kwargs.get("raise_on_failure") is True


async def test_fresh_but_failed_status_is_down_and_alerts(
    monkeypatch, settings_factory
):
    """P1 fold: a fresh run_at with a non-ok status (builder crashed / fail-closed)
    is a real DOWN — not healthy just because the heartbeat is recent."""
    send = AsyncMock()
    _install_fake_alerter(monkeypatch, send)
    w._reset_for_tests()
    db = FakeDB((NOW - timedelta(minutes=5)).isoformat(), status="failed")
    with structlog.testing.capture_logs() as logs:
        status = await w.check_watchlist_freshness(
            db, object(), settings_factory(), structlog.get_logger(), now=NOW
        )
    assert status == "down"
    assert "conviction_watchlist_alert_dispatched" in _events(logs)
    send.assert_awaited_once()


async def test_never_run_is_unknown_no_alert(monkeypatch, settings_factory):
    send = AsyncMock()
    _install_fake_alerter(monkeypatch, send)
    w._reset_for_tests()
    db = FakeDB(None)
    status = await w.check_watchlist_freshness(
        db, object(), settings_factory(), structlog.get_logger(), now=NOW
    )
    assert status == "unknown"
    send.assert_not_awaited()


async def test_stale_alert_deduped_until_healthy(monkeypatch, settings_factory):
    send = AsyncMock()
    _install_fake_alerter(monkeypatch, send)
    w._reset_for_tests()
    stale = FakeDB((NOW - timedelta(hours=10)).isoformat())
    s = settings_factory()
    await w.check_watchlist_freshness(
        stale, object(), s, structlog.get_logger(), now=NOW
    )
    await w.check_watchlist_freshness(
        stale, object(), s, structlog.get_logger(), now=NOW
    )
    send.assert_awaited_once()  # second stale check deduped
    # a healthy run re-arms the alert
    fresh = FakeDB((NOW - timedelta(minutes=5)).isoformat())
    await w.check_watchlist_freshness(
        fresh, object(), s, structlog.get_logger(), now=NOW
    )
    await w.check_watchlist_freshness(
        stale, object(), s, structlog.get_logger(), now=NOW
    )
    assert send.await_count == 2  # alerted again after re-arm
