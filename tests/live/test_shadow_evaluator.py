"""Shadow evaluator tests (spec §6.2 + §10.5).

Covers:
1. TP cross closes row with closed_tp + positive PnL.
2. SL cross closes row with closed_sl + negative PnL.
3. Duration expiry closes row with closed_duration.
4. Transient fetch_price error bumps review_retries and leaves row open.
5. Third consecutive failure flips to needs_manual_review + WARN.
6. Close that breaches LIVE_DAILY_LOSS_CAP_USD arms the kill switch.
7. NULL entry_walked_vwap is skipped gracefully.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import structlog

from scout.config import Settings
from scout.db import Database
from scout.live.config import LiveConfig
from scout.live.exceptions import VenueTransientError
from scout.live.kill_switch import KillSwitch
from scout.live.shadow_evaluator import (
    MAX_REVIEW_RETRIES,
    evaluate_open_shadow_trades,
)
from scout.live.types import Depth, DepthLevel


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


def _depth(mid=Decimal("100"), qty=Decimal("1000")):
    bids = tuple(
        DepthLevel(price=mid - Decimal(i) * Decimal("0.1"), qty=qty)
        for i in range(10)
    )
    asks = tuple(
        DepthLevel(price=mid + Decimal(i) * Decimal("0.1"), qty=qty)
        for i in range(10)
    )
    return Depth(
        pair="TUSDT",
        bids=bids,
        asks=asks,
        mid=mid,
        fetched_at=datetime.now(timezone.utc),
    )


async def _seed_paper_trade(
    db, *, coin_id="c", symbol="T", signal_type="first_signal"
):
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
    review_retries: int = 0,
    status: str = "open",
    next_review_at: str | None = None,
):
    assert db._conn is not None
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO shadow_trades "
        "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, size_usd, "
        " entry_walked_vwap, mid_at_entry, status, review_retries, "
        " next_review_at, created_at) "
        "VALUES (?,'c','T','binance','TUSDT',?,?, ?, '100', ?, ?, ?, ?)",
        (
            paper_trade_id,
            signal_type,
            size_usd,
            entry_vwap,
            status,
            review_retries,
            next_review_at,
            created_at,
        ),
    )
    await db._conn.commit()
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    return (await cur.fetchone())[0]


async def _seed_closed_loss(db, *, paper_trade_id, pnl_usd: float):
    """Seed a closed_sl row with realized_pnl_usd set, closed today."""
    assert db._conn is not None
    today_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO shadow_trades "
        "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, size_usd, "
        " entry_walked_vwap, mid_at_entry, status, realized_pnl_usd, "
        " created_at, closed_at) "
        "VALUES (?,'c','T','binance','TUSDT','first_signal','100',"
        " '100','100','closed_sl', ?, ?, ?)",
        (paper_trade_id, str(pnl_usd), today_iso, today_iso),
    )
    await db._conn.commit()


def _make_adapter(*, fetch_price=None, fetch_depth=None, fetch_price_exc=None):
    adapter = AsyncMock()
    if fetch_price_exc is not None:
        adapter.fetch_price = AsyncMock(side_effect=fetch_price_exc)
    else:
        adapter.fetch_price = AsyncMock(
            return_value=fetch_price if fetch_price is not None else Decimal("100")
        )
    adapter.fetch_depth = AsyncMock(
        return_value=fetch_depth if fetch_depth is not None else _depth()
    )
    return adapter


# ---------- tests ------------------------------------------------------------


async def test_tp_exit_closes_row(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        sid = await _seed_open_shadow(db, paper_trade_id=pt_id)
        settings = _settings()
        adapter = _make_adapter(
            fetch_price=Decimal("125"), fetch_depth=_depth(mid=Decimal("125"))
        )

        closed = await evaluate_open_shadow_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=KillSwitch(db),
            settings=settings,
        )

        assert closed == 1
        cur = await db._conn.execute(
            "SELECT status, exit_walked_vwap, realized_pnl_usd, "
            "realized_pnl_pct FROM shadow_trades WHERE id=?",
            (sid,),
        )
        row = await cur.fetchone()
        assert row[0] == "closed_tp"
        assert row[1] is not None
        assert Decimal(row[2]) > Decimal("0")
        # Near +25 %, allow rounding slack from walked vwap.
        assert Decimal(row[3]) > Decimal("20")
    finally:
        await db.close()


async def test_sl_exit_closes_row(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        sid = await _seed_open_shadow(db, paper_trade_id=pt_id)
        settings = _settings()
        adapter = _make_adapter(
            fetch_price=Decimal("70"), fetch_depth=_depth(mid=Decimal("70"))
        )

        closed = await evaluate_open_shadow_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=KillSwitch(db),
            settings=settings,
        )

        assert closed == 1
        cur = await db._conn.execute(
            "SELECT status, realized_pnl_usd, realized_pnl_pct "
            "FROM shadow_trades WHERE id=?",
            (sid,),
        )
        row = await cur.fetchone()
        assert row[0] == "closed_sl"
        assert Decimal(row[1]) < Decimal("0")
        assert Decimal(row[2]) < Decimal("-10")
    finally:
        await db.close()


async def test_duration_exit_closes_row(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        sid = await _seed_open_shadow(
            db, paper_trade_id=pt_id, created_at=old
        )
        settings = _settings()
        # Mid-range price — neither TP nor SL crosses, so duration wins.
        adapter = _make_adapter(fetch_price=Decimal("110"))

        closed = await evaluate_open_shadow_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=KillSwitch(db),
            settings=settings,
        )

        assert closed == 1
        cur = await db._conn.execute(
            "SELECT status FROM shadow_trades WHERE id=?", (sid,)
        )
        assert (await cur.fetchone())[0] == "closed_duration"
    finally:
        await db.close()


async def test_transient_error_bumps_review_retries(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        sid = await _seed_open_shadow(db, paper_trade_id=pt_id)
        settings = _settings()
        adapter = _make_adapter(
            fetch_price_exc=VenueTransientError("503 upstream")
        )

        before = datetime.now(timezone.utc)
        closed = await evaluate_open_shadow_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=KillSwitch(db),
            settings=settings,
        )

        assert closed == 0
        cur = await db._conn.execute(
            "SELECT status, review_retries, next_review_at "
            "FROM shadow_trades WHERE id=?",
            (sid,),
        )
        row = await cur.fetchone()
        assert row[0] == "open"
        assert row[1] == 1
        next_at = datetime.fromisoformat(row[2])
        delta = next_at - before
        # ~24h +/- a few seconds of test overhead.
        assert timedelta(hours=23, minutes=59) < delta < timedelta(
            hours=24, minutes=1
        )
    finally:
        await db.close()


async def test_third_failure_flips_to_needs_manual_review(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        sid = await _seed_open_shadow(
            db, paper_trade_id=pt_id, review_retries=MAX_REVIEW_RETRIES - 1
        )
        settings = _settings()
        adapter = _make_adapter(
            fetch_price_exc=VenueTransientError("timeout")
        )

        with structlog.testing.capture_logs() as logs:
            await evaluate_open_shadow_trades(
                db=db,
                adapter=adapter,
                config=LiveConfig(settings),
                ks=KillSwitch(db),
                settings=settings,
            )

        cur = await db._conn.execute(
            "SELECT status, review_retries FROM shadow_trades WHERE id=?",
            (sid,),
        )
        row = await cur.fetchone()
        assert row[0] == "needs_manual_review"
        assert row[1] == MAX_REVIEW_RETRIES

        events = [le.get("event") for le in logs]
        assert "live_shadow_review_exhausted" in events
    finally:
        await db.close()


async def test_closing_triggers_daily_cap_check(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        # Already -$40 realised today.
        await _seed_closed_loss(db, paper_trade_id=pt_id, pnl_usd=-40.0)
        # Open row priced for a ~30 % loss → $30 loss → total -$70, cap=$50.
        sid = await _seed_open_shadow(db, paper_trade_id=pt_id)
        settings = _settings()
        adapter = _make_adapter(
            fetch_price=Decimal("70"), fetch_depth=_depth(mid=Decimal("70"))
        )
        ks = KillSwitch(db)

        closed = await evaluate_open_shadow_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=ks,
            settings=settings,
        )

        assert closed == 1
        cur = await db._conn.execute(
            "SELECT status FROM shadow_trades WHERE id=?", (sid,)
        )
        assert (await cur.fetchone())[0] == "closed_sl"
        state = await ks.is_active()
        assert state is not None
        assert state.triggered_by == "daily_loss_cap"
    finally:
        await db.close()


async def test_no_entry_vwap_skipped_gracefully(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        sid = await _seed_open_shadow(
            db, paper_trade_id=pt_id, entry_vwap=None
        )
        settings = _settings()
        adapter = _make_adapter(fetch_price=Decimal("125"))

        with structlog.testing.capture_logs() as logs:
            closed = await evaluate_open_shadow_trades(
                db=db,
                adapter=adapter,
                config=LiveConfig(settings),
                ks=KillSwitch(db),
                settings=settings,
            )

        assert closed == 0
        cur = await db._conn.execute(
            "SELECT status, review_retries FROM shadow_trades WHERE id=?",
            (sid,),
        )
        row = await cur.fetchone()
        assert row[0] == "open"
        assert row[1] == 0
        # fetch_price should NOT have been called — we skipped before that.
        adapter.fetch_price.assert_not_awaited()

        events = [le.get("event") for le in logs]
        assert "live_shadow_entry_vwap_null_skipped" in events
    finally:
        await db.close()
