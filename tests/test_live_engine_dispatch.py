"""BL-NEW-LIVE-HYBRID M1.5b: LiveEngine._dispatch_live + __init__ tests.

Fixture pattern (R1-I4 fold — first engine-level test file): stub
RoutingLayer + stub ExchangeAdapter constructed via Protocol-like
SimpleNamespace shims. M1.5c reconciler tests should reuse the
`_make_engine` helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.adapter_base import OrderConfirmation
from scout.live.binance_adapter import (
    BinanceAuthError,
    BinanceIPBanError,
)
from scout.live.config import LiveConfig
from scout.live.engine import LiveEngine
from scout.live.exceptions import VenueTransientError
from scout.live.kill_switch import KillSwitch
from scout.live.routing import RouteCandidate

_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "x",
    "ANTHROPIC_API_KEY": "x",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


@dataclass
class _PaperTrade:
    id: int = 42
    coin_id: str = "btc"
    symbol: str = "BTC"
    signal_type: str = "first_signal"
    chain: str | None = None


def _make_paper_trade(**overrides) -> _PaperTrade:
    return _PaperTrade(**overrides)


async def _insert_paper_trade(db: Database, *, trade_id: int = 42) -> None:
    """Insert a paper_trades row so live_trades FK on paper_trade_id is
    satisfied. Required for any test that triggers _dispatch_live's
    no_venue / live_trades INSERT path.
    """
    if db._conn is None:
        raise RuntimeError("db not initialized")
    await db._conn.execute(
        """INSERT INTO paper_trades
           (id, token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price,
            sl_price, status, opened_at)
           VALUES (?, 'btc', 'BTC', 'btc', 'binance', 'first_signal',
                   '{}', 100.0, 10.0, 0.1, 20.0, 10.0, 120.0, 90.0,
                   'open', ?)""",
        (trade_id, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def _make_engine(
    db: Database,
    *,
    mode: str = "live",
    routing_flag: bool = True,
    signed_flag: bool = True,
    routing: object | None = None,
    adapter: object | None = None,
):
    settings = _settings(
        LIVE_MODE=mode,
        LIVE_TRADING_ENABLED=True,
        LIVE_USE_REAL_SIGNED_REQUESTS=signed_flag,
        LIVE_USE_ROUTING_LAYER=routing_flag,
        LIVE_SIGNAL_ALLOWLIST="first_signal",
    )
    config = LiveConfig(settings)
    if adapter is None:
        adapter = MagicMock()
        adapter.place_order_request = AsyncMock(return_value="VENUE-1")
        adapter.await_fill_confirmation = AsyncMock(
            return_value=OrderConfirmation(
                venue="binance",
                venue_order_id="VENUE-1",
                client_order_id="gecko-42-aaaa",
                status="filled",
                filled_qty=1.0,
                fill_price=10.0,
                raw_response=None,
            )
        )
    resolver = MagicMock()
    # PR-stage V1+V2+V3 fix: spec=KillSwitch locks the contract — any
    # call to a non-existent attribute (e.g., the now-fixed .engage())
    # raises AttributeError instead of silently returning. Async methods
    # need explicit AsyncMock binding.
    kill_switch = MagicMock(spec=KillSwitch)
    kill_switch.trigger = AsyncMock(return_value=(1, True))
    return LiveEngine(
        config=config,
        resolver=resolver,
        adapter=adapter,
        db=db,
        kill_switch=kill_switch,
        routing=routing,
    )


def _candidate(venue="binance", pair="BTCUSDT", score=0.9) -> RouteCandidate:
    return RouteCandidate(
        venue=venue,
        venue_pair=pair,
        expected_fill_price=10.0,
        expected_slippage_bps=5.0,
        available_capital_usd=1000.0,
        venue_health_score=score,
    )


# ---------- engine __init__ misconfig CRASH tests (R2-C1, R2-I3, R1-M2) ----------


@pytest.mark.asyncio
async def test_engine_init_crashes_on_routing_without_signed(tmp_path):
    """R2-C1 fold: LIVE_USE_ROUTING_LAYER=True AND LIVE_USE_REAL_SIGNED_REQUESTS=False
    -> RuntimeError at __init__ (silent-no-op misconfig prevention)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    with pytest.raises(RuntimeError, match="LIVE_USE_REAL_SIGNED_REQUESTS=False"):
        await _make_engine(
            db,
            mode="live",
            routing_flag=True,
            signed_flag=False,
            routing=MagicMock(),
        )
    await db.close()


