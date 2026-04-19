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

    async def _capture_tg(text, session, settings):
        sent.append(text)

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

    assert any(
        "combo_refresh" in t.lower() for t in sent
    ), "expected a Telegram alert after 3 consecutive failures"

    # Dedup guard: 5 consecutive failures should produce exactly one alert,
    # not one per loop iteration after the streak crosses 3.
    assert len(sent) == 1, (
        f"expected exactly 1 alert across 5 failing days, got {len(sent)}: {sent}"
    )
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
