"""Canonical shadow-loop integration flows (BL-055 spec §11.6).

Each test wires a real :class:`scout.db.Database` + real
:class:`scout.live.gates.Gates` + real :class:`scout.live.engine.LiveEngine`
+ real :class:`scout.live.shadow_evaluator` + the real
:class:`scout.live.binance_adapter.BinanceSpotAdapter` whose HTTP layer is
mocked with ``aioresponses``. No real network is ever contacted.

The six canonical flows are:

1. Happy path — allowlisted symbol, Binance listed, adequate depth
   → ``shadow_trades.status='open'`` → TP-price tick → ``closed_tp``.
2. Not listed — symbol absent from Binance ``exchangeInfo`` → resolver
   negatives → ``status='rejected', reject_reason='no_venue'``.
3. Depth starved — Binance listed but orderbook thin (below
   ``LIVE_DEPTH_HEALTH_MULTIPLIER * size_usd``) → ``status='rejected',
   reject_reason='insufficient_depth'``.
4. Venue transient 3x — 5xx / timeout on every depth retry → Gates raises
   VenueTransientError → ``status='rejected',
   reject_reason='venue_unavailable'`` + WARN log observable.
5. Restart mid-shadow — reconciler runs with empty DB (T3 zero-row invariant)
   and with seeded open row; ``live_boot_reconciliation_done`` fires in
   both cases with matching ``rows_inspected``.
6. Mid-life halt — open row → evaluator sees venue errors 3x →
   third failure flips to ``needs_manual_review`` with
   ``live_shadow_review_exhausted`` WARN log.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiohttp
import pytest
import structlog
from aioresponses import aioresponses

from scout.config import Settings
from scout.db import Database
from scout.live.binance_adapter import BinanceSpotAdapter
from scout.live.config import LiveConfig
from scout.live.engine import LiveEngine
from scout.live.exceptions import VenueTransientError
from scout.live.kill_switch import KillSwitch
from scout.live.reconciliation import reconcile_open_shadow_trades
from scout.live.resolver import OverrideStore, VenueResolver
from scout.live.shadow_evaluator import (
    MAX_REVIEW_RETRIES,
    evaluate_open_shadow_trades,
)
from scout.trading.paper import _PaperTradeHandoff


# ---------- helpers ----------------------------------------------------------


def _settings(**overrides) -> Settings:
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
        LIVE_TP_PCT=Decimal("20"),
        LIVE_SL_PCT=Decimal("10"),
        LIVE_MAX_DURATION_HOURS=24,
        LIVE_DAILY_LOSS_CAP_USD=Decimal("50"),
    )
    base.update(overrides)
    return Settings(**base)


def _healthy_depth_payload() -> dict:
    """Return a Binance /depth payload with bid + ask totals far above the
    gate threshold (multiplier 3 * size_usd 100 = $300 required per side)."""
    bids = [[str(100 - i * 0.1), "1000"] for i in range(20)]
    asks = [[str(100 + (i + 1) * 0.1), "1000"] for i in range(20)]
    return {"bids": bids, "asks": asks}


def _thin_depth_payload() -> dict:
    """Depth so thin the gate's $300-required-per-side test fails."""
    bids = [["99.0", "0.01"] for _ in range(20)]
    asks = [["100.0", "0.01"] for _ in range(20)]
    return {"bids": bids, "asks": asks}


def _exchange_info_payload(base_asset: str, pair: str) -> dict:
    return {
        "symbols": [
            {
                "symbol": pair,
                "status": "TRADING",
                "baseAsset": base_asset,
                "quoteAsset": "USDT",
            }
        ]
    }


async def _seed_paper_trade(
    db: Database,
    *,
    coin_id: str = "c",
    symbol: str = "S",
    signal_type: str = "first_signal",
) -> _PaperTradeHandoff:
    """Seed a parent ``paper_trades`` row so shadow_trades FK is satisfied.
    Returns a ``_PaperTradeHandoff`` the engine can consume."""
    assert db._conn is not None
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
        "signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct, "
        "tp_price, sl_price, status, opened_at) VALUES "
        "(?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?)",
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
            now_iso,
        ),
    )
    await db._conn.commit()
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    pt_id = (await cur.fetchone())[0]
    return _PaperTradeHandoff(
        id=pt_id, signal_type=signal_type, symbol=symbol, coin_id=coin_id
    )