@pytest.mark.asyncio
async def test_engine_init_crashes_on_routing_flag_without_layer(tmp_path):
    """R2-I3 + R1-M2 fold: LIVE_USE_ROUTING_LAYER=True AND routing=None
    -> RuntimeError at __init__."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    with pytest.raises(RuntimeError, match="routing=None"):
        await _make_engine(
            db,
            mode="live",
            routing_flag=True,
            signed_flag=True,
            routing=None,
        )
    await db.close()


@pytest.mark.asyncio
async def test_engine_init_no_crash_under_shadow_mode(tmp_path):
    """Shadow mode is exempt from misconfig CRASH (no live trades dispatched)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    engine = await _make_engine(
        db,
        mode="shadow",
        routing_flag=True,
        signed_flag=False,
        routing=None,
    )
    assert engine is not None
    await db.close()


# ---------- _dispatch_live behavior tests ----------


@pytest.mark.asyncio
async def test_dispatch_live_increments_counter_on_filled(tmp_path):
    """Top candidate + adapter returns FILLED -> counter incremented."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    routing = MagicMock()
    routing.get_candidates = AsyncMock(return_value=[_candidate()])
    engine = await _make_engine(db, routing=routing)
    pt = _make_paper_trade()
    await engine._dispatch_live(paper_trade=pt, size_usd=10.0)
    cur = await db._conn.execute(
        "SELECT consecutive_no_correction FROM signal_venue_correction_count "
        "WHERE signal_type=? AND venue=?",
        ("first_signal", "binance"),
    )
    row = await cur.fetchone()
    assert row[0] == 1
    await db.close()


@pytest.mark.asyncio
async def test_dispatch_live_uses_full_cid_for_await_fill(tmp_path):
    """R1-C2 regression: await_fill receives 'gecko-{paper_trade_id}-{uuid8}'
    cid format, NOT raw intent_uuid."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    routing = MagicMock()
    routing.get_candidates = AsyncMock(return_value=[_candidate()])
    adapter = MagicMock()
    adapter.place_order_request = AsyncMock(return_value="VENUE-1")
    adapter.await_fill_confirmation = AsyncMock(
        return_value=OrderConfirmation(
            venue="binance",
            venue_order_id="VENUE-1",
            client_order_id="gecko-42-aaaa",
            status="filled",
            filled_qty=1.0,
            fill_price=10.0,
            raw_response=None,
        )
    )
    engine = await _make_engine(db, routing=routing, adapter=adapter)
    pt = _make_paper_trade(id=42)
    await engine._dispatch_live(paper_trade=pt, size_usd=10.0)
    call = adapter.await_fill_confirmation.call_args
    cid = call.kwargs["client_order_id"]
    # Format: gecko-{paper_trade_id}-{uuid8} where uuid8 is 8 hex chars
    # (no dashes within). For paper_trade_id=42, cid is "gecko-42-XXXXXXXX".
    assert cid.startswith("gecko-42-"), f"cid format wrong: {cid!r}"
    assert len(cid) == len("gecko-42-") + 8, f"cid length wrong: {cid!r}"
    assert cid != "42", "raw intent_uuid leaked through"
    await db.close()


