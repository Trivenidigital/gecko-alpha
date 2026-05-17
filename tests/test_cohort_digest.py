"""Tests for scout.trading.cohort_digest — BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST cycle 5.

Isolated from tests/test_main.py to avoid OPENSSL_Uplink trigger on Windows
local dev (memory reference_windows_openssl_workaround.md). Imports
scout.trading.cohort_digest directly rather than going through scout.main.
"""

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading.cohort_digest import (
    _LIVE_ELIGIBLE_ENUMERATED_TYPES,
    _classify_verdict,
    _compute_all_cohorts_stats,
    _compute_signal_cohort_stats,
)


# ---------------------------------------------------------------------------
# _classify_verdict (13 unit tests covering the dashboard-mirror rule)
# ---------------------------------------------------------------------------


def test_classify_verdict_strong_pattern_all_four_conditions():
    """signFlip + |fPnl|>=200 + |ePnl|>=200 + |wrDelta|>15 (STRICT)."""
    v = _classify_verdict(
        eN=15, fN=30, wrDelta=16.0, fPnl=300, ePnl=-250,
        signal_type="gainers_early", n_gate=10,
    )
    assert v == "strong-pattern (exploratory)"


def test_classify_verdict_no_signflip_blocks_strong_pattern():
    """V32 MUST-ADD: AND-conjunction regression catcher. Both PnL positive
    + |wrDelta|>15 + both above floor → no signFlip → moderate, NOT
    strong-pattern. Catches a future refactor flipping `signFlipRaw and ...`
    to `signFlipRaw or ...` in _classify_verdict."""
    v = _classify_verdict(
        eN=15, fN=30, wrDelta=20.0, fPnl=300, ePnl=250,
        signal_type="gainers_early", n_gate=10,
    )
    assert v == "moderate"


def test_classify_verdict_strict_inequality_at_15pp_falls_to_moderate():
    """exactly 15pp does NOT qualify (STRICT >); falls to moderate via |wrDelta|>5."""
    v = _classify_verdict(
        eN=15, fN=30, wrDelta=15.0, fPnl=300, ePnl=-250,
        signal_type="gainers_early", n_gate=10,
    )
    assert v == "moderate"


def test_classify_verdict_pnl_floor_not_met_falls_to_moderate():
    """signFlip + |wrDelta|>15 BUT |fPnl|<200 → moderate, not strong-pattern."""
    v = _classify_verdict(
        eN=15, fN=30, wrDelta=20.0, fPnl=150, ePnl=-250,
        signal_type="gainers_early", n_gate=10,
    )
    assert v == "moderate"


def test_classify_verdict_eligible_pnl_floor_not_met_falls_to_moderate():
    """Symmetric: |ePnl|<200 also blocks strong-pattern."""
    v = _classify_verdict(
        eN=15, fN=30, wrDelta=20.0, fPnl=300, ePnl=-150,
        signal_type="gainers_early", n_gate=10,
    )
    assert v == "moderate"


def test_classify_verdict_moderate_via_signflip_alone():
    """signFlip + wrDelta within band → moderate."""
    v = _classify_verdict(
        eN=15, fN=30, wrDelta=2.0, fPnl=100, ePnl=-50,
        signal_type="gainers_early", n_gate=10,
    )
    assert v == "moderate"


def test_classify_verdict_moderate_via_wrgap_alone():
    """No signFlip + |wrDelta|=6 > 5 → moderate."""
    v = _classify_verdict(
        eN=15, fN=30, wrDelta=6.0, fPnl=300, ePnl=200,
        signal_type="gainers_early", n_gate=10,
    )
    assert v == "moderate"


def test_classify_verdict_tracking_below_moderate_threshold():
    """No signFlip + |wrDelta|<5 + no PnL extremes → tracking."""
    v = _classify_verdict(
        eN=15, fN=30, wrDelta=2.0, fPnl=300, ePnl=200,
        signal_type="gainers_early", n_gate=10,
    )
    assert v == "tracking"


def test_classify_verdict_moderate_strict_at_5pp_boundary():
    """STRICT > on moderate gap: exactly 5pp falls to tracking."""
    v = _classify_verdict(
        eN=15, fN=30, wrDelta=5.0, fPnl=300, ePnl=200,
        signal_type="gainers_early", n_gate=10,
    )
    assert v == "tracking"


def test_classify_verdict_near_identical_chain_completed():
    """chain_completed always near-identical regardless of stats."""
    v = _classify_verdict(
        eN=50, fN=60, wrDelta=20.0, fPnl=500, ePnl=-400,
        signal_type="chain_completed", n_gate=10,
    )
    assert v == "near-identical"


def test_classify_verdict_insufficient_data_n_zero():
    v = _classify_verdict(
        eN=0, fN=10, wrDelta=None, fPnl=0, ePnl=0,
        signal_type="gainers_early", n_gate=10,
    )
    assert v == "INSUFFICIENT_DATA (n=0)"


