"""Tests for the 8 pre-trade gates (spec §5).

First-failure-wins ordering:
  1 kill_switch → 2 allowlist (sentinel) → 3/4 venue/override_disabled
  → 5 depth_health (VenueTransientError → venue_unavailable) → 6 slippage
  → 7 exposure → 8 balance (live-only NotImplementedError in BL-055).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.config import LiveConfig
from scout.live.exceptions import VenueTransientError
from scout.live.gates import VALID_REJECT_REASONS, Gates
from scout.live.kill_switch import KillSwitch
from scout.live.types import Depth, DepthLevel, ResolvedVenue

# ---------- fixture helpers --------------------------------------------------


def _depth(asks=None, bids=None, mid=Decimal("100")):
    """Build a Depth with sensible defaults that easily clear depth_health."""
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


def _venue(symbol="TEST", pair="TUSDT"):
    return ResolvedVenue(symbol=symbol, venue="binance", pair=pair, source="cache")


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


async def _make_gates(
    db,
    *,
    settings=None,
    depth=None,
    venue=None,
    fetch_depth_exc=None,
):
    s = settings or _settings()
    config = LiveConfig(s)
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value=venue if venue is not None else _venue())
    adapter = AsyncMock()
    if fetch_depth_exc is not None:
        adapter.fetch_depth = AsyncMock(side_effect=fetch_depth_exc)
    else:
        adapter.fetch_depth = AsyncMock(
            return_value=depth if depth is not None else _depth()
        )
    ks = KillSwitch(db)
    return Gates(
        config=config,
        db=db,
        resolver=resolver,
        adapter=adapter,
        kill_switch=ks,
    )


async def _seed_open_shadow(
    db, *, count, size_usd, symbol_prefix="OPEN", paper_trade_id=1
):
    """Seed N open shadow_trades rows (+ the referenced paper_trades row).

    paper_trades has many NOT NULL columns; we INSERT OR IGNORE one row with
    paper_trade_id matching FK, then attach N shadow_trades rows that all
    reference it.
    """
    assert db._conn is not None
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT OR IGNORE INTO paper_trades "
        "(id, token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, "
        " tp_price, sl_price, status, opened_at) "
        "VALUES (?, 'tok', 'TOK', 'Tok', 'eth', 'first_signal', '{}', "
        " 1.0, 100.0, 100.0, 1.2, 0.9, 'open', ?)",
        (paper_trade_id, now),
    )
    for i in range(count):
        await db._conn.execute(
            "INSERT INTO shadow_trades "
            "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, "
            " size_usd, status, created_at) "
            "VALUES (?, ?, ?, 'binance', 'XUSDT', 'first_signal', "
            " ?, 'open', ?)",
            (
                paper_trade_id,
                f"coin-{i}",
                f"{symbol_prefix}{i}",
                str(size_usd),
                now,
            ),
        )
    await db._conn.commit()


# ---------- tests ------------------------------------------------------------


def test_param_lists_cover_check_constraint():
    """gates.VALID_REJECT_REASONS must match the shadow/live_trades
    reject_reason CHECK constraint exactly. Spec §3.1, extended in
    BL-NEW-LIVE-HYBRID M1 v2.1 with 7 new reject_reasons (Tasks 7+7.5).
    The DB CHECK constraint extension on prod is migrated via
    bl_reject_reason_extend_v1 (schema_version 20260512)."""
    expected = {
        "no_venue",
        "insufficient_depth",
        "slippage_exceeds_cap",
        "insufficient_balance",
        "daily_cap_hit",
        "kill_switch",
        "exposure_cap",
        "override_disabled",
        "venue_unavailable",
        # BL-NEW-LIVE-HYBRID M1 v2.1 additions:
        "notional_cap_exceeded",
        "signal_disabled",
        "token_aggregate",
        "dual_signal_aggregate",
        "all_candidates_failed",
        "master_kill",
        "mode_paper",
        # M1.5a (design-stage R1-I1 + R2-I3) additions:
        "live_signed_disabled",
        "api_key_lacks_trade_scope",
    }
    assert VALID_REJECT_REASONS == expected


async def test_gates_pass_happy_path(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    gates = await _make_gates(db)
    result, venue = await gates.evaluate(
        signal_type="first_signal",
        symbol="TEST",
        size_usd=Decimal("100"),
    )
    assert result.passed is True
    assert result.reject_reason is None
    assert venue is not None
    await db.close()


async def test_gate_kill_switch_rejects(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    kid, _won = await ks.trigger(
        triggered_by="manual",
        reason="test",
        duration=timedelta(hours=1),
    )
    gates = await _make_gates(db)
    result, venue = await gates.evaluate(
        signal_type="first_signal",
        symbol="TEST",
        size_usd=Decimal("100"),
    )
    assert result.passed is False
    assert result.reject_reason == "kill_switch"
    assert str(kid) in result.detail
    assert venue is None
    await db.close()


async def test_gate_allowlist_skip_returns_no_rejection(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    gates = await _make_gates(db)
    result, venue = await gates.evaluate(
        signal_type="volume_spike",  # not in allowlist
        symbol="TEST",
        size_usd=Decimal("100"),
    )
    assert result.passed is False
    assert result.reject_reason is None
    assert result.detail == "not_allowlisted"
    assert venue is None
    await db.close()


async def test_gate_no_venue_when_resolver_returns_none(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # resolver returns None, no venue_overrides row exists.
    gates = await _make_gates(db, venue=None)
    # Override the resolver mock to explicitly return None.
    gates._resolver.resolve = AsyncMock(return_value=None)
    result, venue = await gates.evaluate(
        signal_type="first_signal",
        symbol="UNKNOWN",
        size_usd=Decimal("100"),
    )
    assert result.passed is False
    assert result.reject_reason == "no_venue"
    assert venue is None
    await db.close()


async def test_gate_override_disabled(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "INSERT INTO venue_overrides "
        "(symbol, venue, pair, disabled, note, created_at, updated_at) "
        "VALUES ('TEST','binance','TUSDT',1,'banned',"
        " '2026-04-23T00Z','2026-04-23T00Z')"
    )
    await db._conn.commit()
    gates = await _make_gates(db, venue=None)
    gates._resolver.resolve = AsyncMock(return_value=None)
    result, venue = await gates.evaluate(
        signal_type="first_signal",
        symbol="TEST",
        size_usd=Decimal("100"),
    )
    assert result.passed is False
    assert result.reject_reason == "override_disabled"
    assert venue is None
    await db.close()


async def test_gate_insufficient_depth(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Tiny depth: 10 levels * price 100 * qty 0.01 = $10 total per side.
    # Required = multiplier(3) * size_usd(100) = $300. Fails.
    thin_asks = tuple(
        DepthLevel(price=Decimal("100"), qty=Decimal("0.01")) for _ in range(10)
    )
    thin_bids = tuple(
        DepthLevel(price=Decimal("99"), qty=Decimal("0.01")) for _ in range(10)
    )
    depth = _depth(asks=thin_asks, bids=thin_bids)
    gates = await _make_gates(db, depth=depth)
    result, venue = await gates.evaluate(
        signal_type="first_signal",
        symbol="TEST",
        size_usd=Decimal("100"),
    )
    assert result.passed is False
    assert result.reject_reason == "insufficient_depth"
    assert venue is not None  # venue was resolved before depth check
    await db.close()


async def test_gate_slippage_exceeds_cap(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Steep ask curve: first level thin at mid, later levels at huge prices.
    # mid=100, walk_asks for $100 must cross into a level priced ~200
    # to produce slippage_bps ~5000 (far above cap=50).
    # Level 1 carries only $1 of notional (10 qty * $0.10 wait - let's use
    # price=100, qty=0.01 = $1 notional); so $99 of the $100 request walks
    # up to the next level, priced at $200.
    steep_asks = (
        DepthLevel(price=Decimal("100"), qty=Decimal("0.01")),  # $1
        DepthLevel(price=Decimal("200"), qty=Decimal("1000")),  # $200_000
    ) + tuple(
        DepthLevel(
            price=Decimal("200") + Decimal(i),
            qty=Decimal("1000"),
        )
        for i in range(8)
    )
    # Healthy bids so depth_health passes.
    healthy_bids = tuple(
        DepthLevel(
            price=Decimal("100") - Decimal(i) * Decimal("0.01"),
            qty=Decimal("1000"),
        )
        for i in range(10)
    )
    depth = _depth(asks=steep_asks, bids=healthy_bids)
    gates = await _make_gates(db, depth=depth)
    result, venue = await gates.evaluate(
        signal_type="first_signal",
        symbol="TEST",
        size_usd=Decimal("100"),
    )
    assert result.passed is False
    assert result.reject_reason == "slippage_exceeds_cap"
    assert venue is not None
    await db.close()


async def test_gate_exposure_cap_sum(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Seed 4 open trades at $125 each → $500 exposure == cap.
    # New request of $50 → $550 > cap(500). Reject with exposure_cap.
    # Also keep count (4 + 1 = 5, equal to max_positions but the sum check
    # fires first; we pass max_positions=10 to force the SUM branch.)
    settings = _settings(LIVE_MAX_OPEN_POSITIONS=10)
    await _seed_open_shadow(db, count=4, size_usd=Decimal("125"))
    gates = await _make_gates(db, settings=settings)
    result, venue = await gates.evaluate(
        signal_type="first_signal",
        symbol="TEST",
        size_usd=Decimal("50"),
    )
    assert result.passed is False
    assert result.reject_reason == "exposure_cap"
    assert "sum=" in result.detail
    assert venue is not None
    await db.close()


async def test_gate_exposure_cap_count(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Seed 5 open trades at $1 each → count=5, sum=$5.
    # cap_max_positions=5 → count >= max fires (5 >= 5).
    # Use a large max_exposure so the SUM branch doesn't trip first.
    settings = _settings(
        LIVE_MAX_EXPOSURE_USD=Decimal("100000"),
        LIVE_MAX_OPEN_POSITIONS=5,
    )
    await _seed_open_shadow(db, count=5, size_usd=Decimal("1"))
    gates = await _make_gates(db, settings=settings)
    result, venue = await gates.evaluate(
        signal_type="first_signal",
        symbol="TEST",
        size_usd=Decimal("10"),
    )
    assert result.passed is False
    assert result.reject_reason == "exposure_cap"
    assert "count=" in result.detail
    assert venue is not None
    await db.close()


async def test_gate_venue_unavailable_on_transient_error(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    gates = await _make_gates(
        db,
        fetch_depth_exc=VenueTransientError("binance 5xx"),
    )
    result, venue = await gates.evaluate(
        signal_type="first_signal",
        symbol="TEST",
        size_usd=Decimal("100"),
    )
    assert result.passed is False
    assert result.reject_reason == "venue_unavailable"
    assert "binance 5xx" in result.detail
    assert venue is not None  # venue resolved before fetch_depth
    await db.close()


async def test_gate_balance_returns_live_signed_disabled_in_live_mode(tmp_path):
    """M1.5a (PR #86 R1-I1 fold): LIVE_MODE=live with default
    LIVE_USE_REAL_SIGNED_REQUESTS=False returns reject_reason=
    'live_signed_disabled' (kill-switch state visibility on dashboard)
    instead of the M1 NotImplementedError. BL-055 contract preserved
    semantically — Gate 10 still refuses to fire — but operator
    dashboard can distinguish 'flag-off revert posture' from a real
    balance shortage."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        LIVE_MODE="live"
    )  # LIVE_USE_REAL_SIGNED_REQUESTS defaults False
    gates = await _make_gates(db, settings=settings)
    result, _venue = await gates.evaluate(
        signal_type="first_signal",
        symbol="TEST",
        size_usd=Decimal("100"),
    )
    assert result.passed is False
    assert result.reject_reason == "live_signed_disabled"
    assert "emergency-revert" in result.detail
    await db.close()
