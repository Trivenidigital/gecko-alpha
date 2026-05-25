"""Round 22: targeted coverage gaps in scout/live/shadow_evaluator.py.

The existing tests/live/test_shadow_evaluator.py covers TP/SL/duration
crosses, transient fetch_price error, MAX_REVIEW_RETRIES flip, daily-cap
trigger, and NULL entry_vwap. This file fills the gaps that have
production-realistic failure modes but no isolated test today:

1. fetch_depth fails after fetch_price succeeds → second try/except
   branch (lines 181-189) bumps review_retries without closing.
2. walk_bids returns insufficient_liquidity → mid-price fallback
   (lines 194-199) still closes the row rather than leaving it open.
3. needs_manual_review row with next_review_at <= now is re-picked-up
   by the SELECT — closes the unbounded-growth gap (after MAX_REVIEW
   retries, rows must still be able to close once venue recovers).
4. maybe_trigger_from_daily_loss raising is swallowed — the close is
   already durable; loop must not lose the close due to the side check
   blowing up.
5. Multi-row iteration — closed_count reflects every closure in one pass.
6. Mid-range price (no TP/SL/duration cross) → row stays open,
   closed_count=0.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import structlog

from scout.config import Settings
from scout.db import Database
from scout.live.config import LiveConfig
from scout.live.exceptions import VenueTransientError
from scout.live.kill_switch import KillSwitch
from scout.live.shadow_evaluator import evaluate_open_shadow_trades
from scout.live.types import Depth, DepthLevel

# ---------- helpers (copy-paste from test_shadow_evaluator.py) ---------------


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
        DepthLevel(price=mid - Decimal(i) * Decimal("0.1"), qty=qty) for i in range(10)
    )
    asks = tuple(
        DepthLevel(price=mid + Decimal(i) * Decimal("0.1"), qty=qty) for i in range(10)
    )
    return Depth(
        pair="TUSDT",
        bids=bids,
        asks=asks,
        mid=mid,
        fetched_at=datetime.now(timezone.utc),
    )


def _thin_depth(mid=Decimal("70")):
    """Depth with bids/asks summing to far less than the test's size_usd=100
    so walk_bids/walk_asks returns insufficient_liquidity."""
    bids = (DepthLevel(price=mid - Decimal("0.5"), qty=Decimal("0.01")),)
    asks = (DepthLevel(price=mid + Decimal("0.5"), qty=Decimal("0.01")),)
    return Depth(
        pair="TUSDT",
        bids=bids,
        asks=asks,
        mid=mid,
        fetched_at=datetime.now(timezone.utc),
    )


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
    review_retries: int = 0,
    status: str = "open",
    next_review_at: str | None = None,
    pair: str = "TUSDT",
):
    assert db._conn is not None
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO shadow_trades "
        "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, size_usd, "
        " entry_walked_vwap, mid_at_entry, status, review_retries, "
        " next_review_at, created_at) "
        "VALUES (?,'c','T','binance',?,?,?, ?, '100', ?, ?, ?, ?)",
        (
            paper_trade_id,
            pair,
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


def _make_adapter(*, fetch_price=None, fetch_depth=None, fetch_depth_exc=None):
    adapter = AsyncMock()
    adapter.fetch_price = AsyncMock(
        return_value=fetch_price if fetch_price is not None else Decimal("100")
    )
    if fetch_depth_exc is not None:
        adapter.fetch_depth = AsyncMock(side_effect=fetch_depth_exc)
    else:
        adapter.fetch_depth = AsyncMock(
            return_value=fetch_depth if fetch_depth is not None else _depth()
        )
    return adapter


# ---------- 1. fetch_depth failure path -------------------------------------


async def test_fetch_depth_failure_bumps_review_retries_without_closing(tmp_path):
    """fetch_price returns a TP-cross price but fetch_depth raises →
    second try/except branch (shadow_evaluator.py:181-189) bumps
    review_retries and leaves the row open. The close branch is reached
    via new_status='closed_tp', so this exercises the depth-fetch path
    that the fetch_price test misses."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        sid = await _seed_open_shadow(db, paper_trade_id=pt_id)
        settings = _settings()
        # fetch_price clears TP (+25%), fetch_depth raises.
        adapter = _make_adapter(
            fetch_price=Decimal("125"),
            fetch_depth_exc=VenueTransientError("depth 503"),
        )

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
        assert row[0] == "open", "must NOT close when depth fetch failed"
        assert row[1] == 1

        events = [le.get("event") for le in logs]
        assert "live_shadow_exit_fetch_failed" in events
    finally:
        await db.close()


