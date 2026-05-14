"""Tests for :class:`scout.live.engine.LiveEngine` — the chokepoint dispatcher.

One test per handoff-matrix branch (spec §5 + §2.2):

1. Ineligible signal         → NO DB row, "live_handoff_skipped"
2. Kill active               → NO DB row, "live_handoff_skipped_killed"
3. Resolver None             → rejected/no_venue + metric
4. override_disabled         → rejected/override_disabled + metric
5. Thin depth                → rejected/insufficient_depth + metric
6. Steep slippage            → rejected/slippage_exceeds_cap + metric
7. Exposure cap              → rejected/exposure_cap + metric
8. Happy path                → open row + walked vwap + metric

``paper_trades`` is an FK with ON DELETE RESTRICT, so every test seeds a real
row before invoking the engine.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

from scout.config import Settings
from scout.db import Database
from scout.live.adapter_base import OrderConfirmation
from scout.live.config import LiveConfig
from scout.live.engine import LiveEngine
from scout.live.kill_switch import KillSwitch
from scout.live.types import Depth, DepthLevel

# ---------- fixture helpers --------------------------------------------------


def _depth(asks=None, bids=None, mid=Decimal("100")):
    """Depth snapshot with sensible defaults that clear gate 5 easily."""
    now = datetime.now(timezone.utc)
    if bids is None:
        bids = tuple(
            DepthLevel(
                price=Decimal("100") - Decimal(i) * Decimal("0.1"),
                qty=Decimal("1000"),
            )
            for i in range(10)
        )
    if asks is None:
        asks = tuple(
            DepthLevel(
                price=Decimal("100") + Decimal(i) * Decimal("0.1"),
                qty=Decimal("1000"),
            )
            for i in range(10)
        )
    return Depth(pair="TUSDT", bids=bids, asks=asks, mid=mid, fetched_at=now)


def _settings(**overrides):
    base = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        LIVE_MODE="shadow",
        LIVE_SIGNAL_ALLOWLIST="first_signal",
        LIVE_TRADE_AMOUNT_USD=Decimal("100"),
        LIVE_SLIPPAGE_BPS_CAP=50,
        LIVE_DEPTH_HEALTH_MULTIPLIER=Decimal("3"),
        LIVE_MAX_EXPOSURE_USD=Decimal("500"),
        LIVE_MAX_OPEN_POSITIONS=5,
    )
    base.update(overrides)
    return Settings(**base)


def _make_engine(
    db, *, settings=None, depth=None, venue=None, fetch_depth_exc=None, routing=None
):
    s = settings or _settings()
    config = LiveConfig(s)

    from scout.live.types import ResolvedVenue

    resolver = AsyncMock()
    resolver.resolve = AsyncMock(
        return_value=(
            venue
            if venue is not None
            else ResolvedVenue(
                symbol="SYM", venue="binance", pair="TUSDT", source="cache"
            )
        )
    )
    adapter = AsyncMock()
    if fetch_depth_exc is not None:
        adapter.fetch_depth = AsyncMock(side_effect=fetch_depth_exc)
    else:
        adapter.fetch_depth = AsyncMock(
            return_value=depth if depth is not None else _depth()
        )
    ks = KillSwitch(db)
    return LiveEngine(
        config=config,
        resolver=resolver,
        adapter=adapter,
        db=db,
        kill_switch=ks,
        routing=routing,
    )


async def _seed_paper_trade(
    db, *, coin_id="c", symbol="SYM", signal_type="first_signal"
):
    assert db._conn is not None
    await db._conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
        "signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct, "
        "tp_price, sl_price, status, opened_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            coin_id,
            symbol,
            "Name",
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
    pt_id = (await cur.fetchone())[0]
    return types.SimpleNamespace(
        id=pt_id, coin_id=coin_id, symbol=symbol, signal_type=signal_type
    )


async def _count_shadow(db, *, status=None, reject_reason=None):
    assert db._conn is not None
    sql = "SELECT COUNT(*) FROM shadow_trades WHERE 1=1"
    args: list = []
    if status is not None:
        sql += " AND status = ?"
        args.append(status)
    if reject_reason is not None:
        sql += " AND reject_reason = ?"
        args.append(reject_reason)
    cur = await db._conn.execute(sql, args)
    return (await cur.fetchone())[0]


async def _metric_value(db, metric):
    assert db._conn is not None
    cur = await db._conn.execute(
        "SELECT value FROM live_metrics_daily WHERE metric = ?", (metric,)
    )
    row = await cur.fetchone()
    return row[0] if row is not None else 0


# ---------- tests ------------------------------------------------------------


async def test_ineligible_signal_no_db_row(tmp_path):
    """Signal not in allowlist → is_eligible False, no row, no metric."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    engine = _make_engine(db)
    pt = await _seed_paper_trade(db, signal_type="volume_spike")

    assert engine.is_eligible("volume_spike") is False
    await engine.on_paper_trade_opened(pt)

    cur = await db._conn.execute("SELECT COUNT(*) FROM shadow_trades")
    assert (await cur.fetchone())[0] == 0
    await db.close()


