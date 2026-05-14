"""BL-076: junk filter expansion (test- prefix) + symbol/name population.

Tests for two bugs surfaced by operator audit 2026-05-04:
1. CoinGecko placeholder coins (test-1..test-N) bypassed PR #44 junk filter.
   Trades #980 (first_signal -$9.96) and #1551 (volume_spike +$188.91 by
   lucky pump) opened against `test-3`.
2. volume_spike + narrative_prediction + chain_completed dispatch paths
   wrote empty symbol+name to paper_trades, masking junk in the dashboard.

Test layout:
- T1, T1b — junk filter prefix expansion
- T2, T2b — engine-level WARNING + parallel INFO event (defense-in-depth)
- T3 — trade_volume_spikes wires symbol+name
- T4 — trade_predictions wires symbol+name
- T5 — Database.lookup_symbol_name_by_coin_id sequential lookup (4 cases)
       + chain_completed dispatcher integration (T5e/f) + narrow-except (T5g)
"""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest
import structlog
from structlog.testing import capture_logs

from scout.trading.signals import _is_junk_coinid


def _test_settings():
    from scout.config import Settings

    return Settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


# ---------------------------------------------------------------------------
# Task 1: junk filter — `test-` prefix
# ---------------------------------------------------------------------------


def test_is_junk_coinid_rejects_test_prefix():
    """T1 — pins the test-N placeholder bug. CoinGecko has test-1..test-N
    placeholder coins with real price feeds; they MUST be rejected at
    admission to prevent paper trades like #1551 (test-3 / volume_spike)."""
    assert _is_junk_coinid("test-3") is True
    assert _is_junk_coinid("test-1") is True
    assert _is_junk_coinid("test-99") is True
    assert _is_junk_coinid("test-coin") is True


def test_is_junk_coinid_does_not_overreach_on_test_substrings():
    """T1b — guard against false positives. Tokens whose slug merely
    CONTAINS 'test' must NOT be rejected. The prefix match is anchored
    at slug start."""
    assert _is_junk_coinid("protest-coin") is False
    assert _is_junk_coinid("biggest-token") is False
    assert _is_junk_coinid("pre-testnet") is False
    assert _is_junk_coinid("pretest") is False
    # Existing junk patterns unaffected
    assert _is_junk_coinid("wrapped-bitcoin") is True
    assert _is_junk_coinid("bridged-usdc") is True


# ---------------------------------------------------------------------------
# Task 2: engine WARNING + parallel INFO event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_trade_logs_warning_when_symbol_and_name_both_empty(tmp_path):
    """T2 — engine-level defense-in-depth: log WARNING when caller
    forgets to pass symbol+name. Bug 2 evidence: 150+ paper trades
    across 3 dispatcher paths had empty symbol+name; operator audit
    dashboard couldn't identify them.

    Project uses structlog.PrintLoggerFactory() — pytest's caplog
    only captures stdlib logging, NOT structlog's stdout print path.
    Use structlog.testing.capture_logs() to intercept the structured
    event dict directly (M1 fix).
    """
    from scout.config import Settings
    from scout.db import Database
    from scout.trading.engine import TradingEngine

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    settings = _test_settings()
    engine = TradingEngine("paper", sd, settings)
    with capture_logs() as captured:
        await engine.open_trade(
            token_id="some-coin",
            signal_type="volume_spike",
            signal_data={"foo": "bar"},
            signal_combo="vs|none",
            entry_price=0.001,
        )
    # Pin exact event names — substring match too loose (per aff3517 #6).
    warning_events = [
        e
        for e in captured
        if e.get("event") == "open_trade_called_with_empty_symbol_and_name"
    ]
    info_events = [e for e in captured if e.get("event") == "trade_metadata_empty"]
    assert (
        warning_events
    ), f"Expected WARNING event; got events: {[e.get('event') for e in captured]}"
    assert (
        info_events
    ), f"Expected parallel INFO event (A3); got: {[e.get('event') for e in captured]}"
    assert warning_events[0].get("token_id") == "some-coin"
    assert warning_events[0].get("signal_type") == "volume_spike"
    assert warning_events[0].get("signal_combo") == "vs|none"
    assert info_events[0].get("reason") == "empty_metadata"
    await sd.close()


