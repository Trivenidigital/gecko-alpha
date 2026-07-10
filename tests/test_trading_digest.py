"""Tests for paper trading daily digest."""

import pytest

from scout.db import Database
from scout.trading.digest import build_paper_digest


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


async def test_digest_no_trades_returns_quiet_line_and_writes_row(db):
    """Quiet day: no opens/closes still writes a zeros summary row (so the
    downstream freshness watchdog sees a heartbeat) and returns an explicit
    one-liner, never None (datetime off-by-one #5 — see tasks/lessons.md)."""
    result = await build_paper_digest(db, "2026-04-09")
    assert result == "Paper digest 2026-04-09: no trades opened or closed."

    conn = db._conn
    cursor = await conn.execute(
        "SELECT date, trades_opened, trades_closed, wins, losses, "
        "total_pnl_usd, win_rate_pct FROM paper_daily_summary WHERE date = ?",
        ("2026-04-09",),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "2026-04-09"  # date
    assert row[1] == 0  # trades_opened
    assert row[2] == 0  # trades_closed
    assert row[3] == 0  # wins
    assert row[4] == 0  # losses
    assert row[5] == 0  # total_pnl_usd
    assert row[6] == 0  # win_rate_pct


async def test_digest_date_semantics_includes_yesterday_excludes_today(db):
    """Date-boundary pin (datetime off-by-one #5): a trade closed yesterday at
    23:50 UTC is in yesterday's digest; a trade closed today (00:05 UTC) is not.
    Both directions are asserted so the closed-date filter can't drift."""
    # Closed yesterday (2026-04-09) at 23:50 UTC — belongs in yesterday's digest.
    await _insert_trade(
        db,
        token_id="late_yday",
        symbol="LATE",
        signal_type="volume_spike",
        status="closed_tp",
        pnl_usd=100.0,
        pnl_pct=10.0,
        opened_at="2026-04-08T20:00:00+00:00",
        closed_at="2026-04-09T23:50:00+00:00",
        exit_price=55000.0,
        exit_reason="take_profit",
    )
    # Closed today (2026-04-10) at 00:05 UTC — must NOT leak into yesterday.
    await _insert_trade(
        db,
        token_id="early_today",
        symbol="EARLY",
        signal_type="volume_spike",
        status="closed_tp",
        pnl_usd=999.0,
        pnl_pct=99.0,
        opened_at="2026-04-08T21:00:00+00:00",
        closed_at="2026-04-10T00:05:00+00:00",
        exit_price=99999.0,
        exit_reason="take_profit",
    )

    yday = await build_paper_digest(db, "2026-04-09")
    assert yday is not None
    assert "0 opened, 1 closed" in yday
    assert "PnL: +$100.00" in yday
    assert "999" not in yday  # today's trade must not appear in yesterday's digest

    # And building for today captures the 00:05 trade, not yesterday's.
    today = await build_paper_digest(db, "2026-04-10")
    assert "0 opened, 1 closed" in today
    assert "EARLY" in today
    assert "LATE" not in today


def test_agent_daily_digest_passes_yesterday_not_today():
    """Wiring pin (datetime off-by-one #5): the daily-learn digest tick fires at
    ~01:00 UTC and must summarize the CLOSED period (yesterday), not the partial
    current day. Source-inspection guard in the style of
    test_narrative_agent_prune.py — a run-loop harness isn't warranted for a
    one-line wiring fix."""
    import inspect

    import scout.narrative.agent as narrative_agent

    src = inspect.getsource(narrative_agent)
    assert (
        "build_paper_digest(db, yesterday)" in src
    ), "daily digest must be built for yesterday (the closed day), not today"
    assert (
        "build_paper_digest(db, today)" not in src
    ), "digest must no longer be built for the partial current day"


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