def test_classify_verdict_insufficient_data_n_below_gate():
    v = _classify_verdict(
        eN=5, fN=10, wrDelta=3.0, fPnl=100, ePnl=50,
        signal_type="gainers_early", n_gate=10,
    )
    assert v == "INSUFFICIENT_DATA (n=5, need >=10)"


def test_classify_verdict_insufficient_data_at_n_gate_minus_one():
    v = _classify_verdict(
        eN=9, fN=10, wrDelta=3.0, fPnl=100, ePnl=50,
        signal_type="gainers_early", n_gate=10,
    )
    assert v == "INSUFFICIENT_DATA (n=9, need >=10)"


def test_classify_verdict_at_n_gate_evaluates_verdict():
    """eN == n_gate → eligible for verdict (NOT INSUFFICIENT_DATA)."""
    v = _classify_verdict(
        eN=10, fN=20, wrDelta=2.0, fPnl=300, ePnl=200,
        signal_type="gainers_early", n_gate=10,
    )
    assert v == "tracking"


# ---------------------------------------------------------------------------
# _compute_all_cohorts_stats / _compute_signal_cohort_stats
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_with_paper_trades(tmp_path):
    db = Database(str(tmp_path / "cohort.db"))
    await db.initialize()
    yield db
    await db.close()


async def _insert_paper_trade(
    db, *, token_id, signal_type, status, pnl_usd, would_be_live,
    closed_at, opened_at=None,
):
    """Minimal direct INSERT for test seeding — avoids the full
    Database.create_paper_trade dependency surface."""
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


async def test_compute_stats_uses_status_not_open_not_literal_closed(
    db_with_paper_trades,
):
    """V27 MUST-FIX: status != 'open' covers closed_tp/closed_sl/expired,
    NOT just literal status='closed'."""
    db = db_with_paper_trades
    start = datetime(2026, 5, 10, tzinfo=timezone.utc)
    end = datetime(2026, 5, 17, tzinfo=timezone.utc)
    closed_at = "2026-05-13T10:00:00+00:00"

    # 4 closed-variant trades for gainers_early (all should count)
    await _insert_paper_trade(db, token_id="t1", signal_type="gainers_early",
                              status="closed_tp", pnl_usd=50, would_be_live=1, closed_at=closed_at)
    await _insert_paper_trade(db, token_id="t2", signal_type="gainers_early",
                              status="closed_sl", pnl_usd=-30, would_be_live=1, closed_at=closed_at)
    await _insert_paper_trade(db, token_id="t3", signal_type="gainers_early",
                              status="closed_moonshot", pnl_usd=200, would_be_live=0, closed_at=closed_at)
    await _insert_paper_trade(db, token_id="t4", signal_type="gainers_early",
                              status="expired", pnl_usd=-10, would_be_live=0, closed_at=closed_at)
    # 1 open trade must NOT count
    await _insert_paper_trade(db, token_id="t5", signal_type="gainers_early",
                              status="open", pnl_usd=None, would_be_live=1, closed_at=None,
                              opened_at="2026-05-13T08:00:00+00:00")

    stats = await _compute_signal_cohort_stats(
        db, signal_type="gainers_early", start=start, end=end,
    )
    assert stats["fN"] == 4  # all 4 closed-variants, open excluded
    assert stats["eN"] == 2  # t1, t2 only (would_be_live=1)


async def test_compute_stats_window_filter_on_closed_at(db_with_paper_trades):
    """V27 MUST-FIX: window column is closed_at, NOT opened_at. A trade
    opened in window but still open (closed_at NULL) must NOT count."""
    db = db_with_paper_trades
    start = datetime(2026, 5, 10, tzinfo=timezone.utc)
    end = datetime(2026, 5, 17, tzinfo=timezone.utc)

    # Trade opened in window but still open — closed_at=NULL → excluded
    await _insert_paper_trade(db, token_id="t1", signal_type="gainers_early",
                              status="open", pnl_usd=None, would_be_live=1, closed_at=None,
                              opened_at="2026-05-12T00:00:00+00:00")
    # Trade closed outside window (after end) — excluded
    await _insert_paper_trade(db, token_id="t2", signal_type="gainers_early",
                              status="closed_tp", pnl_usd=50, would_be_live=1,
                              closed_at="2026-05-18T00:00:00+00:00")
    # Trade closed in window — INCLUDED
    await _insert_paper_trade(db, token_id="t3", signal_type="gainers_early",
                              status="closed_tp", pnl_usd=80, would_be_live=1,
                              closed_at="2026-05-13T00:00:00+00:00")

    stats = await _compute_signal_cohort_stats(
        db, signal_type="gainers_early", start=start, end=end,
    )
    assert stats["fN"] == 1
    assert stats["eN"] == 1