@pytest.mark.asyncio
async def test_open_trade_warning_fires_even_during_warmup(tmp_path, monkeypatch):
    """T2b — pins F9 mitigation. Engine WARNING placement BEFORE
    PAPER_STARTUP_WARMUP_SECONDS gate. Asserts both WARNING + warmup-skip
    events fire; warmup-skip alone (without WARNING) means the placement
    regressed."""
    from scout.config import Settings
    from scout.db import Database
    from scout.trading.engine import TradingEngine

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    settings = _test_settings()
    monkeypatch.setattr(settings, "PAPER_STARTUP_WARMUP_SECONDS", 10)
    engine = TradingEngine("paper", sd, settings)
    with capture_logs() as captured:
        result = await engine.open_trade(
            token_id="warmup-test",
            signal_type="volume_spike",
            signal_data={"foo": "bar"},
            signal_combo="vs|none",
            entry_price=0.001,
        )
    # open_trade returns None during warmup
    assert result is None
    events = [e.get("event") for e in captured]
    # All three events must fire — WARNING placement is BEFORE warmup gate
    assert (
        "open_trade_called_with_empty_symbol_and_name" in events
    ), f"WARNING regressed below warmup gate; got: {events}"
    assert (
        "trade_metadata_empty" in events
    ), f"INFO event regressed below warmup gate; got: {events}"
    assert (
        "trade_skipped_warmup" in events
    ), f"warmup gate didn't fire (test setup bug); got: {events}"
    await sd.close()


# ---------------------------------------------------------------------------
# Task 3: trade_volume_spikes wires symbol+name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trade_volume_spikes_passes_symbol_and_name_to_engine(tmp_path):
    """T3 — pins Bug 2 for volume_spike path. VolumeSpike Pydantic model
    carries symbol+name; trade_volume_spikes was calling open_trade
    without them, leaving empty strings in paper_trades."""
    from scout.config import Settings
    from scout.db import Database
    from scout.spikes.models import VolumeSpike
    from scout.trading.signals import trade_volume_spikes

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    settings = _test_settings()
    captured = {}

    class FakeEngine:
        async def open_trade(self, **kwargs):
            captured.update(kwargs)
            return 1

    spike = VolumeSpike(
        coin_id="real-coin",
        symbol="REAL",
        name="Real Coin",
        current_volume=1_000_000,
        avg_volume_7d=100_000,
        spike_ratio=10.0,
        market_cap=1_000_000,
        price=0.01,
        detected_at=datetime.now(timezone.utc),
    )
    await trade_volume_spikes(FakeEngine(), sd, [spike], settings)
    assert (
        captured.get("symbol") == "REAL"
    ), f"trade_volume_spikes must pass symbol; got {captured!r}"
    assert (
        captured.get("name") == "Real Coin"
    ), f"trade_volume_spikes must pass name; got {captured!r}"
    await sd.close()