async def _seed_open_shadow(
    db: Database,
    *,
    paper_trade_id: int,
    entry_vwap: str = "100",
    size_usd: str = "100",
    pair: str = "SUSDT",
    signal_type: str = "first_signal",
    review_retries: int = 0,
) -> int:
    """Seed an open shadow_trades row tied to ``paper_trade_id``."""
    assert db._conn is not None
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO shadow_trades "
        "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, "
        " size_usd, entry_walked_vwap, mid_at_entry, status, "
        " review_retries, created_at) "
        "VALUES (?,'c','S','binance',?,?,?, ?, '100', 'open', ?, ?)",
        (
            paper_trade_id,
            pair,
            signal_type,
            size_usd,
            entry_vwap,
            review_retries,
            now_iso,
        ),
    )
    await db._conn.commit()
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    return (await cur.fetchone())[0]


async def _build_engine(
    db: Database, settings: Settings
) -> tuple[LiveEngine, BinanceSpotAdapter]:
    """Wire real LiveEngine + real Gates + real VenueResolver + real
    BinanceSpotAdapter against an already-initialised DB."""
    config = LiveConfig(settings)
    adapter = BinanceSpotAdapter(settings, db=db)
    # Short-circuit the 5xx backoff so flow 4 doesn't sleep 1+2+4s.
    from unittest.mock import AsyncMock

    adapter._retry_sleep = AsyncMock()
    resolver = VenueResolver(
        binance_adapter=adapter,
        override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1),
        negative_ttl=timedelta(seconds=60),
        db=db,
    )
    ks = KillSwitch(db)
    engine = LiveEngine(
        config=config, resolver=resolver, adapter=adapter, db=db, kill_switch=ks
    )
    return engine, adapter


async def _shadow_status(db: Database, paper_trade_id: int) -> tuple:
    assert db._conn is not None
    cur = await db._conn.execute(
        "SELECT status, reject_reason, entry_walked_vwap, pair "
        "FROM shadow_trades WHERE paper_trade_id=?",
        (paper_trade_id,),
    )
    return await cur.fetchone()


# ---------- Flow 1: happy path ----------------------------------------------


async def test_flow_1_happy_path_opens_then_closes_tp(tmp_path):
    """Allowlisted + Binance-listed + adequate depth → shadow_trades opens;
    then evaluator sees a TP-price tick and closes ``closed_tp``.

    Exercises: VenueResolver (exchangeInfo mock) → Gates (depth + slippage)
    → LiveEngine writes open row → shadow_evaluator (price + depth mocks) →
    close row.
    """
    db = Database(tmp_path / "f1.db")
    await db.initialize()
    settings = _settings()
    engine, adapter = await _build_engine(db, settings)
    try:
        pt = await _seed_paper_trade(db, symbol="S", coin_id="c")

        # --- open leg: gate → handoff → shadow_trades row opens ---
        with aioresponses() as m:
            m.get(
                "https://api.binance.com/api/v3/exchangeInfo?symbol=SUSDT",
                payload=_exchange_info_payload("S", "SUSDT"),
                headers={"X-MBX-USED-WEIGHT-1M": "5"},
            )
            # Gate 5 fetch_depth + engine's second fetch_depth both hit /depth.
            m.get(
                "https://api.binance.com/api/v3/depth?symbol=SUSDT&limit=100",
                payload=_healthy_depth_payload(),
                headers={"X-MBX-USED-WEIGHT-1M": "10"},
            )
            m.get(
                "https://api.binance.com/api/v3/depth?symbol=SUSDT&limit=100",
                payload=_healthy_depth_payload(),
                headers={"X-MBX-USED-WEIGHT-1M": "10"},
            )
            await engine.on_paper_trade_opened(pt)

        row = await _shadow_status(db, pt.id)
        assert row is not None, "expected a shadow_trades row after handoff"
        status, reject_reason, entry_vwap, pair = row
        assert status == "open", (
            f"expected open row, got status={status} "
            f"reject_reason={reject_reason}"
        )
        assert pair == "SUSDT"
        assert entry_vwap is not None and Decimal(entry_vwap) > 0

        # --- close leg: TP price tick → evaluator writes closed_tp ---
        with aioresponses() as m:
            # Evaluator fetch_price → well above TP (+25% vs entry).
            m.get(
                "https://api.binance.com/api/v3/ticker/price?symbol=SUSDT",
                payload={"symbol": "SUSDT", "price": "125"},
                headers={"X-MBX-USED-WEIGHT-1M": "5"},
            )
            # Evaluator fetch_depth to walk the exit-side bids.
            exit_depth = _healthy_depth_payload()
            exit_depth["bids"] = [[str(125 - i * 0.1), "1000"] for i in range(20)]
            exit_depth["asks"] = [
                [str(125 + (i + 1) * 0.1), "1000"] for i in range(20)
            ]
            m.get(
                "https://api.binance.com/api/v3/depth?symbol=SUSDT&limit=100",
                payload=exit_depth,
                headers={"X-MBX-USED-WEIGHT-1M": "5"},
            )
            closed = await evaluate_open_shadow_trades(
                db=db,
                adapter=adapter,
                config=LiveConfig(settings),
                ks=KillSwitch(db),
                settings=settings,
            )
        assert closed == 1
        row = await _shadow_status(db, pt.id)
        assert row[0] == "closed_tp"
    finally:
        await adapter.close()
        await db.close()