async def test_compute_stats_win_definition_pnl_usd_not_pnl_pct(db_with_paper_trades):
    """V27 MUST-FIX: wins counted on pnl_usd > 0 (matches dashboard)."""
    db = db_with_paper_trades
    start = datetime(2026, 5, 10, tzinfo=timezone.utc)
    end = datetime(2026, 5, 17, tzinfo=timezone.utc)
    closed_at = "2026-05-13T10:00:00+00:00"

    # 3 wins (pnl_usd > 0) + 2 losses
    for i, pnl in enumerate([100, 50, 25, -30, -10]):
        await _insert_paper_trade(
            db, token_id=f"t{i}", signal_type="gainers_early",
            status="closed_tp" if pnl > 0 else "closed_sl",
            pnl_usd=pnl, would_be_live=1, closed_at=closed_at,
        )

    stats = await _compute_signal_cohort_stats(
        db, signal_type="gainers_early", start=start, end=end,
    )
    assert stats["eN"] == 5
    assert stats["eWins"] == 3
    assert stats["eWr"] == pytest.approx(60.0, abs=0.01)


async def test_compute_stats_eligible_subset_of_full(db_with_paper_trades):
    """Eligible cohort is a subset of full cohort (would_be_live=1 filter)."""
    db = db_with_paper_trades
    start = datetime(2026, 5, 10, tzinfo=timezone.utc)
    end = datetime(2026, 5, 17, tzinfo=timezone.utc)
    closed_at = "2026-05-13T10:00:00+00:00"

    # 5 trades: 2 with would_be_live=1, 3 with would_be_live=0/NULL
    await _insert_paper_trade(db, token_id="t1", signal_type="gainers_early",
                              status="closed_tp", pnl_usd=50, would_be_live=1, closed_at=closed_at)
    await _insert_paper_trade(db, token_id="t2", signal_type="gainers_early",
                              status="closed_tp", pnl_usd=75, would_be_live=1, closed_at=closed_at)
    await _insert_paper_trade(db, token_id="t3", signal_type="gainers_early",
                              status="closed_tp", pnl_usd=100, would_be_live=0, closed_at=closed_at)
    await _insert_paper_trade(db, token_id="t4", signal_type="gainers_early",
                              status="closed_sl", pnl_usd=-20, would_be_live=0, closed_at=closed_at)
    await _insert_paper_trade(db, token_id="t5", signal_type="gainers_early",
                              status="closed_sl", pnl_usd=-15, would_be_live=None, closed_at=closed_at)

    stats = await _compute_signal_cohort_stats(
        db, signal_type="gainers_early", start=start, end=end,
    )
    assert stats["fN"] == 5  # full = all closed
    assert stats["eN"] == 2  # eligible = would_be_live=1 only
    assert stats["fPnl"] == pytest.approx(190.0, abs=0.01)
    assert stats["ePnl"] == pytest.approx(125.0, abs=0.01)


async def test_compute_stats_handles_zero_n_no_division_by_zero(db_with_paper_trades):
    """V27 SHOULD-FIX: zero trades returns no-div-by-zero defaults."""
    db = db_with_paper_trades
    start = datetime(2026, 5, 10, tzinfo=timezone.utc)
    end = datetime(2026, 5, 17, tzinfo=timezone.utc)

    stats = await _compute_signal_cohort_stats(
        db, signal_type="gainers_early", start=start, end=end,
    )
    assert stats["fN"] == 0
    assert stats["eN"] == 0
    assert stats["fWr"] == 0.0
    assert stats["eWr"] is None
    assert stats["wrDelta"] is None


async def test_compute_all_cohorts_returns_every_enumerated_type(db_with_paper_trades):
    """Even types with zero trades appear with zero values, so the digest
    iterator can render an exhaustive row set."""
    db = db_with_paper_trades
    start = datetime(2026, 5, 10, tzinfo=timezone.utc)
    end = datetime(2026, 5, 17, tzinfo=timezone.utc)

    all_stats = await _compute_all_cohorts_stats(db, start=start, end=end)
    assert set(all_stats.keys()) == set(_LIVE_ELIGIBLE_ENUMERATED_TYPES)
    for sig in _LIVE_ELIGIBLE_ENUMERATED_TYPES:
        assert all_stats[sig]["fN"] == 0


async def test_compute_all_cohorts_skips_non_enumerated_signals(db_with_paper_trades):
    """A trade for losers_contrarian (NOT in enumerated types) doesn't appear."""
    db = db_with_paper_trades
    start = datetime(2026, 5, 10, tzinfo=timezone.utc)
    end = datetime(2026, 5, 17, tzinfo=timezone.utc)
    closed_at = "2026-05-13T10:00:00+00:00"

    await _insert_paper_trade(db, token_id="t1", signal_type="losers_contrarian",
                              status="closed_tp", pnl_usd=50, would_be_live=1, closed_at=closed_at)

    all_stats = await _compute_all_cohorts_stats(db, start=start, end=end)
    assert "losers_contrarian" not in all_stats