# ---------------------------------------------------------------------------
# Task 4: trade_predictions wires symbol+name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trade_predictions_passes_symbol_and_name_to_engine(tmp_path):
    """T4 — same fix shape as T3 but for narrative_prediction path.
    NarrativePrediction Pydantic model has symbol+name; dispatcher
    was discarding them."""
    from scout.config import Settings
    from scout.db import Database
    from scout.narrative.models import NarrativePrediction
    from scout.trading.signals import trade_predictions

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await sd._conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(coin_id, current_price, market_cap, updated_at) "
        "VALUES ('real-coin', 0.01, 10000000, ?)",
        (now,),
    )
    await sd._conn.commit()
    settings = _test_settings()
    captured = []

    class FakeEngine:
        async def open_trade(self, **kwargs):
            captured.append(kwargs)
            return 1

    pred = NarrativePrediction(
        category_id="ai",
        category_name="AI Tokens",
        coin_id="real-coin",
        symbol="REAL",
        name="Real Coin",
        market_cap_at_prediction=10_000_000,
        price_at_prediction=0.01,
        narrative_fit_score=80,
        staying_power="high",
        confidence="high",
        reasoning="x",
        market_regime="bull",
        trigger_count=3,
        strategy_snapshot={},
        predicted_at=datetime.now(timezone.utc),
    )
    await trade_predictions(
        FakeEngine(),
        sd,
        [pred],
        min_mcap=1_000_000,
        max_mcap=None,
        min_fit_score=1,
        settings=settings,
    )
    assert captured, "trade_predictions did not call open_trade"
    assert captured[0].get("symbol") == "REAL", f"got {captured[0]!r}"
    assert captured[0].get("name") == "Real Coin", f"got {captured[0]!r}"
    await sd.close()


# ---------------------------------------------------------------------------
# Task 5: Database.lookup_symbol_name_by_coin_id resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_symbol_name_prefers_gainers_snapshots(tmp_path):
    """T5 — Database.lookup_symbol_name_by_coin_id picks gainers_snapshots
    first (most authoritative source per architecture-review #4)."""
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await sd._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, price_change_24h, market_cap, "
        " price_at_snapshot, snapshot_at) "
        "VALUES ('chain-coin', 'CHAIN', 'Chain Token', 12.0, 5000000, 0.05, ?)",
        (now,),
    )
    await sd._conn.commit()
    symbol, name = await sd.lookup_symbol_name_by_coin_id("chain-coin")
    assert symbol == "CHAIN"
    assert name == "Chain Token"
    await sd.close()


@pytest.mark.asyncio
async def test_lookup_symbol_name_falls_through_to_volume_history_cg(tmp_path):
    """T5b — when gainers_snapshots has no row, falls through to
    volume_history_cg. Validates the sequential prioritized lookup chain."""
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await sd._conn.execute(
        "INSERT INTO volume_history_cg "
        "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
        "VALUES ('only-vh-coin', 'ONLYVH', 'Only VolHist Coin', 1000, 100, 1.0, ?)",
        (now,),
    )
    await sd._conn.commit()
    symbol, name = await sd.lookup_symbol_name_by_coin_id("only-vh-coin")
    assert symbol == "ONLYVH"
    assert name == "Only VolHist Coin"
    await sd.close()


@pytest.mark.asyncio
async def test_lookup_symbol_name_returns_empty_when_no_source_has_row(tmp_path):
    """T5c — orphan coin (no row in any snapshot table) returns ('', '')
    so caller can decide to log + still proceed with the trade."""
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    symbol, name = await sd.lookup_symbol_name_by_coin_id("orphan-coin")
    assert symbol == ""
    assert name == ""
    await sd.close()


@pytest.mark.asyncio
async def test_lookup_symbol_name_skips_null_symbol_in_source(tmp_path):
    """T5d — snapshot row exists but symbol IS NULL (legacy / partial
    data). Helper's `if row and row[0] and row[1]` filter must skip and
    try next table. Here volume_history_cg has the clean row that the
    helper should return."""
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await sd._conn.execute(
        "INSERT INTO volume_history_cg "
        "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
        "VALUES ('partial-coin', 'PART', 'Partial Coin', 1000, 100, 1.0, ?)",
        (now,),
    )
    await sd._conn.commit()
    symbol, name = await sd.lookup_symbol_name_by_coin_id("partial-coin")
    assert symbol == "PART"
    assert name == "Partial Coin"
    await sd.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_coin_id", ["", None, 0, False])
