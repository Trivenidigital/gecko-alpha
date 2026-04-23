"""Boot-time reconciliation tests (spec §10.5).

Covers:
1. Empty DB still emits ``live_boot_reconciliation_done`` — absence of log !=
   success, so the terminal log must ALWAYS fire.
2. Drift window log captures the earliest open ``created_at``.
3. Row crossed above TP is closed as ``closed_via_reconciliation`` with
   positive PnL and the crossed kind recorded in the WARN log.
4. Row crossed below SL is closed as ``closed_via_reconciliation`` with
   negative PnL and the SL kind recorded in the WARN log.
5. Row within bounds remains open, ``rows_resumed`` increments.
6. ``fetch_price`` raising a transient error leaves the row open, logs
   ``live_boot_reconciliation_row_err``, and still emits ``_done``.
7. ``emit_live_startup_status`` emits the event with Binance reachable.
8. ``emit_live_startup_status`` records ``binance_reachable=False`` when the
   probe raises and still emits the event.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import structlog

from scout.config import Settings
from scout.db import Database
from scout.live.config import LiveConfig
from scout.live.exceptions import VenueTransientError
from scout.live.kill_switch import KillSwitch
from scout.live.reconciliation import (
    emit_live_startup_status,
    reconcile_open_shadow_trades,
)

# ---------- helpers ----------------------------------------------------------


def _settings(**overrides):
    base = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        LIVE_MODE="shadow",
        LIVE_SIGNAL_ALLOWLIST="first_signal",
        LIVE_TP_PCT=Decimal("20"),
        LIVE_SL_PCT=Decimal("10"),
        LIVE_MAX_DURATION_HOURS=24,
        LIVE_TRADE_AMOUNT_USD=Decimal("100"),
        LIVE_DAILY_LOSS_CAP_USD=Decimal("50"),
    )
    base.update(overrides)
    return Settings(**base)


async def _seed_paper_trade(db, *, coin_id="c", symbol="T", signal_type="first_signal"):
    assert db._conn is not None
    await db._conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
        "signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct, "
        "tp_price, sl_price, status, opened_at) VALUES "
        "(?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?)",
        (
            coin_id,
            symbol,
            "N",
            "eth",
            signal_type,
            "{}",
            1.0,
            100.0,
            100.0,
            20.0,
            10.0,
            1.2,
            0.9,
            "open",
            "2026-04-23T00:00:00",
        ),
    )
    await db._conn.commit()
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    return (await cur.fetchone())[0]


async def _seed_open_shadow(
    db,
    *,
    paper_trade_id,
    entry_vwap: str | None = "100",
    size_usd: str = "100",
    signal_type: str = "first_signal",
    created_at: str | None = None,
):
    assert db._conn is not None
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO shadow_trades "
        "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, size_usd, "
        " entry_walked_vwap, mid_at_entry, status, created_at) "
        "VALUES (?,'c','T','binance','TUSDT',?,?, ?, '100', 'open', ?)",
        (paper_trade_id, signal_type, size_usd, entry_vwap, created_at),
    )
    await db._conn.commit()
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    return (await cur.fetchone())[0]


def _make_adapter(*, fetch_price=None, fetch_price_exc=None):
    adapter = AsyncMock()
    if fetch_price_exc is not None:
        adapter.fetch_price = AsyncMock(side_effect=fetch_price_exc)
    else:
        adapter.fetch_price = AsyncMock(
            return_value=fetch_price if fetch_price is not None else Decimal("100")
        )
    return adapter


# ---------- tests ------------------------------------------------------------


async def test_zero_rows_still_logs_done(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        settings = _settings()
        adapter = _make_adapter()
        with structlog.testing.capture_logs() as logs:
            await reconcile_open_shadow_trades(
                db=db,
                adapter=adapter,
                config=LiveConfig(settings),
                ks=KillSwitch(db),
                settings=settings,
            )
        done = [le for le in logs if le.get("event") == "live_boot_reconciliation_done"]
        assert len(done) == 1
        assert done[0]["rows_inspected"] == 0
        assert done[0]["rows_closed"] == 0
        assert done[0]["rows_resumed"] == 0
    finally:
        await db.close()


async def test_drift_window_logged_with_earliest_open(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        created = "2026-04-22T00:00:00+00:00"
        await _seed_open_shadow(db, paper_trade_id=pt_id, created_at=created)

        settings = _settings()
        adapter = _make_adapter(fetch_price=Decimal("100"))
        with structlog.testing.capture_logs() as logs:
            await reconcile_open_shadow_trades(
                db=db,
                adapter=adapter,
                config=LiveConfig(settings),
                ks=KillSwitch(db),
                settings=settings,
            )
        drift = [
            le
            for le in logs
            if le.get("event") == "live_boot_reconciliation_drift_window"
        ]
        assert len(drift) == 1
        assert drift[0]["earliest_open_created_at"] == created
        assert drift[0]["restart_at"] is not None
    finally:
        await db.close()


async def test_tp_crossed_during_downtime_closed_as_reconciliation(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        sid = await _seed_open_shadow(db, paper_trade_id=pt_id)

        settings = _settings()
        adapter = _make_adapter(fetch_price=Decimal("130"))  # +30 % > 20 % TP

        with structlog.testing.capture_logs() as logs:
            await reconcile_open_shadow_trades(
                db=db,
                adapter=adapter,
                config=LiveConfig(settings),
                ks=KillSwitch(db),
                settings=settings,
            )

        cur = await db._conn.execute(
            "SELECT status, realized_pnl_usd FROM shadow_trades WHERE id=?",
            (sid,),
        )
        row = await cur.fetchone()
        assert row[0] == "closed_via_reconciliation"
        assert Decimal(row[1]) > Decimal("0")

        closed_events = [
            le for le in logs if le.get("event") == "live_boot_reconciliation_closed"
        ]
        assert len(closed_events) == 1
        assert closed_events[0]["crossed_reason"] == "tp_crossed"
        assert closed_events[0]["shadow_trade_id"] == sid

        done = [le for le in logs if le.get("event") == "live_boot_reconciliation_done"]
        assert done[0]["rows_closed"] == 1
        assert done[0]["rows_inspected"] == 1
    finally:
        await db.close()


async def test_sl_crossed_during_downtime_closed_as_reconciliation(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        sid = await _seed_open_shadow(db, paper_trade_id=pt_id)

        settings = _settings()
        adapter = _make_adapter(fetch_price=Decimal("85"))  # -15 % < -10 % SL

        with structlog.testing.capture_logs() as logs:
            await reconcile_open_shadow_trades(
                db=db,
                adapter=adapter,
                config=LiveConfig(settings),
                ks=KillSwitch(db),
                settings=settings,
            )

        cur = await db._conn.execute(
            "SELECT status, realized_pnl_usd FROM shadow_trades WHERE id=?",
            (sid,),
        )
        row = await cur.fetchone()
        assert row[0] == "closed_via_reconciliation"
        assert Decimal(row[1]) < Decimal("0")

        closed_events = [
            le for le in logs if le.get("event") == "live_boot_reconciliation_closed"
        ]
        assert len(closed_events) == 1
        assert closed_events[0]["crossed_reason"] == "sl_crossed"
    finally:
        await db.close()


async def test_not_crossed_row_remains_open(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        sid = await _seed_open_shadow(db, paper_trade_id=pt_id)

        settings = _settings()
        adapter = _make_adapter(fetch_price=Decimal("105"))  # +5 %, inside bounds

        with structlog.testing.capture_logs() as logs:
            await reconcile_open_shadow_trades(
                db=db,
                adapter=adapter,
                config=LiveConfig(settings),
                ks=KillSwitch(db),
                settings=settings,
            )

        cur = await db._conn.execute(
            "SELECT status FROM shadow_trades WHERE id=?", (sid,)
        )
        assert (await cur.fetchone())[0] == "open"

        done = [le for le in logs if le.get("event") == "live_boot_reconciliation_done"]
        assert done[0]["rows_inspected"] == 1
        assert done[0]["rows_closed"] == 0
        assert done[0]["rows_resumed"] == 1
    finally:
        await db.close()


async def test_adapter_error_does_not_crash(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        sid = await _seed_open_shadow(db, paper_trade_id=pt_id)

        settings = _settings()
        adapter = _make_adapter(fetch_price_exc=VenueTransientError("503 upstream"))

        with structlog.testing.capture_logs() as logs:
            await reconcile_open_shadow_trades(
                db=db,
                adapter=adapter,
                config=LiveConfig(settings),
                ks=KillSwitch(db),
                settings=settings,
            )

        cur = await db._conn.execute(
            "SELECT status FROM shadow_trades WHERE id=?", (sid,)
        )
        assert (await cur.fetchone())[0] == "open"

        err_events = [
            le for le in logs if le.get("event") == "live_boot_reconciliation_row_err"
        ]
        assert len(err_events) == 1
        assert err_events[0]["shadow_trade_id"] == sid

        # Terminal log must still fire.
        done = [le for le in logs if le.get("event") == "live_boot_reconciliation_done"]
        assert len(done) == 1
        assert done[0]["rows_inspected"] == 1
        assert done[0]["rows_closed"] == 0
        assert done[0]["rows_resumed"] == 1
    finally:
        await db.close()


async def test_live_startup_status_event_fires_at_boot(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        await _seed_open_shadow(db, paper_trade_id=pt_id)

        settings = _settings()
        adapter = _make_adapter(fetch_price=Decimal("30000"))  # BTC probe ok

        with structlog.testing.capture_logs() as logs:
            await emit_live_startup_status(
                db=db,
                adapter=adapter,
                config=LiveConfig(settings),
                ks=KillSwitch(db),
            )

        events = [le for le in logs if le.get("event") == "live_startup_status"]
        assert len(events) == 1
        ev = events[0]
        assert ev["live_mode"] == "shadow"
        assert ev["active_kill_event_id"] is None
        assert ev["shadow_trades_open"] == 1
        assert ev["binance_reachable"] is True
    finally:
        await db.close()


async def test_live_startup_status_binance_unreachable(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        settings = _settings()
        adapter = _make_adapter(
            fetch_price_exc=VenueTransientError("connection refused")
        )

        with structlog.testing.capture_logs() as logs:
            await emit_live_startup_status(
                db=db,
                adapter=adapter,
                config=LiveConfig(settings),
                ks=KillSwitch(db),
            )

        events = [le for le in logs if le.get("event") == "live_startup_status"]
        assert len(events) == 1
        ev = events[0]
        assert ev["binance_reachable"] is False
        assert ev["shadow_trades_open"] == 0
    finally:
        await db.close()
