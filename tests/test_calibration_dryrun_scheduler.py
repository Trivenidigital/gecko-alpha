"""Calibrate weekly --dry-run scheduler hook tests.

Pins the new `_run_feedback_schedulers` calibration-dryrun branch added
to `scout/main.py`. Mocks `build_diffs` + `alerter.send_telegram_message`
to avoid network I/O. Dispatcher tests gated by @_SKIP_AIOHTTP for
Windows OpenSSL chain (scout.alerter triggers the same DLL conflict
that's been an issue across the session).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason="Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp tests",
)


@pytest.fixture(autouse=True)
def _reset_calibration_dryrun_sentinel():
    """adv-M1: per-test reset of `_last_calibration_dryrun_date`.
    Without this, T6 (sets sentinel) poisons T8/T0.

    On Windows + SKIP_AIOHTTP_TESTS=1, importing scout.main triggers
    the OpenSSL DLL conflict (transitive scout.alerter chain). Skip the
    reset in that case — the dispatcher tests are also @_SKIP_AIOHTTP-
    gated so the sentinel is never set on Windows; pure config tests
    (T1-T4 + kill-switch default) don't need the reset.
    """
    if sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1":
        yield
        return
    try:
        from scout.main import _clear_calibration_dryrun_date_for_tests
        _clear_calibration_dryrun_date_for_tests()
    except ImportError:
        pass  # Module not yet built (TDD red phase)
    yield


# ---------------------------------------------------------------------------
# T1-T4: Settings validators (run unconditionally — pure config)
# ---------------------------------------------------------------------------


def test_calibration_dryrun_weekday_default_monday(settings_factory):
    """T1 — default 0 = Monday (matches FEEDBACK_WEEKLY_DIGEST_WEEKDAY=6
    pattern shape but uses Monday for Tier 1a/1b weekly review cadence)."""
    s = settings_factory()
    assert s.CALIBRATION_DRY_RUN_WEEKDAY == 0


def test_calibration_dryrun_hour_default_2(settings_factory):
    """T2 — default 2 (local hour, low-traffic)."""
    s = settings_factory()
    assert s.CALIBRATION_DRY_RUN_HOUR == 2


def test_calibration_dryrun_enabled_default_true(settings_factory):
    """adv-S1 — kill-switch defaults True so feature ships active."""
    s = settings_factory()
    assert s.CALIBRATION_DRY_RUN_ENABLED is True


def test_calibration_dryrun_weekday_validator_rejects_out_of_range(settings_factory):
    """T3 — validator: -1, 7, 8 rejected (0-6 only, Mon-Sun)."""
    from pydantic import ValidationError
    for bad in (-1, 7, 8):
        with pytest.raises(ValidationError):
            settings_factory(CALIBRATION_DRY_RUN_WEEKDAY=bad)


def test_calibration_dryrun_hour_validator_rejects_out_of_range(settings_factory):
    """T4 — validator: -1, 24, 25 rejected (0-23 only)."""
    from pydantic import ValidationError
    for bad in (-1, 24, 25):
        with pytest.raises(ValidationError):
            settings_factory(CALIBRATION_DRY_RUN_HOUR=bad)


# ---------------------------------------------------------------------------
# T5: format helper (pure unit; no aiohttp dependency)
# ---------------------------------------------------------------------------


@_SKIP_AIOHTTP
def test_format_dryrun_telegram_message_shape():
    """T5 — format helper produces header + body + footer; truncates if too
    long. Uses real SignalDiff dataclass (arch-NIT-7) so type drift fails
    loudly."""
    from scout.trading.calibrate import (
        SignalDiff,
        SignalStats,
        format_dryrun_telegram_message,
    )

    # Real SignalDiff with no changes (skipped path)
    skipped_diff = SignalDiff(
        signal_type="losers_contrarian",
        stats=None,
        changes=[],
        skipped_reason="n_trades 18 < min 50",
        reason=None,
    )
    # Real SignalDiff with no changes (no_change path)
    nochange_diff = SignalDiff(
        signal_type="first_signal",
        stats=SignalStats(
            n_trades=87,
            wins=45,
            losses=42,
            expired=11,
            win_rate_pct=51.7,
            expired_pct=12.6,
            avg_pnl_pct=2.1,
            avg_loss_pct=-15.0,
        ),
        changes=[],
        skipped_reason=None,
        reason=None,
    )
    msg = format_dryrun_telegram_message(
        [skipped_diff, nochange_diff],
        actionable=0,
        window_days=30,
    )
    assert "Weekly calibration dry-run" in msg
    assert "window=30d" in msg
    assert "0 of 2 signal(s) would change" in msg
    assert "first_signal" in msg
    assert "SKIPPED" in msg
    assert "To apply: ssh root" in msg
    assert "uv run python -m scout.trading.calibrate --apply" in msg
    # Telegram cap headroom
    assert len(msg) <= 4090


# ---------------------------------------------------------------------------
# T0: happy path (adv-M2 — without this, T6/T8 are vacuous)
# ---------------------------------------------------------------------------


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_calibration_dryrun_scheduler_happy_path_fires_alert(
    monkeypatch, settings_factory
):
    """T0 — when conditions match (enabled + weekday + hour + sentinel
    blank + real token), hook calls send_telegram_message ONCE +
    `calibration_dryrun_pass` log fires + sentinel advances."""
    from scout import main as scout_main
    from scout.trading.calibrate import SignalDiff, SignalStats

    # Real SignalDiff with one change to make actionable=1
    actionable_diff = SignalDiff(
        signal_type="gainers_early",
        stats=SignalStats(
            n_trades=131, wins=69, losses=62, expired=11,
            win_rate_pct=52.6, expired_pct=8.4,
            avg_pnl_pct=1.2, avg_loss_pct=-15.0,
        ),
        changes=[
            SimpleNamespace(field="trail_pct", old=20.0, new=18.0),
        ],
        skipped_reason=None,
        reason="expired%",
    )

    async def _fake_build_diffs(*a, **kw):
        return [actionable_diff]

    sent_messages = []

    async def _fake_send(msg, session, settings):
        sent_messages.append(msg)

    monkeypatch.setattr(
        "scout.trading.calibrate.build_diffs", _fake_build_diffs
    )
    monkeypatch.setattr(
        "scout.alerter.send_telegram_message", _fake_send
    )
    # Force token-looks-real → True (no placeholder gate)
    monkeypatch.setattr(
        "scout.trading.calibrate.telegram_token_looks_real",
        lambda s: True,
    )

    settings = settings_factory(
        CALIBRATION_DRY_RUN_ENABLED=True,
        CALIBRATION_DRY_RUN_WEEKDAY=2,
        CALIBRATION_DRY_RUN_HOUR=14,
    )
    # now_local matches WEEKDAY=2 (Wed) + HOUR=14
    now_local = datetime(2026, 5, 6, 14, 30)  # Wed 14:30
    assert now_local.weekday() == 2

    with capture_logs() as logs:
        await scout_main._run_feedback_schedulers(
            db=None, settings=settings,
            last_refresh_date="", last_digest_date="",
            now_local=now_local,
        )
    assert len(sent_messages) == 1, (
        f"expected 1 send_telegram_message call; got {len(sent_messages)}"
    )
    assert "Weekly calibration dry-run" in sent_messages[0]
    events = [e.get("event") for e in logs]
    assert "calibration_dryrun_pass" in events
    # Sentinel advanced
    assert scout_main._last_calibration_dryrun_date == "2026-05-06"


# ---------------------------------------------------------------------------
# T6: idempotency (sentinel prevents re-fire same day)
# ---------------------------------------------------------------------------


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_calibration_dryrun_scheduler_idempotency(
    monkeypatch, settings_factory
):
    """T6 — second call same day → no second alert (sentinel)."""
    from scout import main as scout_main
    from scout.trading.calibrate import SignalDiff

    sent_messages = []
    async def _fake_send(msg, session, settings):
        sent_messages.append(msg)

    async def _fake_build_diffs(*a, **kw):
        return []  # empty is fine for sentinel test

    monkeypatch.setattr(
        "scout.trading.calibrate.build_diffs", _fake_build_diffs
    )
    monkeypatch.setattr(
        "scout.alerter.send_telegram_message", _fake_send
    )
    monkeypatch.setattr(
        "scout.trading.calibrate.telegram_token_looks_real",
        lambda s: True,
    )

    settings = settings_factory(
        CALIBRATION_DRY_RUN_ENABLED=True,
        CALIBRATION_DRY_RUN_WEEKDAY=2,
        CALIBRATION_DRY_RUN_HOUR=14,
    )
    now_local = datetime(2026, 5, 6, 14, 30)

    # First call — fires
    await scout_main._run_feedback_schedulers(
        db=None, settings=settings,
        last_refresh_date="", last_digest_date="",
        now_local=now_local,
    )
    assert len(sent_messages) == 1
    # Second call same day — sentinel blocks
    await scout_main._run_feedback_schedulers(
        db=None, settings=settings,
        last_refresh_date="", last_digest_date="",
        now_local=now_local,
    )
    assert len(sent_messages) == 1, "duplicate alert fired same day"


# ---------------------------------------------------------------------------
# T7: placeholder-token Telegram-skip
# ---------------------------------------------------------------------------


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_calibration_dryrun_scheduler_skips_telegram_on_placeholder_token(
    monkeypatch, settings_factory
):
    """T7 (adv-M2 / arch-Issue2) — when token looks fake,
    `calibration_dryrun_telegram_skipped` log fires + send_telegram NOT
    called + sentinel still advances (so we don't re-attempt every minute
    for the rest of the hour)."""
    from scout import main as scout_main
    from scout.trading.calibrate import SignalDiff

    sent_messages = []
    async def _fake_send(msg, session, settings):
        sent_messages.append(msg)

    async def _fake_build_diffs(*a, **kw):
        return []

    monkeypatch.setattr(
        "scout.trading.calibrate.build_diffs", _fake_build_diffs
    )
    monkeypatch.setattr(
        "scout.alerter.send_telegram_message", _fake_send
    )
    monkeypatch.setattr(
        "scout.trading.calibrate.telegram_token_looks_real",
        lambda s: False,  # placeholder
    )

    settings = settings_factory(
        CALIBRATION_DRY_RUN_ENABLED=True,
        CALIBRATION_DRY_RUN_WEEKDAY=2,
        CALIBRATION_DRY_RUN_HOUR=14,
    )
    now_local = datetime(2026, 5, 6, 14, 30)

    with capture_logs() as logs:
        await scout_main._run_feedback_schedulers(
            db=None, settings=settings,
            last_refresh_date="", last_digest_date="",
            now_local=now_local,
        )
    assert sent_messages == [], "placeholder-token: send must NOT fire"
    events = [e.get("event") for e in logs]
    assert "calibration_dryrun_telegram_skipped" in events
    # Sentinel still advances (so we don't try every minute)
    assert scout_main._last_calibration_dryrun_date == "2026-05-06"


# ---------------------------------------------------------------------------
# T8: build_diffs error → caught + logged + loop continues
# ---------------------------------------------------------------------------


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_calibration_dryrun_scheduler_catches_build_diffs_error(
    monkeypatch, settings_factory
):
    """T8 — build_diffs raises → calibration_dryrun_loop_error log
    + main loop continues (no propagation). Sentinel does NOT advance
    (so we retry next minute within the hour window)."""
    from scout import main as scout_main

    async def _broken_build_diffs(*a, **kw):
        raise RuntimeError("simulated DB lost")

    monkeypatch.setattr(
        "scout.trading.calibrate.build_diffs", _broken_build_diffs
    )

    settings = settings_factory(
        CALIBRATION_DRY_RUN_ENABLED=True,
        CALIBRATION_DRY_RUN_WEEKDAY=2,
        CALIBRATION_DRY_RUN_HOUR=14,
    )
    now_local = datetime(2026, 5, 6, 14, 30)

    with capture_logs() as logs:
        # MUST NOT raise
        await scout_main._run_feedback_schedulers(
            db=None, settings=settings,
            last_refresh_date="", last_digest_date="",
            now_local=now_local,
        )
    events = [e.get("event") for e in logs]
    assert "calibration_dryrun_loop_error" in events
    # Sentinel NOT advanced — operator gets retry next minute
    assert scout_main._last_calibration_dryrun_date == ""


# ---------------------------------------------------------------------------
# T9: kill-switch disabled (adv-S1)
# ---------------------------------------------------------------------------


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_calibration_dryrun_scheduler_disabled_when_killswitch_off(
    monkeypatch, settings_factory
):
    """T9 (adv-S1) — kill-switch False → hook is no-op even when
    weekday + hour match. Operator escape hatch."""
    from scout import main as scout_main

    sent_messages = []
    async def _fake_send(msg, session, settings):
        sent_messages.append(msg)

    async def _fake_build_diffs(*a, **kw):
        # If this fires, test fails — proves the kill-switch short-circuited
        raise AssertionError(
            "build_diffs called despite CALIBRATION_DRY_RUN_ENABLED=False"
        )

    monkeypatch.setattr(
        "scout.trading.calibrate.build_diffs", _fake_build_diffs
    )
    monkeypatch.setattr(
        "scout.alerter.send_telegram_message", _fake_send
    )

    settings = settings_factory(
        CALIBRATION_DRY_RUN_ENABLED=False,  # kill-switch
        CALIBRATION_DRY_RUN_WEEKDAY=2,
        CALIBRATION_DRY_RUN_HOUR=14,
    )
    now_local = datetime(2026, 5, 6, 14, 30)

    with capture_logs() as logs:
        await scout_main._run_feedback_schedulers(
            db=None, settings=settings,
            last_refresh_date="", last_digest_date="",
            now_local=now_local,
        )
    assert sent_messages == []
    # Sentinel does NOT advance when disabled
    assert scout_main._last_calibration_dryrun_date == ""