async def test_lookup_symbol_name_handles_empty_or_none_coin_id(tmp_path, bad_coin_id):
    """T5e — F16 mitigation + ad38b9 S2: extend parametrize to all
    Python falsy. Defensive guard at top of helper for empty/None/0/False
    coin_id (caller-side bug). Should return ("","") AND log
    `lookup_symbol_name_called_with_empty_coin_id` (SF-1 fix)."""
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    with capture_logs() as logs:
        symbol, name = await sd.lookup_symbol_name_by_coin_id(bad_coin_id)
    assert symbol == ""
    assert name == ""
    # SF-1: caller-bug breadcrumb is mandatory
    assert any(
        e.get("event") == "lookup_symbol_name_called_with_empty_coin_id" for e in logs
    ), f"SF-1 caller-bug breadcrumb missing; got: {[e.get('event') for e in logs]}"
    await sd.close()


@pytest.mark.asyncio
async def test_chain_completed_orphan_does_not_trigger_engine_warning(tmp_path):
    """T5f'' — MF-2 fix (PR #67 silent-failure-hunter): when chain
    resolver returns ("", ""), dispatcher passes
    expected_empty_metadata=True so the engine WARNING + INFO events
    DO NOT fire. Otherwise every chain orphan triple-fires events
    (chain_completed_no_metadata + WARNING + INFO), making
    Self-Review #8's "14d zero trade_metadata_empty events" soak
    criterion structurally unreachable."""
    from scout.config import Settings
    from scout.db import Database
    from scout.trading.engine import TradingEngine
    from scout.trading.signals import trade_chain_completions

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await sd._conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(coin_id, current_price, market_cap, updated_at) "
        "VALUES ('orphan-coin', 0.05, 5000000, ?)",
        (now,),
    )
    await sd._conn.execute(
        "INSERT INTO chain_patterns (id, name, description, steps_json, "
        " min_steps_to_trigger, conviction_boost, alert_priority, is_active) "
        "VALUES (3, 'full_conviction', 'narrative full conviction', '[]', 1, 1, 'low', 1)",
    )
    await sd._conn.execute(
        "INSERT INTO chain_matches "
        "(token_id, pipeline, pattern_id, pattern_name, "
        " steps_matched, total_steps, anchor_time, completed_at, "
        " chain_duration_hours, conviction_boost, created_at) "
        "VALUES ('orphan-coin', 'narrative', 3, 'full_conviction', "
        " 3, 3, ?, ?, 4.0, 1, ?)",
        (now, now, now),
    )
    await sd._conn.commit()
    settings = _test_settings()
    # Use REAL TradingEngine so engine WARNING actually fires when called
    # without the sentinel — this is the bug we're guarding against.
    engine = TradingEngine("paper", sd, settings)
    with capture_logs() as logs:
        await trade_chain_completions(engine, sd, settings=settings)
    events = [e.get("event") for e in logs]
    # Dispatcher's WARNING fires (visibility for orphan rate)
    assert (
        "chain_completed_no_metadata" in events
    ), f"chain_completed_no_metadata did not fire; got: {events}"
    # Engine WARNING + INFO MUST NOT fire — they're suppressed by
    # expected_empty_metadata=True
    assert "open_trade_called_with_empty_symbol_and_name" not in events, (
        f"MF-2 regression: engine WARNING fired for known-orphan chain; "
        f"got events: {events}"
    )
    assert "trade_metadata_empty" not in events, (
        f"MF-2 regression: engine INFO fired for known-orphan chain; "
        f"got events: {events}"
    )
    await sd.close()