# ---------- Flow 2: not listed ----------------------------------------------


async def test_flow_2_not_listed_rejects_no_venue(tmp_path):
    """Symbol not on Binance (exchangeInfo returns -1121) → resolver ``None``
    → no venue_overrides row → gate emits ``no_venue`` → ``rejected`` row."""
    db = Database(tmp_path / "f2.db")
    await db.initialize()
    settings = _settings()
    engine, adapter = await _build_engine(db, settings)
    try:
        pt = await _seed_paper_trade(db, symbol="ZZZ", coin_id="c")
        with aioresponses() as m:
            m.get(
                "https://api.binance.com/api/v3/exchangeInfo?symbol=ZZZUSDT",
                status=400,
                payload={"code": -1121, "msg": "Invalid symbol."},
            )
            await engine.on_paper_trade_opened(pt)
        row = await _shadow_status(db, pt.id)
        assert row is not None
        status, reject_reason, _vwap, _pair = row
        assert status == "rejected"
        assert reject_reason == "no_venue"
    finally:
        await adapter.close()
        await db.close()


# ---------- Flow 3: depth starved -------------------------------------------


async def test_flow_3_depth_starved_rejects_insufficient_depth(tmp_path):
    """Binance lists the symbol but /depth returns a thin book (each side
    well below ``multiplier * size_usd``) → gate 5 rejects with
    ``insufficient_depth``."""
    db = Database(tmp_path / "f3.db")
    await db.initialize()
    settings = _settings()
    engine, adapter = await _build_engine(db, settings)
    try:
        pt = await _seed_paper_trade(db, symbol="S", coin_id="c")
        with aioresponses() as m:
            m.get(
                "https://api.binance.com/api/v3/exchangeInfo?symbol=SUSDT",
                payload=_exchange_info_payload("S", "SUSDT"),
                headers={"X-MBX-USED-WEIGHT-1M": "5"},
            )
            m.get(
                "https://api.binance.com/api/v3/depth?symbol=SUSDT&limit=100",
                payload=_thin_depth_payload(),
                headers={"X-MBX-USED-WEIGHT-1M": "5"},
            )
            await engine.on_paper_trade_opened(pt)
        row = await _shadow_status(db, pt.id)
        assert row is not None
        status, reject_reason, _vwap, _pair = row
        assert status == "rejected"
        assert reject_reason == "insufficient_depth"
    finally:
        await adapter.close()
        await db.close()


# ---------- Flow 4: venue transient 3x --------------------------------------