@pytest.mark.asyncio
async def test_dispatch_live_no_counter_on_partial(tmp_path):
    """R1+R2 C3: partial fills do NOT increment counter."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    routing = MagicMock()
    routing.get_candidates = AsyncMock(return_value=[_candidate()])
    adapter = MagicMock()
    adapter.place_order_request = AsyncMock(return_value="VENUE-1")
    adapter.await_fill_confirmation = AsyncMock(
        return_value=OrderConfirmation(
            venue="binance",
            venue_order_id="VENUE-1",
            client_order_id="gecko-42-aaaa",
            status="partial",
            filled_qty=0.5,
            fill_price=10.0,
            raw_response=None,
        )
    )
    engine = await _make_engine(db, routing=routing, adapter=adapter)
    pt = _make_paper_trade()
    await engine._dispatch_live(paper_trade=pt, size_usd=10.0)
    cur = await db._conn.execute("SELECT COUNT(*) FROM signal_venue_correction_count")
    row = await cur.fetchone()
    assert row[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_dispatch_live_no_counter_on_timeout(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    routing = MagicMock()
    routing.get_candidates = AsyncMock(return_value=[_candidate()])
    adapter = MagicMock()
    adapter.place_order_request = AsyncMock(return_value="VENUE-1")
    adapter.await_fill_confirmation = AsyncMock(
        return_value=OrderConfirmation(
            venue="binance",
            venue_order_id="VENUE-1",
            client_order_id="gecko-42-aaaa",
            status="timeout",
            filled_qty=None,
            fill_price=None,
            raw_response=None,
        )
    )
    engine = await _make_engine(db, routing=routing, adapter=adapter)
    pt = _make_paper_trade()
    await engine._dispatch_live(paper_trade=pt, size_usd=10.0)
    cur = await db._conn.execute("SELECT COUNT(*) FROM signal_venue_correction_count")
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_dispatch_live_status_rejected_no_counter(tmp_path):
    """R1-I2 fold: status='rejected' -> counter NOT incremented."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    routing = MagicMock()
    routing.get_candidates = AsyncMock(return_value=[_candidate()])
    adapter = MagicMock()
    adapter.place_order_request = AsyncMock(return_value="VENUE-1")
    adapter.await_fill_confirmation = AsyncMock(
        return_value=OrderConfirmation(
            venue="binance",
            venue_order_id="VENUE-1",
            client_order_id="gecko-42-aaaa",
            status="rejected",
            filled_qty=None,
            fill_price=None,
            raw_response=None,
        )
    )
    engine = await _make_engine(db, routing=routing, adapter=adapter)
    pt = _make_paper_trade()
    await engine._dispatch_live(paper_trade=pt, size_usd=10.0)
    cur = await db._conn.execute("SELECT COUNT(*) FROM signal_venue_correction_count")
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_dispatch_live_no_venue_writes_reject_row(tmp_path):
    """R2-M1 / Q2 fold + V1+V3 PR-stage CRITICAL fix: empty candidates ->
    live_trades reject row written with all NOT NULL columns populated."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    pt = _make_paper_trade()
    await _insert_paper_trade(db, trade_id=pt.id)
    routing = MagicMock()
    routing.get_candidates = AsyncMock(return_value=[])
    engine = await _make_engine(db, routing=routing)
    await engine._dispatch_live(paper_trade=pt, size_usd=10.0)
    cur = await db._conn.execute(
        "SELECT status, reject_reason FROM live_trades WHERE paper_trade_id=?",
        (pt.id,),
    )
    row = await cur.fetchone()
    assert row[0] == "rejected"
    assert row[1] == "no_venue"
    # counter NOT incremented
    cur = await db._conn.execute("SELECT COUNT(*) FROM signal_venue_correction_count")
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_dispatch_live_binance_auth_error_engages_killswitch(tmp_path):
    """R1-M1 fold + V1+V2+V3 PR-stage fix: BinanceAuthError ->
    KillSwitch.trigger(triggered_by, reason, duration). Earlier code
    called .engage() which does not exist on KillSwitch — production
    raised AttributeError silently."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    routing = MagicMock()
    routing.get_candidates = AsyncMock(return_value=[_candidate()])
    adapter = MagicMock()
    adapter.place_order_request = AsyncMock(side_effect=BinanceAuthError("revoked"))
    adapter.await_fill_confirmation = AsyncMock()
    engine = await _make_engine(db, routing=routing, adapter=adapter)
    pt = _make_paper_trade()
    await engine._dispatch_live(paper_trade=pt, size_usd=10.0)
    engine._ks.trigger.assert_awaited_once()
    call = engine._ks.trigger.call_args
    assert call.kwargs["reason"] == "binance_auth_revoked_mid_session"
    assert call.kwargs["triggered_by"] == "live_engine"
    assert "duration" in call.kwargs
    await db.close()