@pytest.mark.asyncio
async def test_open_trade_with_expected_empty_metadata_suppresses_warnings(tmp_path):
    """T2c — MF-2 sentinel direct test: open_trade with
    expected_empty_metadata=True + empty symbol+name MUST NOT log the
    WARNING or INFO events. With the sentinel False (default), the
    same call DOES log both. Pins the kwarg semantics."""
    from scout.config import Settings
    from scout.db import Database
    from scout.trading.engine import TradingEngine

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    settings = _test_settings()
    engine = TradingEngine("paper", sd, settings)

    # Sentinel ON: WARNING + INFO MUST NOT fire
    with capture_logs() as logs_on:
        await engine.open_trade(
            token_id="known-orphan",
            symbol="",
            name="",
            signal_type="chain_completed",
            signal_data={"foo": "bar"},
            signal_combo="cc|none",
            entry_price=0.001,
            expected_empty_metadata=True,
        )
    events_on = [e.get("event") for e in logs_on]
    assert (
        "open_trade_called_with_empty_symbol_and_name" not in events_on
    ), f"sentinel did not suppress WARNING; got: {events_on}"
    assert (
        "trade_metadata_empty" not in events_on
    ), f"sentinel did not suppress INFO; got: {events_on}"

    # Sentinel OFF (default): WARNING + INFO MUST fire (regression check)
    with capture_logs() as logs_off:
        await engine.open_trade(
            token_id="caller-drift",
            symbol="",
            name="",
            signal_type="volume_spike",
            signal_data={"foo": "bar"},
            signal_combo="vs|none",
            entry_price=0.001,
        )
    events_off = [e.get("event") for e in logs_off]
    assert (
        "open_trade_called_with_empty_symbol_and_name" in events_off
    ), f"sentinel False did not log WARNING; got: {events_off}"
    assert (
        "trade_metadata_empty" in events_off
    ), f"sentinel False did not log INFO; got: {events_off}"
    await sd.close()


@pytest.mark.asyncio
async def test_lookup_symbol_name_logs_breadcrumb_on_operational_error(
    tmp_path, monkeypatch
):
    """T5h — MF-1 fix (PR #67 silent-failure-hunter): per-table
    OperationalError swallow MUST emit a debug breadcrumb so
    connection-drop / lock signature is greppable. Without the log,
    operator can't distinguish F6 orphans from F3/F17 infra failures."""
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    real_execute = sd._conn.execute

    async def fail_first(*args, **kwargs):
        # First call raises OperationalError (gainers_snapshots);
        # subsequent calls work normally.
        if "gainers_snapshots" in args[0]:
            raise aiosqlite.OperationalError("simulated lock")
        return await real_execute(*args, **kwargs)

    monkeypatch.setattr(sd._conn, "execute", fail_first)
    with capture_logs() as logs:
        symbol, name = await sd.lookup_symbol_name_by_coin_id("test-coin")
    # Helper falls through to volume_history_cg + volume_spikes (both
    # empty in this test) → returns ("", "")
    assert symbol == ""
    assert name == ""
    # MF-1: the swallow MUST be visible
    assert any(
        e.get("event") == "lookup_symbol_name_table_unavailable"
        and e.get("table") == "gainers_snapshots"
        for e in logs
    ), f"MF-1 breadcrumb missing; got: {[e.get('event') for e in logs]}"
    await sd.close()


@pytest.mark.asyncio
async def test_trade_chain_completions_uses_lookup_helper_for_metadata(tmp_path):
    """T5f — trade_chain_completions calls Database.lookup_symbol_name_by_coin_id
    and passes the result through to engine.open_trade.

    M3 fix: chain_matches schema requires steps_matched, total_steps,
    anchor_time, chain_duration_hours, conviction_boost (all NOT NULL).
    chain_patterns FK on pattern_id — seed pattern row first."""
    from scout.config import Settings
    from scout.db import Database
    from scout.trading.signals import trade_chain_completions

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await sd._conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(coin_id, current_price, market_cap, updated_at) "
        "VALUES ('chain-coin', 0.05, 5000000, ?)",
        (now,),
    )
    await sd._conn.execute(
        "INSERT INTO chain_patterns (id, name, description, steps_json, "
        " min_steps_to_trigger, conviction_boost, alert_priority, is_active) "
        "VALUES (1, 'full_conviction', 'narrative full conviction', '[]', 1, 1, 'low', 1)",
    )
    await sd._conn.execute(
        "INSERT INTO chain_matches "
        "(token_id, pipeline, pattern_id, pattern_name, "
        " steps_matched, total_steps, anchor_time, completed_at, "
        " chain_duration_hours, conviction_boost, created_at) "
        "VALUES ('chain-coin', 'narrative', 1, 'full_conviction', "
        " 3, 3, ?, ?, 4.0, 1, ?)",
        (now, now, now),
    )
    await sd._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, price_change_24h, market_cap, "
        " price_at_snapshot, snapshot_at) "
        "VALUES ('chain-coin', 'CHAIN', 'Chain Token', 12.0, 5000000, 0.05, ?)",
        (now,),
    )
    await sd._conn.commit()
    settings = _test_settings()
    captured = []

    class FakeEngine:
        async def open_trade(self, **kwargs):
            captured.append(kwargs)
            return 1

    await trade_chain_completions(FakeEngine(), sd, settings=settings)
    assert captured, "trade_chain_completions did not call open_trade"
    assert captured[0].get("symbol") == "CHAIN", f"got {captured[0]!r}"
    assert captured[0].get("name") == "Chain Token", f"got {captured[0]!r}"
    await sd.close()


