"""Tests for main-loop scheduling of combo refresh + weekly digest.

Approach: factor the schedule check into a pure helper
`_run_feedback_schedulers(db, settings, last_refresh, last_digest, now_local)`
inside main.py so we can drive it with a fake clock. The loop body calls it
once per cycle and updates the last_* state with the return value.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest


async def test_refresh_fires_once_per_day_at_configured_hour(
    tmp_path,
    settings_factory,
    monkeypatch,
):
    from scout.db import Database
    from scout import main as main_mod

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(
        FEEDBACK_COMBO_REFRESH_HOUR=3,
        FEEDBACK_WEEKLY_DIGEST_HOUR=9,
        FEEDBACK_WEEKLY_DIGEST_WEEKDAY=6,
    )

    refresh_mock = AsyncMock(return_value={"refreshed": 0, "failed": 0})
    digest_mock = AsyncMock()
    monkeypatch.setattr(main_mod._combo_refresh, "refresh_all", refresh_mock)
    monkeypatch.setattr(main_mod._weekly_digest, "send_weekly_digest", digest_mock)

    last_refresh = ""
    last_digest = ""

    # 02:59 — neither fires.
    now = datetime(2026, 4, 19, 2, 59, 0)  # Sunday
    last_refresh, last_digest = await main_mod._run_feedback_schedulers(
        db,
        s,
        last_refresh,
        last_digest,
        now,
    )
    assert refresh_mock.call_count == 0

    # 03:00 — refresh fires.
    now = datetime(2026, 4, 19, 3, 0, 0)
    last_refresh, last_digest = await main_mod._run_feedback_schedulers(
        db,
        s,
        last_refresh,
        last_digest,
        now,
    )
    assert refresh_mock.call_count == 1
    assert last_refresh == "2026-04-19"

    # 03:30 same day — must NOT fire again.
    now = datetime(2026, 4, 19, 3, 30, 0)
    last_refresh, last_digest = await main_mod._run_feedback_schedulers(
        db,
        s,
        last_refresh,
        last_digest,
        now,
    )
    assert refresh_mock.call_count == 1

    # 09:00 Sunday — digest fires.
    now = datetime(2026, 4, 19, 9, 0, 0)  # weekday() == 6
    last_refresh, last_digest = await main_mod._run_feedback_schedulers(
        db,
        s,
        last_refresh,
        last_digest,
        now,
    )
    assert digest_mock.call_count == 1
    assert last_digest == "2026-04-19"
    await db.close()


async def test_refresh_failure_streak_alerts_telegram(
    tmp_path,
    settings_factory,
    monkeypatch,
):
    """Three consecutive combo_refresh failures → exactly one Telegram alert.

    A 4th consecutive failure must NOT send a duplicate alert (dedup guard).
    """
    from scout.db import Database
    from scout import main as main_mod

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(FEEDBACK_COMBO_REFRESH_HOUR=3)

    async def _boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(main_mod._combo_refresh, "refresh_all", _boom)

    sent: list = []
    sent_kwargs: list[dict] = []

    async def _capture_tg(text, session, settings, **kwargs):
        sent.append(text)
        sent_kwargs.append(kwargs)

    monkeypatch.setattr(main_mod.alerter, "send_telegram_message", _capture_tg)

    # Reset module-level dedup state so each test run is independent.
    main_mod._combo_refresh_failure_streak = 0
    main_mod._combo_refresh_streak_last_alerted = 0

    last_refresh = ""
    last_digest = ""
    for day in range(1, 6):  # 5 days: failures on days 1-5 → streak hits 3 on day 3
        now = datetime(2026, 4, day, 3, 0, 0)
        last_refresh, last_digest = await main_mod._run_feedback_schedulers(
            db,
            s,
            last_refresh,
            last_digest,
            now,
        )
        # Fix 3: last_refresh_date must NOT advance when refresh_all fails.
        assert (
            last_refresh == ""
        ), f"last_refresh_date advanced to {last_refresh!r} on day {day} despite failure"

    assert any(
        "combo_refresh" in t.lower() for t in sent
    ), "expected a Telegram alert after 3 consecutive failures"
    assert sent_kwargs[0].get("parse_mode") is None

    # Dedup guard: 5 consecutive failures should produce exactly one alert,
    # not one per loop iteration after the streak crosses 3.
    assert (
        len(sent) == 1
    ), f"expected exactly 1 alert across 5 failing days, got {len(sent)}: {sent}"
    await db.close()


async def test_transient_failure_allows_same_hour_retry(
    tmp_path,
    settings_factory,
    monkeypatch,
):
    """Day 1 03:00 — refresh fails → sentinel stays '' → same-hour retry at 03:01 succeeds."""
    from scout.db import Database
    from scout import main as main_mod

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(FEEDBACK_COMBO_REFRESH_HOUR=3)

    call_results: list[bool] = []

    async def _first_fails_second_succeeds(*a, **k):
        if len(call_results) == 0:
            call_results.append(False)
            raise RuntimeError("transient failure")
        call_results.append(True)
        return {"refreshed": 1, "failed": 0}

    monkeypatch.setattr(
        main_mod._combo_refresh, "refresh_all", _first_fails_second_succeeds
    )
    monkeypatch.setattr(main_mod.alerter, "send_telegram_message", lambda *a, **k: None)

    main_mod._combo_refresh_failure_streak = 0
    main_mod._combo_refresh_streak_last_alerted = 0

    last_refresh = ""
    last_digest = ""

    # 03:00 — fails → sentinel stays ''
    now = datetime(2026, 4, 19, 3, 0, 0)
    last_refresh, last_digest = await main_mod._run_feedback_schedulers(
        db, s, last_refresh, last_digest, now
    )
    assert (
        last_refresh == ""
    ), f"Sentinel must not advance on failure, got {last_refresh!r}"
    assert len(call_results) == 1

    # 03:01 — same hour, sentinel still '' → scheduler fires again → succeeds
    now = datetime(2026, 4, 19, 3, 1, 0)
    last_refresh, last_digest = await main_mod._run_feedback_schedulers(
        db, s, last_refresh, last_digest, now
    )
    assert (
        last_refresh == "2026-04-19"
    ), f"Sentinel must advance on success, got {last_refresh!r}"
    assert len(call_results) == 2, "refresh_all must have been called twice"
    await db.close()


async def test_streak_alert_counter_resets_after_success(
    tmp_path,
    settings_factory,
    monkeypatch,
):
    """After a streak alert fires, one success must clear the counter so a
    subsequent 3-failure streak can alert again.

    Covers the dedup-state reset path (main.py:101-102). Without resetting
    _combo_refresh_streak_last_alerted on success, the SECOND alert would be
    suppressed forever, leaving operators blind to a repeat outage."""
    from scout.db import Database
    from scout import main as main_mod

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(FEEDBACK_COMBO_REFRESH_HOUR=3)

    # Alternate: fails first 3 days, succeeds day 4, fails days 5-7.
    results = iter([False, False, False, True, False, False, False])

    async def _seq(*a, **k):
        if next(results):
            return {"refreshed": 1, "failed": 0}
        raise RuntimeError("transient")

    monkeypatch.setattr(main_mod._combo_refresh, "refresh_all", _seq)

    sent: list = []
    sent_kwargs: list[dict] = []

    async def _capture_tg(text, session, settings, **kwargs):
        sent.append(text)
        sent_kwargs.append(kwargs)

    monkeypatch.setattr(main_mod.alerter, "send_telegram_message", _capture_tg)

    main_mod._combo_refresh_failure_streak = 0
    main_mod._combo_refresh_streak_last_alerted = 0

    last_refresh = ""
    last_digest = ""
    for day in range(1, 8):
        now = datetime(2026, 4, day, 3, 0, 0)
        last_refresh, last_digest = await main_mod._run_feedback_schedulers(
            db,
            s,
            last_refresh,
            last_digest,
            now,
        )
        # After the day-4 success, both counters must be zero so that the
        # next 3-failure streak can re-alert. Checking streak alone isn't
        # enough — a regression that zeros streak but leaves last_alerted
        # non-zero would suppress all future streak alerts forever.
        if day == 4:
            assert main_mod._combo_refresh_failure_streak == 0
            assert main_mod._combo_refresh_streak_last_alerted == 0

    # Exactly two alerts: one at end of day 3, one at end of day 7.
    assert len(sent) == 2, (
        f"expected 2 alerts (one per 3-failure streak with a success between), "
        f"got {len(sent)}: {sent}"
    )
    assert all(kwargs.get("parse_mode") is None for kwargs in sent_kwargs)
    # Counter was cleared by the day-4 success.
    assert main_mod._combo_refresh_failure_streak == 3
    await db.close()


async def test_dst_fall_back_does_not_double_fire(
    tmp_path,
    settings_factory,
    monkeypatch,
):
    """Fall-back DST: local hour 1 repeats on the transition date. The
    last_refresh_date guard must prevent the refresh from firing twice.

    Real-world example: US "fall back" 2026-11-01 — 02:00 → 01:00. An hourly
    cron seeing local time would observe distinct wall-clock moments inside
    hour 1 twice (once pre-shift, once post-shift). We rely on the date
    sentinel, not on tz arithmetic, to dedupe. Use distinct-but-same-hour
    timestamps so the test proves dedup works against genuinely different
    clock reads, not an accidental identity match."""
    from scout.db import Database
    from scout import main as main_mod

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(FEEDBACK_COMBO_REFRESH_HOUR=1)

    refresh_mock = AsyncMock(return_value={"refreshed": 0, "failed": 0})
    monkeypatch.setattr(main_mod._combo_refresh, "refresh_all", refresh_mock)

    last_refresh = ""
    last_digest = ""

    # First 01:30:00 (pre-fall-back instant — EDT on the wall clock).
    now = datetime(2026, 11, 1, 1, 30, 0)
    last_refresh, last_digest = await main_mod._run_feedback_schedulers(
        db,
        s,
        last_refresh,
        last_digest,
        now,
    )
    assert refresh_mock.call_count == 1
    assert last_refresh == "2026-11-01"

    # Second read inside the repeated hour-1 wall-clock window — distinct
    # instant (01:45:15, post-fall-back EST), same local date and same hour.
    # Must be deduped despite being a genuinely later clock observation.
    now = datetime(2026, 11, 1, 1, 45, 15)
    last_refresh, last_digest = await main_mod._run_feedback_schedulers(
        db,
        s,
        last_refresh,
        last_digest,
        now,
    )
    assert (
        refresh_mock.call_count == 1
    ), "refresh_all must not fire a second time during the repeated DST hour"
    await db.close()


async def test_dst_spring_forward_gapped_hour_skips_silently(
    tmp_path,
    settings_factory,
    monkeypatch,
):
    """Spring-forward DST: if the configured hour falls inside the gap (e.g.
    02:xx doesn't exist on spring-forward dates), the job is silently skipped
    for that day. Documented accepted constraint (main.py:88).

    The scheduler is not timezone-aware, so we verify the observable outcome:
    a clock that skips from 01:59 → 03:00 never sees hour == 2, so no fire."""
    from scout.db import Database
    from scout import main as main_mod

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(FEEDBACK_COMBO_REFRESH_HOUR=2)

    refresh_mock = AsyncMock(return_value={"refreshed": 0, "failed": 0})
    monkeypatch.setattr(main_mod._combo_refresh, "refresh_all", refresh_mock)

    last_refresh = ""
    last_digest = ""

    # 01:59 — below target hour.
    now = datetime(2026, 3, 8, 1, 59, 0)  # US spring-forward
    last_refresh, last_digest = await main_mod._run_feedback_schedulers(
        db,
        s,
        last_refresh,
        last_digest,
        now,
    )
    # 03:00 — clock jumped past the target hour.
    now = datetime(2026, 3, 8, 3, 0, 0)
    last_refresh, last_digest = await main_mod._run_feedback_schedulers(
        db,
        s,
        last_refresh,
        last_digest,
        now,
    )
    assert refresh_mock.call_count == 0, (
        "spring-forward gapped hour: refresh must not fire when the loop "
        "never observes hour == target"
    )
    # Sentinel must remain unstamped for the skipped day — a future bug that
    # stamps last_refresh_date on gap-skip would also break next-day behavior.
    assert (
        last_refresh == ""
    ), f"sentinel must not advance on gap-skip, got {last_refresh!r}"
    # Next day at the configured hour — fires normally.
    now = datetime(2026, 3, 9, 2, 0, 0)
    last_refresh, last_digest = await main_mod._run_feedback_schedulers(
        db,
        s,
        last_refresh,
        last_digest,
        now,
    )
    assert refresh_mock.call_count == 1
    assert last_refresh == "2026-03-09"
    await db.close()


def test_schedule_keys_exist_in_main_source():
    """Belt-and-braces: also confirm the schedule constants are referenced."""
    from pathlib import Path

    src = Path(__file__).parent.parent / "scout" / "main.py"
    text = src.read_text(encoding="utf-8")
    assert "last_combo_refresh_date" in text
    assert "last_weekly_digest_date" in text
    assert "FEEDBACK_COMBO_REFRESH_HOUR" in text
    assert "FEEDBACK_WEEKLY_DIGEST_WEEKDAY" in text
    assert "FEEDBACK_WEEKLY_DIGEST_HOUR" in text
