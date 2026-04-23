"""Tests for paper trading daily digest and BL-060 weekly A/B digest."""

import json
from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.trading.digest import build_paper_digest
from scout.trading.weekly_digest import _build_bl060_ab, _fmt_sharpe


@pytest.fixture
async def db(tmp_path):
    """Create in-memory database with paper_trades tables."""
    d = Database(str(tmp_path / "test.db"))
    await d.initialize()
    yield d
    await d.close()


async def _insert_trade(db, **overrides):
    """Helper to insert a paper trade row."""
    conn = db._conn
    defaults = {
        "token_id": "bitcoin",
        "symbol": "BTC",
        "name": "Bitcoin",
        "chain": "coingecko",
        "signal_type": "volume_spike",
        "signal_data": "{}",
        "entry_price": 50000.0,
        "amount_usd": 1000.0,
        "quantity": 0.02,
        "tp_pct": 20.0,
        "sl_pct": 10.0,
        "tp_price": 60000.0,
        "sl_price": 45000.0,
        "status": "open",
        "opened_at": "2026-04-09T12:00:00+00:00",
    }
    defaults.update(overrides)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" * len(defaults))
    await conn.execute(
        f"INSERT INTO paper_trades ({cols}) VALUES ({placeholders})",
        list(defaults.values()),
    )
    await conn.commit()


async def test_digest_no_trades_returns_none(db):
    result = await build_paper_digest(db, "2026-04-09")
    assert result is None


async def test_digest_opened_only(db):
    await _insert_trade(db, opened_at="2026-04-09T12:00:00+00:00", status="open")
    result = await build_paper_digest(db, "2026-04-09")
    assert result is not None
    assert "1 opened, 0 closed" in result
    assert "Paper Trading" in result


async def test_digest_with_closed_trades(db):
    # One winning trade
    await _insert_trade(
        db,
        token_id="bitcoin",
        symbol="BTC",
        signal_type="volume_spike",
        status="closed_tp",
        pnl_usd=200.0,
        pnl_pct=20.0,
        opened_at="2026-04-09T08:00:00+00:00",
        closed_at="2026-04-09T14:00:00+00:00",
        exit_price=60000.0,
        exit_reason="take_profit",
    )
    # One losing trade
    await _insert_trade(
        db,
        token_id="ethereum",
        symbol="ETH",
        name="Ethereum",
        signal_type="narrative_prediction",
        status="closed_sl",
        pnl_usd=-100.0,
        pnl_pct=-10.0,
        opened_at="2026-04-09T09:00:00+00:00",
        closed_at="2026-04-09T15:00:00+00:00",
        exit_price=2700.0,
        exit_reason="stop_loss",
    )

    result = await build_paper_digest(db, "2026-04-09")
    assert result is not None
    assert "2 opened, 2 closed" in result
    assert "PnL: +$100.00" in result
    assert "win rate: 50.0%" in result
    assert "Best: BTC +20.0% (+$200.00)" in result
    assert "Worst: ETH -10.0% (-$100.00)" in result


async def test_digest_by_signal_type(db):
    await _insert_trade(
        db,
        token_id="btc1",
        symbol="BTC",
        signal_type="volume_spike",
        status="closed_tp",
        pnl_usd=150.0,
        pnl_pct=15.0,
        opened_at="2026-04-09T08:00:00+00:00",
        closed_at="2026-04-09T14:00:00+00:00",
        exit_price=57500.0,
        exit_reason="take_profit",
    )
    await _insert_trade(
        db,
        token_id="eth1",
        symbol="ETH",
        name="Ethereum",
        signal_type="narrative_prediction",
        status="closed_sl",
        pnl_usd=-50.0,
        pnl_pct=-5.0,
        opened_at="2026-04-09T09:00:00+00:00",
        closed_at="2026-04-09T15:00:00+00:00",
        exit_price=2850.0,
        exit_reason="stop_loss",
    )

    result = await build_paper_digest(db, "2026-04-09")
    assert "By signal type:" in result
    assert "volume_spike: 1 trades, +$150.00 (100.0% WR)" in result
    assert "narrative_prediction: 1 trades, -$50.00 (0.0% WR)" in result


async def test_digest_stores_summary_in_db(db):
    await _insert_trade(
        db,
        token_id="sol1",
        symbol="SOL",
        signal_type="volume_spike",
        status="closed_tp",
        pnl_usd=300.0,
        pnl_pct=30.0,
        opened_at="2026-04-09T08:00:00+00:00",
        closed_at="2026-04-09T14:00:00+00:00",
        exit_price=130.0,
        exit_reason="take_profit",
    )

    await build_paper_digest(db, "2026-04-09")

    conn = db._conn
    cursor = await conn.execute(
        "SELECT * FROM paper_daily_summary WHERE date = '2026-04-09'"
    )
    row = await cursor.fetchone()
    assert row is not None
    # trades_opened=1, trades_closed=1, wins=1
    assert row[1] == "2026-04-09"  # date
    assert row[2] == 1  # trades_opened
    assert row[3] == 1  # trades_closed
    assert row[4] == 1  # wins
    assert row[5] == 0  # losses


async def test_digest_open_positions_exposure(db):
    # 2 open trades
    await _insert_trade(
        db,
        token_id="a",
        amount_usd=500.0,
        status="open",
        opened_at="2026-04-09T08:00:00+00:00",
    )
    await _insert_trade(
        db,
        token_id="b",
        amount_usd=700.0,
        status="open",
        opened_at="2026-04-09T09:00:00+00:00",
    )

    result = await build_paper_digest(db, "2026-04-09")
    assert result is not None
    assert "Open: 2 positions ($1200.00 exposure)" in result