# ---------- 2. insufficient_liquidity → mid fallback -------------------------


async def test_insufficient_liquidity_walk_falls_back_to_mid(tmp_path):
    """Bid side is too thin to fill size_usd → walk_bids returns
    insufficient_liquidity. The evaluator still closes the row using
    mid as exit_vwap (lines 194-199). Realised PnL ends up approximate
    but the row does NOT pile up open forever."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        sid = await _seed_open_shadow(db, paper_trade_id=pt_id)
        settings = _settings()
        # SL cross (-30%) + thin depth.
        thin = _thin_depth(mid=Decimal("70"))
        adapter = _make_adapter(fetch_price=Decimal("70"), fetch_depth=thin)

        closed = await evaluate_open_shadow_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=KillSwitch(db),
            settings=settings,
        )

        assert closed == 1
        cur = await db._conn.execute(
            "SELECT status, exit_walked_vwap, realized_pnl_pct "
            "FROM shadow_trades WHERE id=?",
            (sid,),
        )
        row = await cur.fetchone()
        assert row[0] == "closed_sl"
        # Mid fallback used → exit_vwap == mid (70).
        assert Decimal(row[1]) == Decimal("70")
        # PnL pct based on mid → exactly -30%.
        assert Decimal(row[2]) == Decimal("-30")
    finally:
        await db.close()


# ---------- 3. needs_manual_review re-evaluation ----------------------------


async def test_needs_manual_review_row_picked_up_when_next_review_due(tmp_path):
    """After MAX_REVIEW_RETRIES the row sits at status='needs_manual_review'
    with next_review_at scheduled 24h out. Once that timestamp is in the
    past AND the venue recovers, the row must be re-picked-up by the
    SELECT (`OR status='needs_manual_review' AND next_review_at <= now`)
    and closed normally. Without this, exhausted rows would pile up
    forever — operator-visible but unrecoverable."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        # Row that already maxed out review_retries, with next_review_at
        # in the past (venue recovered, time to retry).
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        sid = await _seed_open_shadow(
            db,
            paper_trade_id=pt_id,
            status="needs_manual_review",
            review_retries=3,
            next_review_at=past,
        )
        settings = _settings()
        # TP cross + healthy depth — venue is back.
        adapter = _make_adapter(
            fetch_price=Decimal("125"),
            fetch_depth=_depth(mid=Decimal("125")),
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
            "SELECT status FROM shadow_trades WHERE id=?", (sid,)
        )
        assert (await cur.fetchone())[0] == "closed_tp"
    finally:
        await db.close()


