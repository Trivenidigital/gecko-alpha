"""Integration tests for cohort-digest weekly-loop hook (cycle 5 commit 5/5).

Verifies _run_feedback_schedulers wiring without booting the full pipeline.
Imports the helper directly to dodge OPENSSL_Uplink on Windows
(memory reference_windows_openssl_workaround.md).
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scout.config import Settings
from scout.main import _run_feedback_schedulers


def _make_settings(tmp_path, **overrides):
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        DB_PATH=tmp_path / "scout.db",
        **overrides,
    )


async def test_run_feedback_schedulers_returns_three_tuple(tmp_path):
    """V29 MUST-FIX: signature is tuple[str, str, str]."""
    settings = _make_settings(tmp_path)
    db = MagicMock()
    db.cohort_digest_read_state = AsyncMock(return_value={"last_digest_date": None, "last_final_block_fired_at": None})
    # now_local on a Friday (NOT digest day, NOT cohort day) at non-trigger hour.
    now_local = datetime(2026, 5, 15, 14, 0, 0)  # Friday 14:00
    with patch("scout.main._combo_refresh.refresh_all", new=AsyncMock(return_value={})):
        with patch("scout.main._weekly_digest.send_weekly_digest", new=AsyncMock()):
            with patch(
                "scout.trading.cohort_digest.send_cohort_digest", new=AsyncMock()
            ):
                result = await _run_feedback_schedulers(
                    db, settings, "", "", "", now_local
                )
    assert isinstance(result, tuple)
    assert len(result) == 3


async def test_main_weekly_loop_fires_send_cohort_digest_on_configured_day_and_hour(tmp_path):
    """Monday at COHORT_DIGEST_HOUR (default 9) with empty sentinel → fires."""
    settings = _make_settings(tmp_path)
    db = MagicMock()
    now_local = datetime(2026, 5, 18, 9, 0, 0)  # Monday 09:00
    mock_send = AsyncMock()
    with patch("scout.main._combo_refresh.refresh_all", new=AsyncMock()), \
         patch("scout.main._weekly_digest.send_weekly_digest", new=AsyncMock()), \
         patch("scout.trading.cohort_digest.send_cohort_digest", new=mock_send):
        last_refresh, last_digest, last_cohort = await _run_feedback_schedulers(
            db, settings, "", "", "", now_local
        )
    mock_send.assert_awaited_once_with(db, settings)
    assert last_cohort == "2026-05-18"


async def test_main_weekly_loop_skips_send_cohort_digest_when_disabled(tmp_path):
    """COHORT_DIGEST_ENABLED=False → no call."""
    settings = _make_settings(tmp_path, COHORT_DIGEST_ENABLED=False)
    db = MagicMock()
    now_local = datetime(2026, 5, 18, 9, 0, 0)  # Monday 09:00
    mock_send = AsyncMock()
    with patch("scout.main._combo_refresh.refresh_all", new=AsyncMock()), \
         patch("scout.main._weekly_digest.send_weekly_digest", new=AsyncMock()), \
         patch("scout.trading.cohort_digest.send_cohort_digest", new=mock_send):
        await _run_feedback_schedulers(db, settings, "", "", "", now_local)
    mock_send.assert_not_awaited()


async def test_main_weekly_loop_doesnt_re_fire_same_day(tmp_path):
    """Same-day sentinel set → cohort digest NOT called again."""
    settings = _make_settings(tmp_path)
    db = MagicMock()
    now_local = datetime(2026, 5, 18, 9, 0, 0)  # Monday 09:00
    mock_send = AsyncMock()
    with patch("scout.main._combo_refresh.refresh_all", new=AsyncMock()), \
         patch("scout.main._weekly_digest.send_weekly_digest", new=AsyncMock()), \
         patch("scout.trading.cohort_digest.send_cohort_digest", new=mock_send):
        await _run_feedback_schedulers(
            db, settings, "", "", "2026-05-18", now_local
        )
    mock_send.assert_not_awaited()


async def test_main_weekly_loop_doesnt_fire_off_day(tmp_path):
    """Sunday (weekday=6, day-of-week mismatch) → no fire."""
    settings = _make_settings(tmp_path)
    db = MagicMock()
    now_local = datetime(2026, 5, 17, 9, 0, 0)  # Sunday 09:00
    mock_send = AsyncMock()
    with patch("scout.main._combo_refresh.refresh_all", new=AsyncMock()), \
         patch("scout.main._weekly_digest.send_weekly_digest", new=AsyncMock()), \
         patch("scout.trading.cohort_digest.send_cohort_digest", new=mock_send):
        await _run_feedback_schedulers(db, settings, "", "", "", now_local)
    mock_send.assert_not_awaited()


async def test_main_weekly_loop_doesnt_fire_off_hour(tmp_path):
    """Monday at 14:00 (not COHORT_DIGEST_HOUR=9) → no fire."""
    settings = _make_settings(tmp_path)
    db = MagicMock()
    now_local = datetime(2026, 5, 18, 14, 0, 0)  # Monday 14:00
    mock_send = AsyncMock()
    with patch("scout.main._combo_refresh.refresh_all", new=AsyncMock()), \
         patch("scout.main._weekly_digest.send_weekly_digest", new=AsyncMock()), \
         patch("scout.trading.cohort_digest.send_cohort_digest", new=mock_send):
        await _run_feedback_schedulers(db, settings, "", "", "", now_local)
    mock_send.assert_not_awaited()


async def test_main_weekly_loop_exception_doesnt_abort_helper(tmp_path):
    """send_cohort_digest raising → helper logs and returns (does NOT propagate)."""
    settings = _make_settings(tmp_path)
    db = MagicMock()
    now_local = datetime(2026, 5, 18, 9, 0, 0)  # Monday 09:00
    mock_send = AsyncMock(side_effect=RuntimeError("simulated"))
    with patch("scout.main._combo_refresh.refresh_all", new=AsyncMock()), \
         patch("scout.main._weekly_digest.send_weekly_digest", new=AsyncMock()), \
         patch("scout.trading.cohort_digest.send_cohort_digest", new=mock_send):
        # Must not raise
        last_refresh, last_digest, last_cohort = await _run_feedback_schedulers(
            db, settings, "", "", "", now_local
        )
    mock_send.assert_awaited_once()
    # Sentinel NOT advanced on exception (so next eligible hour retries)
    assert last_cohort == ""