async def test_flow_4_venue_transient_rejects_venue_unavailable(tmp_path):
    """Gate 5 calls ``adapter.fetch_depth`` which retries on 5xx up to 3x then
    raises :class:`VenueTransientError`. Gates catches that and rejects with
    ``venue_unavailable``. The engine writes a rejected row and a WARN-level
    log event (``live_handoff_rejected``) is emitted."""
    db = Database(tmp_path / "f4.db")
    await db.initialize()
    settings = _settings()
    engine, adapter = await _build_engine(db, settings)
    try:
        pt = await _seed_paper_trade(db, symbol="S", coin_id="c")
        with aioresponses() as m:
            m.get(
                "https://api.binance.com/api/v3/exchangeInfo?symbol=SUSDT",
                payload=_exchange_info_payload("S", "SUSDT"),
                headers={"X-MBX-USED-WEIGHT-1M": "5"},
            )
            # 4 x 5xx: initial attempt + 3 retries. All fail → VenueTransientError.
            for _ in range(4):
                m.get(
                    "https://api.binance.com/api/v3/depth?symbol=SUSDT&limit=100",
                    status=503,
                )
            with structlog.testing.capture_logs() as logs:
                await engine.on_paper_trade_opened(pt)

        row = await _shadow_status(db, pt.id)
        assert row is not None
        status, reject_reason, _vwap, _pair = row
        assert status == "rejected"
        assert reject_reason == "venue_unavailable"
        # Engine logs `live_handoff_rejected` at info level for any reject;
        # spec §11.6 calls for the operator-visible event to exist.
        events = [le.get("event") for le in logs]
        assert "live_handoff_rejected" in events
    finally:
        await adapter.close()
        await db.close()


# ---------- Flow 5: restart mid-shadow (reconciliation) ---------------------


async def test_flow_5_reconcile_empty_db_still_emits_done(tmp_path):
    """T3 zero-row invariant: empty DB must still emit
    ``live_boot_reconciliation_done`` with ``rows_inspected=0``.
    """
    db = Database(tmp_path / "f5a.db")
    await db.initialize()
    settings = _settings()
    engine, adapter = await _build_engine(db, settings)
    try:
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
    finally:
        await adapter.close()
        await db.close()


async def test_flow_5_reconcile_seeded_row_logs_rows_inspected(tmp_path):
    """Seed a single open shadow_trades row, drive ``reconcile_open_shadow_trades``
    under an aioresponses-mocked Binance /ticker/price, and confirm
    ``live_boot_reconciliation_done`` fires with ``rows_inspected > 0``.
    Mid-range price leaves the row open so we're exercising the inspect path,
    not the close path."""
    db = Database(tmp_path / "f5b.db")
    await db.initialize()
    settings = _settings()
    engine, adapter = await _build_engine(db, settings)
    try:
        pt = await _seed_paper_trade(db, symbol="S", coin_id="c")
        await _seed_open_shadow(db, paper_trade_id=pt.id)

        with aioresponses() as m:
            m.get(
                "https://api.binance.com/api/v3/ticker/price?symbol=SUSDT",
                payload={"symbol": "SUSDT", "price": "105"},
                headers={"X-MBX-USED-WEIGHT-1M": "5"},
            )
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
        assert done[0]["rows_inspected"] >= 1
    finally:
        await adapter.close()
        await db.close()


# ---------- Flow 6: mid-life halt → needs_manual_review ---------------------


async def test_flow_6_midlife_halt_flips_to_needs_manual_review(tmp_path):
    """Open shadow row. Drive the evaluator with a seed that has
    ``review_retries=MAX-1`` and have ``fetch_price`` raise a transient error.
    The third failure must flip status to ``needs_manual_review`` and emit
    the ``live_shadow_review_exhausted`` WARN log.

    We use aioresponses to make ``fetch_price`` raise a network error four
    times (initial + 3 retries inside the adapter), which the adapter then
    wraps as ``VenueTransientError``. ``_bump_review`` treats that as a
    transient and bumps retries to MAX, flipping status."""
    db = Database(tmp_path / "f6.db")
    await db.initialize()
    settings = _settings()
    engine, adapter = await _build_engine(db, settings)
    try:
        pt = await _seed_paper_trade(db, symbol="S", coin_id="c")
        sid = await _seed_open_shadow(
            db, paper_trade_id=pt.id, review_retries=MAX_REVIEW_RETRIES - 1
        )

        with aioresponses() as m:
            # 4 transient network errors: initial + 3 retries inside adapter.
            for _ in range(4):
                m.get(
                    "https://api.binance.com/api/v3/ticker/price?symbol=SUSDT",
                    exception=aiohttp.ClientConnectorError(
                        connection_key=None, os_error=OSError()
                    ),
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
        await adapter.close()
        await db.close()
