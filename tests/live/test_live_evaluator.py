"""LIVE-02 live evaluator tests — flat TP/SL/duration close over open
``live_trades`` via a real venue sell (mocked adapter), symmetric to the
shadow evaluator. Also covers the daily-loss re-check (LIVE-04), non-filled
exit handling (LIVE-08), the entry-fill-null skip, and the stuck-open watchdog.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import structlog

from scout.config import Settings
from scout.db import Database
from scout.live.adapter_base import OrderConfirmation
from scout.live.config import LiveConfig
from scout.live.kill_switch import KillSwitch
from scout.live import live_evaluator
from scout.live.live_evaluator import evaluate_open_live_trades


def _settings(**overrides):
    base = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        LIVE_MODE="live",
        LIVE_TRADING_ENABLED=True,
        LIVE_SIGNAL_ALLOWLIST="first_signal",
        LIVE_TP_PCT=Decimal("20"),
        LIVE_SL_PCT=Decimal("10"),
        LIVE_MAX_DURATION_HOURS=24,
        LIVE_TRADE_AMOUNT_USD=Decimal("100"),
        LIVE_DAILY_LOSS_CAP_USD=Decimal("50"),
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


async def _seed_paper_trade(db, *, signal_type="first_signal"):
    assert db._conn is not None
    await db._conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
        "signal_data, entry_price, amount_usd, quantity, tp_price, sl_price, "
        "status, opened_at) VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?)",
        (
            "c",
            "L",
            "N",
            "eth",
            signal_type,
            "{}",
            1.0,
            100.0,
            100.0,
            1.2,
            0.9,
            "open",
            "2026-07-11T00:00:00+00:00",
        ),
    )
    await db._conn.commit()
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    return (await cur.fetchone())[0]


async def _seed_open_live(
    db,
    *,
    paper_trade_id,
    entry_fill_price: str | None = "100",
    entry_fill_qty: str | None = "1",
    size_usd: str = "100",
    created_at: str | None = None,
    cid: str = "gecko-1-abcd1234",
):
    assert db._conn is not None
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO live_trades "
        "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, size_usd, "
        " entry_fill_price, entry_fill_qty, mid_at_entry, status, "
        " client_order_id, created_at) "
        "VALUES (?,'c','L','binance','LUSDT','first_signal',?, ?, ?, '100', "
        " 'open', ?, ?)",
        (paper_trade_id, size_usd, entry_fill_price, entry_fill_qty, cid, created_at),
    )
    await db._conn.commit()
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    return (await cur.fetchone())[0]


def _adapter(*, price=None, price_exc=None, exit_conf=None, exit_exc=None):
    adapter = MagicMock()
    if price_exc is not None:
        adapter.fetch_price = AsyncMock(side_effect=price_exc)
    else:
        adapter.fetch_price = AsyncMock(
            return_value=price if price is not None else Decimal("100")
        )
    if exit_exc is not None:
        adapter.place_exit_order = AsyncMock(side_effect=exit_exc)
    else:
        adapter.place_exit_order = AsyncMock(return_value=exit_conf)
    return adapter


def _conf(status="filled", *, fill_price=None, filled_qty=1.0, order_id="EXIT-1"):
    return OrderConfirmation(
        venue="binance",
        venue_order_id=order_id,
        client_order_id="gecko-x-1",
        status=status,
        filled_qty=filled_qty,
        fill_price=fill_price,
        raw_response=None,
    )


# ---------- close-path tests ----------


async def test_tp_close_writes_positive_realized_pnl(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt = await _seed_paper_trade(db)
        lid = await _seed_open_live(db, paper_trade_id=pt)
        settings = _settings(LIVE_TRADING_ENABLED=False)  # isolate from cap
        adapter = _adapter(
            price=Decimal("130"),  # +30% > 20% TP
            exit_conf=_conf(fill_price=130.0, filled_qty=1.0),
        )
        n = await evaluate_open_live_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=KillSwitch(db),
            settings=settings,
        )
        assert n == 1
        adapter.place_exit_order.assert_awaited_once()
        # sold entry_fill_qty base units, keyed by deterministic exit cid
        call = adapter.place_exit_order.call_args
        assert call.kwargs["base_qty"] == Decimal("1")
        assert call.kwargs["client_order_id"] == f"gecko-x-{lid}"
        cur = await db._conn.execute(
            "SELECT status, realized_pnl_usd, exit_fill_price, exit_order_id "
            "FROM live_trades WHERE id=?",
            (lid,),
        )
        row = await cur.fetchone()
        assert row[0] == "closed_tp"
        assert Decimal(row[1]) == Decimal("30")  # 130*1 - 100*1
        assert Decimal(row[2]) == Decimal("130")
        assert row[3] == "EXIT-1"
    finally:
        await db.close()


async def test_sl_close_writes_negative_realized_pnl(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt = await _seed_paper_trade(db)
        lid = await _seed_open_live(db, paper_trade_id=pt)
        settings = _settings(LIVE_TRADING_ENABLED=False)
        adapter = _adapter(
            price=Decimal("85"),  # -15% < -10% SL
            exit_conf=_conf(fill_price=85.0, filled_qty=1.0),
        )
        n = await evaluate_open_live_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=KillSwitch(db),
            settings=settings,
        )
        assert n == 1
        cur = await db._conn.execute(
            "SELECT status, realized_pnl_usd FROM live_trades WHERE id=?", (lid,)
        )
        row = await cur.fetchone()
        assert row[0] == "closed_sl"
        assert Decimal(row[1]) == Decimal("-15")
    finally:
        await db.close()


async def test_duration_close(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt = await _seed_paper_trade(db)
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        lid = await _seed_open_live(db, paper_trade_id=pt, created_at=old)
        settings = _settings(LIVE_TRADING_ENABLED=False)
        adapter = _adapter(
            price=Decimal("103"),  # inside TP/SL bounds → duration wins
            exit_conf=_conf(fill_price=103.0, filled_qty=1.0),
        )
        n = await evaluate_open_live_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=KillSwitch(db),
            settings=settings,
        )
        assert n == 1
        cur = await db._conn.execute(
            "SELECT status FROM live_trades WHERE id=?", (lid,)
        )
        assert (await cur.fetchone())[0] == "closed_duration"
    finally:
        await db.close()


async def test_not_crossed_stays_open(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt = await _seed_paper_trade(db)
        lid = await _seed_open_live(db, paper_trade_id=pt)
        settings = _settings(LIVE_TRADING_ENABLED=False)
        adapter = _adapter(price=Decimal("105"), exit_conf=_conf(fill_price=105.0))
        n = await evaluate_open_live_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=KillSwitch(db),
            settings=settings,
        )
        assert n == 0
        adapter.place_exit_order.assert_not_called()
        cur = await db._conn.execute(
            "SELECT status FROM live_trades WHERE id=?", (lid,)
        )
        assert (await cur.fetchone())[0] == "open"
    finally:
        await db.close()


async def test_exit_rejected_flags_needs_manual_review(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt = await _seed_paper_trade(db)
        lid = await _seed_open_live(db, paper_trade_id=pt)
        settings = _settings(LIVE_TRADING_ENABLED=False)
        adapter = _adapter(
            price=Decimal("130"),
            exit_conf=_conf(status="rejected", fill_price=None, filled_qty=None),
        )
        alerts = []

        async def _hook(msg):
            alerts.append(msg)

        n = await evaluate_open_live_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=KillSwitch(db),
            settings=settings,
            alert_hook=_hook,
        )
        assert n == 0  # not a clean close
        cur = await db._conn.execute(
            "SELECT status, realized_pnl_usd FROM live_trades WHERE id=?", (lid,)
        )
        row = await cur.fetchone()
        assert row[0] == "needs_manual_review"
        assert row[1] is None  # no realized PnL booked on a failed sell
        assert len(alerts) == 1  # §12b operator alert fired
    finally:
        await db.close()


async def test_entry_fill_null_skipped(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt = await _seed_paper_trade(db)
        lid = await _seed_open_live(
            db, paper_trade_id=pt, entry_fill_price=None, entry_fill_qty=None
        )
        settings = _settings(LIVE_TRADING_ENABLED=False)
        adapter = _adapter(price=Decimal("130"), exit_conf=_conf(fill_price=130.0))
        with structlog.testing.capture_logs() as logs:
            n = await evaluate_open_live_trades(
                db=db,
                adapter=adapter,
                config=LiveConfig(settings),
                ks=KillSwitch(db),
                settings=settings,
            )
        assert n == 0
        adapter.place_exit_order.assert_not_called()
        assert any(
            le.get("event") == "live_eval_entry_fill_null_skipped" for le in logs
        )
        cur = await db._conn.execute(
            "SELECT status FROM live_trades WHERE id=?", (lid,)
        )
        assert (await cur.fetchone())[0] == "open"
    finally:
        await db.close()


async def test_daily_cap_trips_after_live_loss(tmp_path):
    """LIVE-02 close + LIVE-04 union: a live SL close breaching the cap trips the
    kill switch via the post-close re-check."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt = await _seed_paper_trade(db)
        # entry 100 x qty 1 → SL exit at 40 books -60 < -50 cap
        await _seed_open_live(
            db, paper_trade_id=pt, entry_fill_price="100", entry_fill_qty="1"
        )
        settings = _settings(
            LIVE_TRADING_ENABLED=True, LIVE_DAILY_LOSS_CAP_USD=Decimal("50")
        )
        ks = KillSwitch(db)
        adapter = _adapter(
            price=Decimal("40"), exit_conf=_conf(fill_price=40.0, filled_qty=1.0)
        )
        n = await evaluate_open_live_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=ks,
            settings=settings,
        )
        assert n == 1
        assert await ks.is_active() is not None
    finally:
        await db.close()


async def test_exit_place_error_leaves_open_and_warns_stuck(tmp_path):
    """A failing sell leaves the row open (retried next tick); an open row older
    than max_duration + grace trips the §12a stuck-open watchdog."""
    live_evaluator._last_stuck_open_warn_date = None
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt = await _seed_paper_trade(db)
        old = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
        lid = await _seed_open_live(db, paper_trade_id=pt, created_at=old)
        settings = _settings(LIVE_TRADING_ENABLED=False)
        adapter = _adapter(price=Decimal("103"), exit_exc=RuntimeError("venue down"))
        with structlog.testing.capture_logs() as logs:
            n = await evaluate_open_live_trades(
                db=db,
                adapter=adapter,
                config=LiveConfig(settings),
                ks=KillSwitch(db),
                settings=settings,
            )
        assert n == 0
        cur = await db._conn.execute(
            "SELECT status FROM live_trades WHERE id=?", (lid,)
        )
        assert (await cur.fetchone())[0] == "open"  # still held; retried later
        assert any(le.get("event") == "live_stuck_open" for le in logs)
        assert any(le.get("event") == "live_exit_place_failed" for le in logs)
    finally:
        await db.close()
