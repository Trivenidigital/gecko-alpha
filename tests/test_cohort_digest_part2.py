"""Part 2: build_cohort_digest + send_cohort_digest + _detect_verdict_flip.

Split from test_cohort_digest.py to keep the file size manageable and
preserve module-level fixture isolation. BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST
cycle 5 commit 4/5.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.cohort_digest import (
    _detect_verdict_flip,
    build_cohort_digest,
    send_cohort_digest,
)


@pytest.fixture
async def db_with_paper_trades(tmp_path):
    db = Database(str(tmp_path / "cohort2.db"))
    await db.initialize()
    yield db
    await db.close()


def _make_settings(tmp_path):
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        DB_PATH=tmp_path / "scout.db",
    )


async def _insert_paper_trade(
    db, *, token_id, signal_type, status, pnl_usd, would_be_live,
    closed_at, opened_at=None,
):
    opened_at = opened_at or "2026-05-10T00:00:00+00:00"
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, pnl_usd, would_be_live, opened_at, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (token_id, token_id.upper(), token_id, "eth", signal_type, "{}",
         1.0, 100.0, 100.0, 1.2, 0.9,
         status, pnl_usd, would_be_live, opened_at, closed_at),
    )
    await db._conn.commit()


# ---------------------------------------------------------------------------
# _detect_verdict_flip
# ---------------------------------------------------------------------------


def test_detect_verdict_flip_emits_when_both_n_above_gate():
    flips = _detect_verdict_flip(
        current={"gainers_early": {"verdict": "strong-pattern (exploratory)", "eN": 14}},
        previous={"gainers_early": {"verdict": "moderate", "eN": 12}},
        n_gate=10,
    )
    assert flips == [("gainers_early", "moderate", "strong-pattern (exploratory)")]


def test_detect_verdict_flip_ignores_when_previous_n_below_gate():
    """V27 SHOULD-FIX: prev eN<n_gate means prev was INSUFFICIENT — not flip."""
    flips = _detect_verdict_flip(
        current={"gainers_early": {"verdict": "moderate", "eN": 12}},
        previous={"gainers_early": {"verdict": "tracking", "eN": 5}},
        n_gate=10,
    )
    assert flips == []


def test_detect_verdict_flip_ignores_when_current_n_below_gate():
    flips = _detect_verdict_flip(
        current={"gainers_early": {"verdict": "tracking", "eN": 5}},
        previous={"gainers_early": {"verdict": "moderate", "eN": 12}},
        n_gate=10,
    )
    assert flips == []


def test_detect_verdict_flip_ignores_insufficient_data_transitions():
    flips = _detect_verdict_flip(
        current={"slow_burn": {"verdict": "moderate", "eN": 12}},
        previous={"slow_burn": {"verdict": "INSUFFICIENT_DATA (n=5, need >=10)", "eN": 5}},
        n_gate=10,
    )
    assert flips == []


def test_detect_verdict_flip_ignores_near_identical():
    flips = _detect_verdict_flip(
        current={"chain_completed": {"verdict": "near-identical", "eN": 50}},
        previous={"chain_completed": {"verdict": "moderate", "eN": 50}},
        n_gate=10,
    )
    assert flips == []


def test_detect_verdict_flip_same_label_no_flip():
    flips = _detect_verdict_flip(
        current={"gainers_early": {"verdict": "moderate", "eN": 14}},
        previous={"gainers_early": {"verdict": "moderate", "eN": 12}},
        n_gate=10,
    )
    assert flips == []


# ---------------------------------------------------------------------------
# build_cohort_digest
# ---------------------------------------------------------------------------


async def test_build_cohort_digest_returns_none_when_no_activity(
    db_with_paper_trades, tmp_path,
):
    settings = _make_settings(tmp_path)
    text = await build_cohort_digest(
        db_with_paper_trades, date(2026, 5, 17), settings,
    )
    assert text is None


async def test_build_cohort_digest_renders_signal_blocks(
    db_with_paper_trades, tmp_path,
):
    settings = _make_settings(tmp_path)
    db = db_with_paper_trades
    for i in range(12):
        await _insert_paper_trade(
            db, token_id=f"t{i}", signal_type="gainers_early",
            status="closed_tp" if i < 8 else "closed_sl",
            pnl_usd=20 if i < 8 else -10,
            would_be_live=1,
            closed_at=f"2026-05-13T0{i % 9}:00:00+00:00",
        )
    text = await build_cohort_digest(db, date(2026, 5, 17), settings)
    assert text is not None
    assert "gainers_early" in text
    assert "Cohort Digest" in text
    assert "n-gate" in text


async def test_build_cohort_digest_emits_single_flips_this_week_line(
    db_with_paper_trades, tmp_path,
):
    """V28 SHOULD-FIX: max one 'FLIPS THIS WEEK' line per digest."""
    settings = _make_settings(tmp_path)
    db = db_with_paper_trades
    for i in range(12):
        await _insert_paper_trade(
            db, token_id=f"prev_{i}", signal_type="gainers_early",
            status="closed_tp", pnl_usd=10, would_be_live=1,
            closed_at=f"2026-05-{4 + i % 3}T0{i % 9}:00:00+00:00",
        )
    for i in range(12):
        await _insert_paper_trade(
            db, token_id=f"curr_{i}", signal_type="gainers_early",
            status="closed_tp" if i % 2 == 0 else "closed_sl",
            pnl_usd=20 if i % 2 == 0 else -15,
            would_be_live=1,
            closed_at=f"2026-05-1{i % 4}T0{i % 9}:00:00+00:00",
        )
    text = await build_cohort_digest(db, date(2026, 5, 17), settings)
    assert text is not None
    assert text.count("FLIPS THIS WEEK") <= 1


async def test_build_cohort_digest_appends_final_block_at_lock_date(
    db_with_paper_trades, tmp_path,
):
    """V28 SHOULD-FIX: end_date >= COHORT_DIGEST_FINAL_DATE AND not fired → block."""
    settings = _make_settings(tmp_path)
    db = db_with_paper_trades
    for i in range(11):
        await _insert_paper_trade(
            db, token_id=f"t{i}", signal_type="gainers_early",
            status="closed_tp", pnl_usd=10, would_be_live=1,
            closed_at=f"2026-06-0{1 + i % 7}T0{i % 9}:00:00+00:00",
        )
    text = await build_cohort_digest(db, date(2026, 6, 8), settings)
    assert text is not None
    assert "4-week decision point" in text
    assert "__FINAL_BLOCK_INCLUDED__" in text


async def test_build_cohort_digest_skips_final_block_when_already_fired(
    db_with_paper_trades, tmp_path,
):
    """Idempotency: once stamped, final block does NOT re-render."""
    settings = _make_settings(tmp_path)
    db = db_with_paper_trades
    await db.cohort_digest_stamp_final_block_fired("2026-06-08T09:00:00+00:00")
    for i in range(11):
        await _insert_paper_trade(
            db, token_id=f"t{i}", signal_type="gainers_early",
            status="closed_tp", pnl_usd=10, would_be_live=1,
            closed_at=f"2026-06-1{i % 4}T0{i % 9}:00:00+00:00",
        )
    text = await build_cohort_digest(db, date(2026, 6, 15), settings)
    assert text is not None
    assert "4-week decision point" not in text


async def test_build_cohort_digest_skips_final_block_before_lock_date(
    db_with_paper_trades, tmp_path,
):
    settings = _make_settings(tmp_path)
    db = db_with_paper_trades
    for i in range(11):
        await _insert_paper_trade(
            db, token_id=f"t{i}", signal_type="gainers_early",
            status="closed_tp", pnl_usd=10, would_be_live=1,
            closed_at=f"2026-05-1{i % 4}T0{i % 9}:00:00+00:00",
        )
    text = await build_cohort_digest(db, date(2026, 5, 17), settings)
    assert text is not None
    assert "4-week decision point" not in text


def test_window_string_format_matches_writer_format():
    """V30 SHOULD-FIX: window start/end ISO strings must lex-match writer format."""
    end_dt = datetime.combine(
        date(2026, 5, 17), datetime.min.time(), tzinfo=timezone.utc,
    )
    start_dt = end_dt - timedelta(days=7)
    s = start_dt.isoformat()
    assert s[10] == "T", f"window-start missing T separator: {s!r}"
    assert s.endswith("+00:00"), f"window-start missing TZ suffix: {s!r}"


# ---------------------------------------------------------------------------
# send_cohort_digest
# ---------------------------------------------------------------------------


async def test_send_cohort_digest_disabled_short_circuits(
    db_with_paper_trades, tmp_path,
):
    settings = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        DB_PATH=tmp_path / "scout.db",
        COHORT_DIGEST_ENABLED=False,
    )
    with patch(
        "scout.trading.cohort_digest.alerter.send_telegram_message",
        new=AsyncMock(),
    ) as mock_send:
        await send_cohort_digest(db_with_paper_trades, settings)
        assert mock_send.await_count == 0


async def test_send_cohort_digest_passes_parse_mode_none(
    db_with_paper_trades, tmp_path,
):
    """V29 SHOULD-FIX D8: every alerter call must carry parse_mode=None."""
    settings = _make_settings(tmp_path)
    db = db_with_paper_trades
    for i in range(11):
        await _insert_paper_trade(
            db, token_id=f"t{i}", signal_type="gainers_early",
            status="closed_tp", pnl_usd=10, would_be_live=1,
            closed_at=f"2026-05-1{i % 4}T0{i % 9}:00:00+00:00",
        )
    with patch(
        "scout.trading.cohort_digest.alerter.send_telegram_message",
        new=AsyncMock(),
    ) as mock_send:
        await send_cohort_digest(db, settings)
        assert mock_send.await_count >= 1
        for call in mock_send.await_args_list:
            assert call.kwargs.get("parse_mode") is None


async def test_send_cohort_digest_stamps_last_digest_date_after_dispatch(
    db_with_paper_trades, tmp_path,
):
    settings = _make_settings(tmp_path)
    db = db_with_paper_trades
    for i in range(11):
        await _insert_paper_trade(
            db, token_id=f"t{i}", signal_type="gainers_early",
            status="closed_tp", pnl_usd=10, would_be_live=1,
            closed_at=f"2026-05-1{i % 4}T0{i % 9}:00:00+00:00",
        )
    with patch(
        "scout.trading.cohort_digest.alerter.send_telegram_message",
        new=AsyncMock(),
    ):
        await send_cohort_digest(db, settings)
    state = await db.cohort_digest_read_state()
    assert state["last_digest_date"] == date.today().isoformat()
