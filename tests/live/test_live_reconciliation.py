"""LIVE-02 live boot/periodic reconciler tests — matches open ``live_trades``
to the venue by ``client_order_id`` and classifies orphans (filled-venue /
open-local, partial, missing) with §12b alerts. Always emits the terminal
``live_boot_live_reconciliation_done`` log (absence of log != success).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import structlog

from scout.config import Settings
from scout.db import Database
from scout.live.adapter_base import OrderConfirmation
from scout.live.config import LiveConfig
from scout.live.exceptions import VenueTransientError
from scout.live.kill_switch import KillSwitch
from scout.live.reconciliation import reconcile_open_live_trades


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


async def _seed_paper_trade(db):
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
            "first_signal",
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
    entry_fill_price=None,
    entry_fill_qty=None,
    cid="gecko-1-abcd1234",
):
    assert db._conn is not None
    created = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO live_trades "
        "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, size_usd, "
        " entry_fill_price, entry_fill_qty, status, client_order_id, created_at) "
        "VALUES (?,'c','L','binance','LUSDT','first_signal','100', ?, ?, 'open', ?, ?)",
        (paper_trade_id, entry_fill_price, entry_fill_qty, cid, created),
    )
    await db._conn.commit()
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    return (await cur.fetchone())[0]


def _adapter(*, conf=None, exc=None):
    adapter = MagicMock()
    if exc is not None:
        adapter.fetch_order_by_client_id = AsyncMock(side_effect=exc)
    else:
        adapter.fetch_order_by_client_id = AsyncMock(return_value=conf)
    return adapter


def _conf(status, *, fill_price=None, filled_qty=None):
    return OrderConfirmation(
        venue="binance",
        venue_order_id="V1",
        client_order_id="gecko-1-abcd1234",
        status=status,
        filled_qty=filled_qty,
        fill_price=fill_price,
        raw_response=None,
    )


async def _run(db, adapter, alert_hook=None):
    settings = _settings()
    await reconcile_open_live_trades(
        db=db,
        adapter=adapter,
        config=LiveConfig(settings),
        ks=KillSwitch(db),
        settings=settings,
        alert_hook=alert_hook,
    )


async def test_zero_open_rows_still_logs_done(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        with structlog.testing.capture_logs() as logs:
            await _run(db, _adapter(conf=None))
        done = [
            le for le in logs if le.get("event") == "live_boot_live_reconciliation_done"
        ]
        assert len(done) == 1
        assert done[0]["rows_inspected"] == 0
    finally:
        await db.close()


async def test_healthy_open_resumed(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt = await _seed_paper_trade(db)
        lid = await _seed_open_live(
            db, paper_trade_id=pt, entry_fill_price="100", entry_fill_qty="1"
        )
        adapter = _adapter(conf=_conf("filled", fill_price=100.0, filled_qty=1.0))
        with structlog.testing.capture_logs() as logs:
            await _run(db, adapter)
        cur = await db._conn.execute(
            "SELECT status FROM live_trades WHERE id=?", (lid,)
        )
        assert (await cur.fetchone())[0] == "open"
        done = [
            le for le in logs if le.get("event") == "live_boot_live_reconciliation_done"
        ]
        assert done[0]["rows_resumed"] == 1
    finally:
        await db.close()


async def test_filled_venue_open_local_recovers_entry_fill(tmp_path):
    """Crash after the Binance POST but before persisting the fill: venue says
    FILLED, local entry_fill_* is NULL → persist the fill, keep open, alert."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt = await _seed_paper_trade(db)
        lid = await _seed_open_live(db, paper_trade_id=pt)  # entry_fill_* NULL
        adapter = _adapter(conf=_conf("filled", fill_price=8.0, filled_qty=1.25))
        alerts = []

        async def _hook(m):
            alerts.append(m)

        with structlog.testing.capture_logs() as logs:
            await _run(db, adapter, alert_hook=_hook)
        cur = await db._conn.execute(
            "SELECT status, entry_fill_price, entry_fill_qty FROM live_trades WHERE id=?",
            (lid,),
        )
        row = await cur.fetchone()
        assert row[0] == "open"
        assert float(row[1]) == 8.0
        assert float(row[2]) == 1.25
        assert any(le.get("event") == "live_orphan_recovered_fill" for le in logs)
        assert len(alerts) == 1
    finally:
        await db.close()


async def test_partial_flags_needs_manual_review(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt = await _seed_paper_trade(db)
        lid = await _seed_open_live(db, paper_trade_id=pt)
        adapter = _adapter(conf=_conf("partial", fill_price=8.0, filled_qty=0.5))
        alerts = []

        async def _hook(m):
            alerts.append(m)

        with structlog.testing.capture_logs() as logs:
            await _run(db, adapter, alert_hook=_hook)
        cur = await db._conn.execute(
            "SELECT status FROM live_trades WHERE id=?", (lid,)
        )
        assert (await cur.fetchone())[0] == "needs_manual_review"
        assert any(le.get("event") == "live_orphan_partial" for le in logs)
        assert len(alerts) == 1
    finally:
        await db.close()


async def test_missing_order_flags_needs_manual_review(tmp_path):
    """Venue has no such order (buy never produced a position) → terminalize."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt = await _seed_paper_trade(db)
        lid = await _seed_open_live(db, paper_trade_id=pt)
        adapter = _adapter(conf=None)  # fetch_order_by_client_id -> None
        alerts = []

        async def _hook(m):
            alerts.append(m)

        with structlog.testing.capture_logs() as logs:
            await _run(db, adapter, alert_hook=_hook)
        cur = await db._conn.execute(
            "SELECT status FROM live_trades WHERE id=?", (lid,)
        )
        assert (await cur.fetchone())[0] == "needs_manual_review"
        assert any(le.get("event") == "live_orphan_no_fill" for le in logs)
        assert len(alerts) == 1
    finally:
        await db.close()


async def test_adapter_error_leaves_row_open_and_logs_done(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt = await _seed_paper_trade(db)
        lid = await _seed_open_live(
            db, paper_trade_id=pt, entry_fill_price="100", entry_fill_qty="1"
        )
        adapter = _adapter(exc=VenueTransientError("503"))
        with structlog.testing.capture_logs() as logs:
            await _run(db, adapter)
        cur = await db._conn.execute(
            "SELECT status FROM live_trades WHERE id=?", (lid,)
        )
        assert (await cur.fetchone())[0] == "open"
        assert any(
            le.get("event") == "live_boot_live_reconciliation_row_err" for le in logs
        )
        assert any(
            le.get("event") == "live_boot_live_reconciliation_done" for le in logs
        )
    finally:
        await db.close()