async def test_needs_manual_review_row_skipped_when_next_review_future(tmp_path):
    """Inverse case: next_review_at is still in the future → row stays
    in needs_manual_review and is NOT re-evaluated. Otherwise we'd burn
    venue calls on rows the operator is waiting to inspect."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        sid = await _seed_open_shadow(
            db,
            paper_trade_id=pt_id,
            status="needs_manual_review",
            review_retries=3,
            next_review_at=future,
        )
        settings = _settings()
        adapter = _make_adapter(fetch_price=Decimal("125"))

        closed = await evaluate_open_shadow_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=KillSwitch(db),
            settings=settings,
        )

        assert closed == 0
        # fetch_price MUST NOT have been called — row was filtered at SELECT.
        adapter.fetch_price.assert_not_awaited()
        cur = await db._conn.execute(
            "SELECT status FROM shadow_trades WHERE id=?", (sid,)
        )
        assert (await cur.fetchone())[0] == "needs_manual_review"
    finally:
        await db.close()


# ---------- 4. daily-cap exception swallowed, close still durable ----------


async def test_daily_cap_exception_does_not_undo_close(tmp_path):
    """maybe_trigger_from_daily_loss raises after the row is closed.
    Spec §6.2: the close lives in its own transaction; the side check
    sits OUTSIDE that transaction. An exception there must not undo
    the close. The evaluator catches + logs and moves on (lines 224-231)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        sid = await _seed_open_shadow(db, paper_trade_id=pt_id)
        settings = _settings()
        adapter = _make_adapter(
            fetch_price=Decimal("70"), fetch_depth=_depth(mid=Decimal("70"))
        )

        with patch(
            "scout.live.shadow_evaluator.maybe_trigger_from_daily_loss",
            side_effect=RuntimeError("daily-cap probe blew up"),
        ):
            with structlog.testing.capture_logs() as logs:
                closed = await evaluate_open_shadow_trades(
                    db=db,
                    adapter=adapter,
                    config=LiveConfig(settings),
                    ks=KillSwitch(db),
                    settings=settings,
                )

        # The close itself MUST have landed — that's the spec invariant.
        assert closed == 1
        cur = await db._conn.execute(
            "SELECT status, closed_at FROM shadow_trades WHERE id=?", (sid,)
        )
        row = await cur.fetchone()
        assert row[0] == "closed_sl"
        assert row[1] is not None

        # The side-check error must be logged so the operator can investigate.
        events = [le.get("event") for le in logs]
        assert "live_shadow_eval_daily_cap_err" in events
    finally:
        await db.close()


# ---------- 5. multi-row iteration ------------------------------------------


async def test_multiple_open_rows_close_in_one_pass(tmp_path):
    """Two open rows in one evaluation pass — both eligible for TP →
    closed_count=2 and both rows reach status='closed_tp'.

    Guards against subtle iteration bugs (e.g. an early `return` that
    only closes the first row, or a per-row commit failure that aborts
    the loop)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id1 = await _seed_paper_trade(db, coin_id="c1", symbol="A")
        pt_id2 = await _seed_paper_trade(db, coin_id="c2", symbol="B")
        sid1 = await _seed_open_shadow(db, paper_trade_id=pt_id1, pair="AUSDT")
        sid2 = await _seed_open_shadow(db, paper_trade_id=pt_id2, pair="BUSDT")
        settings = _settings()
        adapter = _make_adapter(
            fetch_price=Decimal("125"),
            fetch_depth=_depth(mid=Decimal("125")),
        )

        closed = await evaluate_open_shadow_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=KillSwitch(db),
            settings=settings,
        )

        assert closed == 2
        for sid in (sid1, sid2):
            cur = await db._conn.execute(
                "SELECT status FROM shadow_trades WHERE id=?", (sid,)
            )
            assert (await cur.fetchone())[0] == "closed_tp", f"sid={sid} did not close"
    finally:
        await db.close()


# ---------- 6. mid-range price → no close -----------------------------------


async def test_mid_range_price_leaves_row_open(tmp_path):
    """Price moved +5% — below TP (20%), above SL (-10%), inside duration
    (created just now). No close branch fires → row stays open,
    closed_count=0, review_retries unchanged."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        pt_id = await _seed_paper_trade(db)
        sid = await _seed_open_shadow(db, paper_trade_id=pt_id)
        settings = _settings()
        adapter = _make_adapter(
            fetch_price=Decimal("105"), fetch_depth=_depth(mid=Decimal("105"))
        )

        closed = await evaluate_open_shadow_trades(
            db=db,
            adapter=adapter,
            config=LiveConfig(settings),
            ks=KillSwitch(db),
            settings=settings,
        )

        assert closed == 0
        cur = await db._conn.execute(
            "SELECT status, review_retries, closed_at FROM shadow_trades WHERE id=?",
            (sid,),
        )
        row = await cur.fetchone()
        assert row[0] == "open"
        assert row[1] == 0
        assert row[2] is None
        # fetch_depth should NOT have been called — no close branch fired.
        adapter.fetch_depth.assert_not_awaited()
    finally:
        await db.close()
