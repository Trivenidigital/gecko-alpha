"""narrative_prediction token_id divergence — pre-open validation gate.

Pins the dispatcher gate at scout/trading/signals.py:trade_predictions
(AFTER _is_junk_coinid filter, BEFORE should_open call) plus the upstream
defense-in-depth filter at scout/narrative/predictor.py:filter_laggards
plus the new Database.coin_id_resolves method + new shared filters module.

Fix references:
- adv-M1: empty/whitespace handling
- adv-M2: fail-CLOSED on resolution-check exception
- arch-A1: upstream filter prevents junk in predictions table
- arch-A2: explicit Database.coin_id_resolves replaces fragile probe
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason="Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp tests",
)


@pytest.fixture
async def db(tmp_path):
    from scout.db import Database
    d = Database(tmp_path / "t.db")
    await d.initialize()
    yield d
    await d.close()


class _StubEngine:
    """Captures engine.open_trade calls without DB writes."""

    def __init__(self):
        self.opened: list[dict] = []

    async def open_trade(self, **kwargs):
        self.opened.append(kwargs)


def _make_pred(coin_id="real-coin", symbol="REAL", name="Real Coin"):
    """Minimal NarrativePrediction-shaped object for the dispatcher."""
    return SimpleNamespace(
        coin_id=coin_id,
        symbol=symbol,
        name=name,
        market_cap_at_prediction=10_000_000.0,
        narrative_fit_score=2,
        category_name="ai",
        is_control=False,
    )


# ---------------------------------------------------------------------------
# T3: Database.coin_id_resolves (arch-A2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_database_coin_id_resolves_method(db):
    """T3 — coin_id_resolves probes 4 tables (price_cache + 3 snapshots).
    Returns True if present in any, False if none. Empty/whitespace → False."""
    # Empty + whitespace
    assert await db.coin_id_resolves("") is False
    assert await db.coin_id_resolves("   ") is False

    # Unknown coin_id
    assert await db.coin_id_resolves("never-seen") is False

    # Insert into gainers_snapshots only — should resolve True
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, price_change_24h, market_cap, "
        " volume_24h, price_at_snapshot, snapshot_at) "
        "VALUES ('seen-via-gainers', 'X', 'X', 12.0, 5e6, 1000, 1.0, ?)",
        (now,),
    )
    await db._conn.commit()
    assert await db.coin_id_resolves("seen-via-gainers") is True

    # Insert into price_cache only — should also resolve True
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES ('seen-via-cache', 1.0, ?)",
        (now,),
    )
    await db._conn.commit()
    assert await db.coin_id_resolves("seen-via-cache") is True


# ---------------------------------------------------------------------------
# T2c / T1 / T2a / T2b / T2d: dispatcher gate
# ---------------------------------------------------------------------------


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_synthetic_token_id_rejected_with_telemetry(db, settings_factory):
    """T1 — coin_id missing from price_cache + snapshot tables → reject
    with `signal_skipped_synthetic_token_id` reason=token_id_not_in_*."""
    from scout.trading.signals import trade_predictions
    settings = settings_factory()
    engine = _StubEngine()
    pred = _make_pred(coin_id="synthetic-coin-xyz")
    with capture_logs() as logs:
        await trade_predictions(engine, db, [pred], settings=settings)
    events = [e for e in logs if e.get("event") == "signal_skipped_synthetic_token_id"]
    assert events, f"expected skip event; got {[e.get('event') for e in logs]}"
    assert events[0]["coin_id"] == "synthetic-coin-xyz"
    assert events[0]["reason"] == "token_id_not_in_price_cache_or_snapshots"
    # Skip event must NOT include signal_combo (arch-D2)
    assert "signal_combo" not in events[0]
    assert engine.opened == []  # adv-N3: explicit no-call assert


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_legit_in_price_cache_opens_trade(db, settings_factory):
    """T2a — coin_id in price_cache → trade opens (existing behavior preserved)."""
    from scout.trading.signals import trade_predictions
    settings = settings_factory()
    engine = _StubEngine()
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("legit-coin", 1.0, now),
    )
    await db._conn.commit()
    pred = _make_pred(coin_id="legit-coin")
    with capture_logs() as logs:
        await trade_predictions(engine, db, [pred], settings=settings)
    events = [e.get("event") for e in logs]
    assert "signal_skipped_synthetic_token_id" not in events
    assert len(engine.opened) == 1
    assert engine.opened[0]["token_id"] == "legit-coin"


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_legit_in_lookup_chain_opens_trade(db, settings_factory):
    """T2b — race scenario: coin_id missing from price_cache but PRESENT
    in gainers_snapshots → fallback accepts; trade opens."""
    from scout.trading.signals import trade_predictions
    settings = settings_factory()
    engine = _StubEngine()
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, price_change_24h, market_cap, "
        " volume_24h, price_at_snapshot, snapshot_at) "
        "VALUES ('race-coin', 'RACE', 'Race', 12.0, 5e6, 1000, 1.0, ?)",
        (now,),
    )
    await db._conn.commit()
    pred = _make_pred(coin_id="race-coin")
    with capture_logs() as logs:
        await trade_predictions(engine, db, [pred], settings=settings)
    events = [e.get("event") for e in logs]
    assert "signal_skipped_synthetic_token_id" not in events
    assert len(engine.opened) == 1
    assert engine.opened[0]["token_id"] == "race-coin"


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_empty_or_whitespace_coin_id_rejected(db, settings_factory):
    """T2c (adv-M1) — empty + whitespace coin_id → reject with
    reason=empty_or_whitespace_coin_id; engine NOT called.

    Note: NarrativePrediction model has `coin_id: str` so Pydantic
    rejects None at boundary. But empty / whitespace strings pass the
    model and arrive at the dispatcher; gate must catch both."""
    from scout.trading.signals import trade_predictions
    settings = settings_factory()
    engine = _StubEngine()
    pred_empty = _make_pred(coin_id="")
    pred_ws = _make_pred(coin_id="   ")
    with capture_logs() as logs:
        await trade_predictions(
            engine, db, [pred_empty, pred_ws], settings=settings
        )
    skip_events = [
        e for e in logs if e.get("event") == "signal_skipped_synthetic_token_id"
    ]
    assert len(skip_events) == 2
    for e in skip_events:
        assert e["reason"] == "empty_or_whitespace_coin_id"
    assert engine.opened == []


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_resolution_check_error_fails_closed(
    db, settings_factory, monkeypatch
):
    """T2d (adv-M2) — coin_id_resolves raises → fail-CLOSED with
    reason=resolution_check_error; engine NOT called."""
    from scout.trading.signals import trade_predictions
    settings = settings_factory()
    engine = _StubEngine()

    async def _broken_resolves(coin_id):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(db, "coin_id_resolves", _broken_resolves)
    pred = _make_pred(coin_id="some-coin")
    with capture_logs() as logs:
        await trade_predictions(engine, db, [pred], settings=settings)
    skip_events = [
        e for e in logs if e.get("event") == "signal_skipped_synthetic_token_id"
    ]
    assert len(skip_events) == 1
    assert skip_events[0]["reason"] == "resolution_check_error"
    assert engine.opened == []


# ---------------------------------------------------------------------------
# T4: upstream defense-in-depth (arch-A1)
# ---------------------------------------------------------------------------


def test_predictor_filter_laggards_rejects_junk_prefix():
    """T4 — _is_tradeable_candidate (now in scout/trading/filters.py)
    rejects junk-prefix coin_ids before they enter the predictions table."""
    from scout.trading.filters import _is_tradeable_candidate

    # Real CoinGecko-shape inputs
    assert _is_tradeable_candidate("bitcoin", "BTC") is True
    assert _is_tradeable_candidate("test-1", "TEST1") is False
    assert _is_tradeable_candidate("wrapped-bitcoin", "WBTC") is False
    assert _is_tradeable_candidate("bridged-usdc", "USDC") is False
    # Empty / whitespace
    assert _is_tradeable_candidate("", "BTC") is False
    assert _is_tradeable_candidate("   ", "BTC") is False
    assert _is_tradeable_candidate("bitcoin", "") is False
    # Non-ASCII
    assert _is_tradeable_candidate("bitcoin", "比特币") is False


# ---------------------------------------------------------------------------
# T5: shared module back-compat
# ---------------------------------------------------------------------------


@_SKIP_AIOHTTP
def test_filters_module_exports_match_signals_back_compat():
    """T5 — `scout/trading/filters.py` exports the symbols still
    importable from `scout.trading.signals` for back-compat with existing
    callers."""
    from scout.trading import filters as filt
    from scout.trading import signals as sig

    # Both modules expose the same callable
    assert filt._is_junk_coinid is sig._is_junk_coinid
    assert filt._is_tradeable_candidate is sig._is_tradeable_candidate