@pytest.mark.asyncio
async def test_trade_chain_completions_falls_back_to_empty_when_no_snapshot(tmp_path):
    """T5f' — orphan chain coin (no row in any snapshot table). Helper
    returns ('', ''), dispatcher logs `chain_completed_no_metadata`,
    AND open_trade still fires (the trade is real; we just lack metadata).
    Engine-level WARNING from Task 2 ALSO fires (defense-in-depth)."""
    from scout.config import Settings
    from scout.db import Database
    from scout.trading.signals import trade_chain_completions

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await sd._conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(coin_id, current_price, market_cap, updated_at) "
        "VALUES ('orphan-coin', 0.05, 5000000, ?)",
        (now,),
    )
    await sd._conn.execute(
        "INSERT INTO chain_patterns (id, name, description, steps_json, "
        " min_steps_to_trigger, conviction_boost, alert_priority, is_active) "
        "VALUES (2, 'full_conviction', 'narrative full conviction', '[]', 1, 1, 'low', 1)",
    )
    await sd._conn.execute(
        "INSERT INTO chain_matches "
        "(token_id, pipeline, pattern_id, pattern_name, "
        " steps_matched, total_steps, anchor_time, completed_at, "
        " chain_duration_hours, conviction_boost, created_at) "
        "VALUES ('orphan-coin', 'narrative', 2, 'full_conviction', "
        " 3, 3, ?, ?, 4.0, 1, ?)",
        (now, now, now),
    )
    await sd._conn.commit()
    settings = _test_settings()
    captured = []

    class FakeEngine:
        async def open_trade(self, **kwargs):
            captured.append(kwargs)
            return 1

    with capture_logs() as logs:
        await trade_chain_completions(FakeEngine(), sd, settings=settings)
    assert captured, "open_trade still called even with empty symbol/name"
    assert captured[0].get("symbol") == ""
    assert captured[0].get("name") == ""
    assert any(
        e.get("event") == "chain_completed_no_metadata" for e in logs
    ), f"expected chain_completed_no_metadata; got {[e.get('event') for e in logs]}"
    await sd.close()


@pytest.mark.asyncio
async def test_lookup_symbol_name_propagates_non_operational_errors(
    tmp_path, monkeypatch
):
    """T5g (A11 fix) — pin that the per-table catch is narrow:
    `except aiosqlite.OperationalError` ONLY. Other exception types
    (programming errors, type mismatches) MUST propagate — otherwise
    we hide real bugs behind silent ("","") returns."""
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    real_execute = sd._conn.execute
    call_count = {"n": 0}

    async def boom(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("simulated programming error")
        return await real_execute(*args, **kwargs)

    monkeypatch.setattr(sd._conn, "execute", boom)
    with pytest.raises(ValueError, match="simulated programming error"):
        await sd.lookup_symbol_name_by_coin_id("any-coin")
    await sd.close()