async def test_kill_active_no_db_row(tmp_path):
    """Kill switch active → no DB row; engine short-circuits before insert."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    await ks.trigger(
        triggered_by="manual",
        reason="test",
        duration=timedelta(hours=1),
    )
    engine = _make_engine(db)
    # Attach the already-triggered KillSwitch so engine's Gates see it.
    engine._ks = ks
    engine._gates._ks = ks
    pt = await _seed_paper_trade(db)

    await engine.on_paper_trade_opened(pt)

    cur = await db._conn.execute("SELECT COUNT(*) FROM shadow_trades")
    assert (await cur.fetchone())[0] == 0
    await db.close()


async def test_no_venue_writes_rejected_row(tmp_path):
    """Resolver returns None + no venue_overrides row → rejected/no_venue."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    engine = _make_engine(db)
    engine._gates._resolver.resolve = AsyncMock(return_value=None)
    pt = await _seed_paper_trade(db, symbol="UNKNOWN")

    await engine.on_paper_trade_opened(pt)

    assert await _count_shadow(db, status="rejected", reject_reason="no_venue") == 1
    assert await _metric_value(db, "shadow_rejects_no_venue") == 1
    await db.close()


async def test_override_disabled_writes_rejected_row(tmp_path):
    """venue_overrides.disabled=1 + resolver None → rejected/override_disabled."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "INSERT INTO venue_overrides "
        "(symbol, venue, pair, disabled, note, created_at, updated_at) "
        "VALUES ('SYM','binance','TUSDT',1,'banned',"
        " '2026-04-23T00Z','2026-04-23T00Z')"
    )
    await db._conn.commit()
    engine = _make_engine(db)
    engine._gates._resolver.resolve = AsyncMock(return_value=None)
    pt = await _seed_paper_trade(db, symbol="SYM")

    await engine.on_paper_trade_opened(pt)

    assert (
        await _count_shadow(db, status="rejected", reject_reason="override_disabled")
        == 1
    )
    assert await _metric_value(db, "shadow_rejects_override_disabled") == 1
    await db.close()


async def test_insufficient_depth_writes_rejected_row(tmp_path):
    """Thin book → gate 5 rejects insufficient_depth."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    thin_asks = tuple(
        DepthLevel(price=Decimal("100"), qty=Decimal("0.01")) for _ in range(10)
    )
    thin_bids = tuple(
        DepthLevel(price=Decimal("99"), qty=Decimal("0.01")) for _ in range(10)
    )
    depth = _depth(asks=thin_asks, bids=thin_bids)
    engine = _make_engine(db, depth=depth)
    pt = await _seed_paper_trade(db)

    await engine.on_paper_trade_opened(pt)

    assert (
        await _count_shadow(db, status="rejected", reject_reason="insufficient_depth")
        == 1
    )
    assert await _metric_value(db, "shadow_rejects_insufficient_depth") == 1
    await db.close()


