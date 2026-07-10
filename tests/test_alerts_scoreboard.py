"""Tests for the weekly alerts scoreboard (ALR-04).

Golden-file coverage for three shapes (populated / sparse / unlinked-heavy)
plus the n-gate, window filter, send hygiene (parse_mode=None + source label),
config default, and the _run_feedback_schedulers wiring.

Regenerate goldens after an intentional format change with:
    SCOREBOARD_GOLDEN_REGEN=1 python -m pytest tests/test_alerts_scoreboard.py -k golden
Then eyeball the diff and commit the updated tests/golden/alerts_scoreboard/*.txt.
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scout.config import Settings
from scout.db import Database
from scout.main import _run_feedback_schedulers
from scout.trading import alerts_scoreboard

GOLDEN_DIR = Path(__file__).parent / "golden" / "alerts_scoreboard"
NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


async def _seed_trade(
    db,
    *,
    symbol,
    signal_type,
    status,
    pnl_usd,
    pnl_pct,
    peak_pct,
) -> int:
    cur = await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_price, sl_price, status, "
        " pnl_usd, pnl_pct, peak_pct, opened_at) "
        "VALUES (?, ?, ?, 'coingecko', ?, '{}', 1.0, 300.0, 300.0, 1.2, 0.9, "
        " ?, ?, ?, ?, ?)",
        (
            f"tok-{symbol}",
            symbol,
            symbol,
            signal_type,
            status,
            pnl_usd,
            pnl_pct,
            peak_pct,
            (NOW - timedelta(days=2)).isoformat(),
        ),
    )
    return cur.lastrowid


async def _seed_alert(
    db,
    *,
    signal_type,
    paper_trade_id,
    token_id,
    outcome="sent",
    alerted_days_ago=1,
) -> None:
    await db._conn.execute(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            paper_trade_id,
            signal_type,
            token_id,
            (NOW - timedelta(days=alerted_days_ago)).isoformat(),
            outcome,
        ),
    )


async def _populate_populated(db) -> None:
    """6 linked trades (win/loss/open across 3 signals) + 2 unlinked alerts."""
    spec = [
        ("volume_spike", "WIF", "closed_tp", 900.00, 300.0, 342.0),
        ("volume_spike", "BONK", "closed_sl", -30.00, -10.0, 5.0),
        ("volume_spike", "POPCAT", "open", None, None, 15.0),
        ("first_signal", "MOON", "closed_tp", 12.00, 12.0, 180.0),
        ("first_signal", "DEGEN", "closed_sl", -25.00, -8.0, 22.0),
        ("tg_social", "PEPE", "closed_tp", 8.00, 6.0, 20.0),
    ]
    for sig, sym, status, pnl_usd, pnl_pct, peak in spec:
        tid = await _seed_trade(
            db,
            symbol=sym,
            signal_type=sig,
            status=status,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            peak_pct=peak,
        )
        await _seed_alert(
            db, signal_type=sig, paper_trade_id=tid, token_id=f"tok-{sym}"
        )
    # Two unlinked sent alerts (no paper_trade).
    await _seed_alert(
        db, signal_type="volume_spike", paper_trade_id=None, token_id="u1"
    )
    await _seed_alert(
        db, signal_type="narrative_prediction", paper_trade_id=None, token_id="u2"
    )
    await db._conn.commit()


async def _populate_sparse(db) -> None:
    """Only 2 linked alerts (< MIN 5) -> INSUFFICIENT_DATA, plus 1 unlinked."""
    for sym, pnl in (("AAA", 10.0), ("BBB", -5.0)):
        tid = await _seed_trade(
            db,
            symbol=sym,
            signal_type="volume_spike",
            status="closed_tp" if pnl > 0 else "closed_sl",
            pnl_usd=pnl,
            pnl_pct=pnl,
            peak_pct=abs(pnl) + 5,
        )
        await _seed_alert(
            db, signal_type="volume_spike", paper_trade_id=tid, token_id=f"tok-{sym}"
        )
    await _seed_alert(db, signal_type="first_signal", paper_trade_id=None, token_id="u")
    await db._conn.commit()


async def _populate_unlinked_heavy(db) -> None:
    """6 linked (passes gate) but 15 unlinked sent alerts dominate the counts."""
    spec = [
        ("volume_spike", "SYMA", "closed_tp", 50.00, 10.0, 25.0),
        ("volume_spike", "SYMB", "closed_sl", -20.00, -8.0, 12.0),
        ("volume_spike", "SYMC", "closed_tp", 15.00, 5.0, 30.0),
        ("volume_spike", "SYMD", "open", None, None, 8.0),
        ("first_signal", "SYME", "closed_sl", -40.00, -12.0, 60.0),
        ("first_signal", "SYMF", "closed_tp", 5.00, 4.0, 90.0),
    ]
    for sig, sym, status, pnl_usd, pnl_pct, peak in spec:
        tid = await _seed_trade(
            db,
            symbol=sym,
            signal_type=sig,
            status=status,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            peak_pct=peak,
        )
        await _seed_alert(
            db, signal_type=sig, paper_trade_id=tid, token_id=f"tok-{sym}"
        )
    for i in range(10):
        await _seed_alert(
            db, signal_type="volume_spike", paper_trade_id=None, token_id=f"uv{i}"
        )
    for i in range(5):
        await _seed_alert(
            db,
            signal_type="narrative_prediction",
            paper_trade_id=None,
            token_id=f"un{i}",
        )
    await db._conn.commit()


_SCENARIOS = {
    "populated": _populate_populated,
    "sparse": _populate_sparse,
    "unlinked_heavy": _populate_unlinked_heavy,
}


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
async def test_scoreboard_golden(tmp_path, name):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        await _SCENARIOS[name](db)
        got = await alerts_scoreboard.build_alerts_scoreboard(db, now=NOW)
    finally:
        await db.close()

    golden = GOLDEN_DIR / f"{name}.txt"
    if os.environ.get("SCOREBOARD_GOLDEN_REGEN"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(got + "\n", encoding="utf-8")
        pytest.skip(f"regenerated golden {golden.name}")
    expected = golden.read_text(encoding="utf-8").rstrip("\n")
    assert got == expected


async def test_empty_returns_insufficient_data_not_none(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        got = await alerts_scoreboard.build_alerts_scoreboard(db, now=NOW)
    finally:
        await db.close()
    assert got is not None
    assert "INSUFFICIENT_DATA" in got
    assert "Sent 0" in got


async def test_window_excludes_old_alerts(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        tid = await _seed_trade(
            db,
            symbol="OLD",
            signal_type="volume_spike",
            status="closed_tp",
            pnl_usd=10.0,
            pnl_pct=10.0,
            peak_pct=20.0,
        )
        # 30 days ago -> outside the default 7d window.
        await _seed_alert(
            db,
            signal_type="volume_spike",
            paper_trade_id=tid,
            token_id="tok-OLD",
            alerted_days_ago=30,
        )
        await db._conn.commit()
        got = await alerts_scoreboard.build_alerts_scoreboard(db, now=NOW)
    finally:
        await db.close()
    assert "Sent 0" in got


async def test_send_uses_plain_text_and_source_label(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(WEEKLY_ALERTS_SCOREBOARD_ENABLED=True)
    sent = AsyncMock()
    try:
        with patch.object(alerts_scoreboard.alerter, "send_telegram_message", sent):
            await alerts_scoreboard.send_alerts_scoreboard(db, settings)
    finally:
        await db.close()
    assert sent.await_count >= 1
    for call in sent.await_args_list:
        assert call.kwargs["parse_mode"] is None
        assert call.kwargs["source"] == "weekly_alerts_scoreboard"


def test_config_default_off():
    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    assert s.WEEKLY_ALERTS_SCOREBOARD_ENABLED is False
    assert s.WEEKLY_ALERTS_SCOREBOARD_MIN_LINKED == 5
    assert s.WEEKLY_ALERTS_SCOREBOARD_WINDOW_DAYS == 7


# --- _run_feedback_schedulers wiring (mirrors test_cohort_digest_main_hook) ---


def _make_settings(tmp_path, **overrides):
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        DB_PATH=tmp_path / "scout.db",
        **overrides,
    )


@contextlib.contextmanager
def _patched_schedulers(scoreboard_mock):
    with (
        patch("scout.main._combo_refresh.refresh_all", new=AsyncMock(return_value={})),
        patch("scout.main._weekly_digest.send_weekly_digest", new=AsyncMock()),
        patch("scout.trading.cohort_digest.send_cohort_digest", new=AsyncMock()),
        patch(
            "scout.main._alerts_scoreboard.send_alerts_scoreboard", new=scoreboard_mock
        ),
    ):
        yield


async def test_scoreboard_fires_on_digest_tick_when_enabled(tmp_path):
    settings = _make_settings(tmp_path, WEEKLY_ALERTS_SCOREBOARD_ENABLED=True)
    db = MagicMock()
    now_local = datetime(2026, 5, 17, 9, 0, 0)  # Sunday 09:00 = digest tick
    mock_sb = AsyncMock()
    with _patched_schedulers(mock_sb):
        await _run_feedback_schedulers(db, settings, "", "", "", now_local)
    mock_sb.assert_awaited_once_with(db, settings)


async def test_scoreboard_skipped_when_disabled_by_default(tmp_path):
    settings = _make_settings(tmp_path)  # flag default False
    db = MagicMock()
    now_local = datetime(2026, 5, 17, 9, 0, 0)  # Sunday 09:00
    mock_sb = AsyncMock()
    with _patched_schedulers(mock_sb):
        await _run_feedback_schedulers(db, settings, "", "", "", now_local)
    mock_sb.assert_not_awaited()


async def test_scoreboard_not_fired_off_day(tmp_path):
    settings = _make_settings(tmp_path, WEEKLY_ALERTS_SCOREBOARD_ENABLED=True)
    db = MagicMock()
    now_local = datetime(2026, 5, 18, 9, 0, 0)  # Monday 09:00 (not digest weekday 6)
    mock_sb = AsyncMock()
    with _patched_schedulers(mock_sb):
        await _run_feedback_schedulers(db, settings, "", "", "", now_local)
    mock_sb.assert_not_awaited()


async def test_scoreboard_not_double_fired_same_day(tmp_path):
    settings = _make_settings(tmp_path, WEEKLY_ALERTS_SCOREBOARD_ENABLED=True)
    db = MagicMock()
    now_local = datetime(2026, 5, 17, 9, 0, 0)  # Sunday 09:00
    mock_sb = AsyncMock()
    with _patched_schedulers(mock_sb):
        # digest sentinel already == today -> whole block skipped
        await _run_feedback_schedulers(db, settings, "", "2026-05-17", "", now_local)
    mock_sb.assert_not_awaited()