@pytest.mark.asyncio
async def test_dispatch_live_ip_ban_engages_killswitch(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    routing = MagicMock()
    routing.get_candidates = AsyncMock(return_value=[_candidate()])
    adapter = MagicMock()
    adapter.place_order_request = AsyncMock(side_effect=BinanceIPBanError("418"))
    adapter.await_fill_confirmation = AsyncMock()
    engine = await _make_engine(db, routing=routing, adapter=adapter)
    await engine._dispatch_live(paper_trade=_make_paper_trade(), size_usd=10.0)
    engine._ks.trigger.assert_awaited_once()
    call = engine._ks.trigger.call_args
    assert call.kwargs["reason"] == "binance_ip_banned"
    assert call.kwargs["triggered_by"] == "live_engine"
    await db.close()


@pytest.mark.asyncio
async def test_dispatch_live_venue_transient_no_counter(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    routing = MagicMock()
    routing.get_candidates = AsyncMock(return_value=[_candidate()])
    adapter = MagicMock()
    adapter.place_order_request = AsyncMock(side_effect=VenueTransientError("503"))
    adapter.await_fill_confirmation = AsyncMock()
    engine = await _make_engine(db, routing=routing, adapter=adapter)
    await engine._dispatch_live(paper_trade=_make_paper_trade(), size_usd=10.0)
    engine._ks.trigger.assert_not_called()
    cur = await db._conn.execute("SELECT COUNT(*) FROM signal_venue_correction_count")
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_dispatch_live_picks_highest_health_score(tmp_path):
    """R1-I2 fold: multi-candidate, dispatch picks list[0] (routing layer
    sorts by venue_health_score DESC). Verifies dispatch uses [0] which
    is the top after routing's sort."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Routing returns pre-sorted (highest score first per routing.py)
    cands = [
        _candidate(venue="binance", pair="BTCUSDT", score=0.9),
        _candidate(venue="kraken", pair="XBTUSD", score=0.6),
        _candidate(venue="other", pair="BTC-USD", score=0.4),
    ]
    routing = MagicMock()
    routing.get_candidates = AsyncMock(return_value=cands)
    engine = await _make_engine(db, routing=routing)
    pt = _make_paper_trade()
    await engine._dispatch_live(paper_trade=pt, size_usd=10.0)
    # place_order called with venue_pair from the top (binance/BTCUSDT)
    call = engine._adapter.place_order_request.call_args
    request = call.args[0]
    assert request.venue_pair == "BTCUSDT"
    # Counter for binance pair only
    cur = await db._conn.execute(
        "SELECT venue, consecutive_no_correction " "FROM signal_venue_correction_count"
    )
    rows = await cur.fetchall()
    assert {r[0]: r[1] for r in rows} == {"binance": 1}
    await db.close()


@pytest.mark.asyncio
async def test_dispatch_live_signed_disabled_no_counter(tmp_path):
    """Defense-in-depth: NotImplementedError silently returns."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    routing = MagicMock()
    routing.get_candidates = AsyncMock(return_value=[_candidate()])
    adapter = MagicMock()
    adapter.place_order_request = AsyncMock(side_effect=NotImplementedError("disabled"))
    adapter.await_fill_confirmation = AsyncMock()
    engine = await _make_engine(db, routing=routing, adapter=adapter)
    await engine._dispatch_live(paper_trade=_make_paper_trade(), size_usd=10.0)
    cur = await db._conn.execute("SELECT COUNT(*) FROM signal_venue_correction_count")
    assert (await cur.fetchone())[0] == 0
    await db.close()