async def test_slippage_exceeds_cap_writes_rejected_row(tmp_path):
    """Steep ask curve → gate 6 rejects slippage_exceeds_cap."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    steep_asks = (
        DepthLevel(price=Decimal("100"), qty=Decimal("0.01")),  # $1
        DepthLevel(price=Decimal("200"), qty=Decimal("1000")),  # $200_000
    ) + tuple(
        DepthLevel(price=Decimal("200") + Decimal(i), qty=Decimal("1000"))
        for i in range(8)
    )
    healthy_bids = tuple(
        DepthLevel(
            price=Decimal("100") - Decimal(i) * Decimal("0.01"),
            qty=Decimal("1000"),
        )
        for i in range(10)
    )
    depth = _depth(asks=steep_asks, bids=healthy_bids)
    engine = _make_engine(db, depth=depth)
    pt = await _seed_paper_trade(db)

    await engine.on_paper_trade_opened(pt)

    assert (
        await _count_shadow(db, status="rejected", reject_reason="slippage_exceeds_cap")
        == 1
    )
    assert await _metric_value(db, "shadow_rejects_slippage_exceeds_cap") == 1
    await db.close()


async def test_exposure_cap_writes_rejected_row(tmp_path):
    """5 open shadow_trades with max_positions=5 → rejected/exposure_cap."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Seed a paper_trades row the pre-existing shadow rows can reference, then
    # five open shadow_trades tied to it.
    pre = await _seed_paper_trade(db, coin_id="pre", symbol="PRE")
    now = datetime.now(timezone.utc).isoformat()
    for i in range(5):
        await db._conn.execute(
            "INSERT INTO shadow_trades "
            "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, "
            " size_usd, status, created_at) "
            "VALUES (?, ?, ?, 'binance', 'XUSDT', 'first_signal', "
            " '1', 'open', ?)",
            (pre.id, f"coin-{i}", f"OPEN{i}", now),
        )
    await db._conn.commit()

    settings = _settings(
        LIVE_MAX_EXPOSURE_USD=Decimal("100000"),
        LIVE_MAX_OPEN_POSITIONS=5,
    )
    engine = _make_engine(db, settings=settings)
    pt = await _seed_paper_trade(db, coin_id="new", symbol="NEW")

    await engine.on_paper_trade_opened(pt)

    assert await _count_shadow(db, status="rejected", reject_reason="exposure_cap") == 1
    assert await _metric_value(db, "shadow_rejects_exposure_cap") == 1
    await db.close()


async def test_happy_path_writes_open_row(tmp_path):
    """All gates pass → open row with entry_walked_vwap + mid + slippage."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    engine = _make_engine(db)
    pt = await _seed_paper_trade(db)

    await engine.on_paper_trade_opened(pt)

    cur = await db._conn.execute(
        "SELECT status, entry_walked_vwap, mid_at_entry, entry_slippage_bps, "
        " venue, pair, size_usd "
        "FROM shadow_trades WHERE paper_trade_id = ?",
        (pt.id,),
    )
    row = await cur.fetchone()
    assert row is not None
    status, vwap, mid, slip, venue, pair, size = row
    assert status == "open"
    assert vwap is not None
    assert Decimal(vwap) > 0
    assert Decimal(mid) == Decimal("100")
    assert slip is not None and slip >= 0
    assert venue == "binance"
    assert pair == "TUSDT"
    assert Decimal(size) == Decimal("100")

    assert await _metric_value(db, "shadow_orders_opened") == 1
    await db.close()


async def test_live_dispatch_preserves_signal_type_in_order_request(tmp_path):
    """Live routing path must preserve signal_type for live_trades attribution."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        LIVE_MODE="live",
        LIVE_USE_ROUTING_LAYER=True,
        LIVE_USE_REAL_SIGNED_REQUESTS=True,
        LIVE_SIGNAL_ALLOWLIST="volume_spike",
    )
    adapter = AsyncMock()
    adapter.fetch_depth = AsyncMock(return_value=_depth())
    adapter.fetch_account_balance = AsyncMock(return_value=1000.0)
    adapter.place_order_request = AsyncMock(return_value="venue-order-1")
    adapter.await_fill_confirmation = AsyncMock(
        return_value=OrderConfirmation(
            venue="binance",
            venue_order_id="venue-order-1",
            client_order_id="cid",
            status="filled",
            filled_qty=1.0,
            fill_price=100.0,
            raw_response={},
        )
    )
    routing = AsyncMock()
    routing.get_candidates = AsyncMock(
        return_value=[
            types.SimpleNamespace(venue="binance", venue_pair="TUSDT"),
        ]
    )
    engine = _make_engine(db, settings=settings, depth=_depth(), routing=routing)
    engine._adapter = adapter
    engine._gates._adapter = adapter
    engine._routing = routing
    pt = await _seed_paper_trade(db, signal_type="volume_spike")

    await engine.on_paper_trade_opened(pt)

    request = adapter.place_order_request.await_args.args[0]
    assert request.signal_type == "volume_spike"
    await db.close()