# ---------------------------------------------------------------------------
# BL-060 weekly digest A/B tests
# ---------------------------------------------------------------------------

_CLOSED_INSERT = (
    "INSERT INTO paper_trades "
    "(token_id, symbol, name, chain, signal_type, signal_data, "
    " entry_price, amount_usd, quantity, tp_pct, sl_pct, "
    " tp_price, sl_price, status, opened_at, "
    " pnl_pct, signal_combo, "
    " lead_time_vs_trending_min, lead_time_vs_trending_status, "
    " would_be_live) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_CLOSED_VALS = lambda tid, wbl, pnl, opened="2026-04-25T00:00:00": (
    tid, "S", "N", "eth", "first_signal", "{}",
    1.0, 100.0, 100.0, 40.0, 20.0, 1.4, 0.8,
    "closed_tp", opened,
    pnl, "first_signal", None, None, wbl,
)


@pytest.mark.asyncio
async def test_digest_ab_cohort_excludes_nulls(tmp_path, settings_factory):
    """Test #9 — cohort query filters would_be_live IS NOT NULL."""
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()

    for i in range(3):
        await db._conn.execute(_CLOSED_INSERT, _CLOSED_VALS(f"L{i}", 1, 5.0))
        await db._conn.execute(_CLOSED_INSERT, _CLOSED_VALS(f"B{i}", 0, -2.0))
        await db._conn.execute(_CLOSED_INSERT, _CLOSED_VALS(f"N{i}", None, 10.0))
    await db._conn.commit()

    section = await _build_bl060_ab(
        db, end_date=datetime(2026, 5, 2), settings=settings_factory(),
    )
    assert "live-eligible" in section.lower()
    assert "n_closed=3" in section
    await db.close()


@pytest.mark.asyncio
async def test_digest_ab_excludes_both_null_regimes(tmp_path, settings_factory):
    """Test #14 — both NULL regimes excluded; only wbl=1 and wbl=0 count."""
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()

    for i in range(2):
        await db._conn.execute(_CLOSED_INSERT, _CLOSED_VALS(f"NULL{i}", None, 3.0, "2026-04-27T00:00:00"))
        await db._conn.execute(_CLOSED_INSERT, _CLOSED_VALS(f"LIVE{i}", 1, 5.0, "2026-04-27T00:00:00"))
        await db._conn.execute(_CLOSED_INSERT, _CLOSED_VALS(f"CAP{i}", 0, -2.0, "2026-04-27T00:00:00"))
    await db._conn.commit()

    section = await _build_bl060_ab(
        db, end_date=datetime(2026, 5, 2), settings=settings_factory(),
    )
    assert "n_closed=2 | " in section
    assert section.count("n_closed=2") >= 4
    await db.close()


def test_sharpe_noisy_below_30():
    """Test #15a — noisy annotation for n < 30."""
    assert "(n_closed=22, noisy)" in _fmt_sharpe(0.42, 22)
    assert "(n_closed=29, noisy)" in _fmt_sharpe(0.42, 29)


def test_sharpe_plain_at_30_and_above():
    """Test #15b — plain value at n >= 30."""
    assert _fmt_sharpe(0.42, 30) == "0.42"
    assert _fmt_sharpe(0.42, 31) == "0.42"


def test_sharpe_dash_on_zero_n():
    """Test #15c — dash when n == 0 or x is None."""
    assert _fmt_sharpe(None, 0) == "-"
    assert _fmt_sharpe(0.42, 0) == "-"


@pytest.mark.asyncio
async def test_first_week_post_cutover_zero_n_guard(tmp_path, settings_factory):
    """Test #16 — previous week zero-n renders '-' not crash."""
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()

    # Only seed current-week trades (no previous-week data)
    await db._conn.execute(_CLOSED_INSERT, _CLOSED_VALS("cur1", 1, 5.0, "2026-04-27T00:00:00"))
    await db._conn.execute(_CLOSED_INSERT, _CLOSED_VALS("cur2", 0, 5.0, "2026-04-27T00:00:00"))
    await db._conn.commit()

    section = await _build_bl060_ab(
        db, end_date=datetime(2026, 5, 2), settings=settings_factory(),
    )
    assert "| -" in section or "| - " in section, (
        "last-week zero-n must render '-'; section:\n" + section
    )
    await db.close()


@pytest.mark.asyncio
async def test_delta_excludes_sharpe_under_small_n(tmp_path, settings_factory):
    """Test #18 — delta omits Sharpe row when either side has n_closed < 30."""
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()

    # live side: 25 trades (< 30), beyond side: 60 trades (>= 30)
    for i in range(25):
        await db._conn.execute(
            _CLOSED_INSERT,
            _CLOSED_VALS(f"L{i}", 1, 5.0 + i * 0.1, "2026-04-27T00:00:00"),
        )
    for i in range(60):
        await db._conn.execute(
            _CLOSED_INSERT,
            _CLOSED_VALS(f"C{i}", 0, -1.0 + i * 0.05, "2026-04-27T00:00:00"),
        )
    await db._conn.commit()

    section = await _build_bl060_ab(
        db, end_date=datetime(2026, 5, 2), settings=settings_factory(),
    )
    delta_block = section.split("Delta (live-eligible minus beyond-cap):")[1].split(
        "Per-path"
    )[0]
    assert "Win-rate:" in delta_block
    assert "Avg P&L:" in delta_block
    assert "Sharpe:" not in delta_block, (
        "delta must omit Sharpe row when either side has n_closed<30; got:\n"
        + delta_block
    )
    await db.close()
