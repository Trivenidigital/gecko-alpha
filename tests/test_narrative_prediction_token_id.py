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
    # PR #72 Gap #4 — explicit assertion that price_cache is EMPTY for
    # race-coin; otherwise the test could pass on either source path.
    cur = await db._conn.execute(
        "SELECT 1 FROM price_cache WHERE coin_id = 'race-coin'"
    )
    assert (await cur.fetchone()) is None, (
        "T2b precondition: price_cache must be empty for race-coin; "
        "test exercises the snapshot-table fallback path"
    )
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
        await trade_predictions(engine, db, [pred_empty, pred_ws], settings=settings)
    skip_events = [
        e for e in logs if e.get("event") == "signal_skipped_synthetic_token_id"
    ]
    assert len(skip_events) == 2
    for e in skip_events:
        assert e["reason"] == "empty_or_whitespace_coin_id"
    assert engine.opened == []


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_resolution_check_error_fails_closed(db, settings_factory, monkeypatch):
    """T2d (adv-M2) — coin_id_resolves raises → fail-CLOSED with
    reason=resolution_check_error; engine NOT called."""
    from scout.db import CoinIdResolutionError
    from scout.trading.signals import trade_predictions

    settings = settings_factory()
    engine = _StubEngine()

    async def _broken_resolves(coin_id):
        raise CoinIdResolutionError("simulated DB outage")

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


# ---------------------------------------------------------------------------
# PR #72 reviewer-requested gaps (silent-failure + test-coverage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coin_id_resolves_raises_on_operational_error(db, monkeypatch):
    """Gap #1 (PR #72 test-coverage CRITICAL) — F3 fail-CLOSED at the
    helper source. Mock _conn.execute to raise aiosqlite.OperationalError;
    assert CoinIdResolutionError propagates from the helper itself."""
    import aiosqlite
    from scout.db import CoinIdResolutionError

    real_execute = db._conn.execute

    async def _broken_execute(sql, params=()):
        if "FROM price_cache" in sql:
            raise aiosqlite.OperationalError("simulated column rename")
        return await real_execute(sql, params)

    monkeypatch.setattr(db._conn, "execute", _broken_execute)
    with pytest.raises(CoinIdResolutionError) as exc_info:
        await db.coin_id_resolves("any-coin")
    assert "OperationalError on price_cache" in str(exc_info.value)


@pytest.mark.asyncio
async def test_coin_id_resolves_raises_db_not_initialized_when_conn_none(
    tmp_path,
):
    """PR #72 silent-failure H2 — distinct exception class when
    self._conn is None (catastrophic) vs OperationalError (transient).
    Caller dispatches to different `reason` fields."""
    from scout.db import Database, DbNotInitializedError

    d = Database(tmp_path / "x.db")
    # Don't initialize → _conn is None
    with pytest.raises(DbNotInitializedError):
        await d.coin_id_resolves("any-coin")


def test_is_tradeable_candidate_rejects_all_whitespace_inputs():
    """Gap #2 (PR #72 test-coverage CRITICAL) — cross-dispatcher
    regression pin. The strip() addition rejects ALL-whitespace
    coin_ids/tickers across all 6 callers (trade_volume_spikes,
    trade_gainers, etc.) + the new predictor.filter_laggards.

    Pre-PR: `"   "` was silently accepted by `not coin_id` (truthy).
    Post-PR: `not coin_id.strip()` rejects.

    Whitespace-PADDED inputs (e.g., `"bitcoin "` with trailing space)
    are still accepted — strip evaluates to "bitcoin" which is truthy.
    Documented behavior: this PR doesn't normalize whitespace, only
    rejects pure-whitespace junk."""
    from scout.trading.filters import _is_tradeable_candidate

    # ALL-whitespace coin_id → reject (pre-PR accepted silently)
    assert _is_tradeable_candidate("   ", "BTC") is False
    assert _is_tradeable_candidate("\t\n", "BTC") is False
    # ALL-whitespace ticker → reject (pre-PR accepted silently)
    assert _is_tradeable_candidate("bitcoin", "   ") is False
    # Empty → reject (pre-PR also rejected via `not coin_id`)
    assert _is_tradeable_candidate("", "BTC") is False
    assert _is_tradeable_candidate("bitcoin", "") is False
    # Whitespace-PADDED still accepted (stripped value is non-empty;
    # this PR doesn't normalize). Documents the boundary.
    assert _is_tradeable_candidate("bitcoin ", "BTC") is True
    assert _is_tradeable_candidate(" bitcoin", "BTC") is True
    # Clean input → accepted (existing behavior preserved)
    assert _is_tradeable_candidate("bitcoin", "BTC") is True


@_SKIP_AIOHTTP
def test_predictor_filter_laggards_drops_junk_prefix_tokens_end_to_end():
    """Gap #3 (PR #72 test-coverage HIGH) — exercise the actual
    filter_laggards function with a CoinGecko-shape response containing
    junk-prefix entries. Without this, a revert of the predictor.py
    edit would leave T4 (helper-only) green but the upstream defense
    would silently disappear."""
    from scout.narrative.predictor import filter_laggards

    raw_tokens = [
        {
            "id": "test-1",
            "symbol": "TEST1",
            "name": "Test 1",
            "market_cap": 1_000_000,
            "price_change_percentage_24h": -10.0,
            "total_volume": 100_000,
            "current_price": 1.0,
        },
        {
            "id": "real-coin",
            "symbol": "REAL",
            "name": "Real Coin",
            "market_cap": 5_000_000,
            "price_change_percentage_24h": -5.0,
            "total_volume": 100_000,
            "current_price": 2.0,
        },
        {
            "id": "wrapped-bitcoin",
            "symbol": "WBTC",
            "name": "Wrapped BTC",
            "market_cap": 10_000_000,
            "price_change_percentage_24h": -8.0,
            "total_volume": 100_000,
            "current_price": 50000.0,
        },
    ]
    result = filter_laggards(
        raw_tokens,
        category_id="test",
        category_name="Test",
        max_mcap=1e9,
        max_change=100,
        min_change=-100,
        min_volume=0,
    )
    coin_ids = [tok.coin_id for tok in result]
    # Junk-prefix entries REJECTED by upstream filter
    assert "test-1" not in coin_ids
    assert "wrapped-bitcoin" not in coin_ids
    # Real coin ACCEPTED
    assert "real-coin" in coin_ids
