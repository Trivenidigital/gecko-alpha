"""Dispatcher-layer suppressed blocks -> signal_outcome_ledger (recall lane).

Edge audit 2026-07-02 (tasks/gecko-alpha-fable-review_2026_07.md Phase 3): the
engine's GatedOutSampler lane samples ONLY engine-level blocked decisions.
Dispatcher-layer suppression (scout/trading/signals.py should_open ->
reason='suppressed') is a DIFFERENT path that never reaches that sampler, yet
it is the dominant winner-killer (12 of 24 >=5x winners). This suite locks in
that every suppressed block is RECORDED AT EMISSION into the ledger's
gated_out_sample lane, tagged source_layer='dispatcher' so analysis can
separate it from engine-level blocks — and that the recording is strictly
additive + fail-soft (never changes or breaks the block itself).

Negative-regression coverage:
- a suppressed dispatch records exactly one ledger row (reason='suppressed',
  source_layer='dispatcher');
- LEDGER_SAMPLE_SUPPRESSED=False records none (LEDGER_ENABLED=False too);
- a ledger failure does NOT break suppression / dispatch (fail-soft);
- existing suppression behavior is unchanged (block still happens; the
  trade_decision_events row is still written; recording is additive).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.spikes.models import VolumeSpike
from scout.trading import signals as signals_mod
from scout.trading.engine import TradingEngine
from scout.trading.signals import trade_gainers, trade_volume_spikes


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def _settings(tmp_path, **overrides):
    base = dict(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        DB_PATH=tmp_path / "test.db",
        TRADING_ENABLED=True,
        TRADING_MODE="paper",
        PAPER_TRADE_AMOUNT_USD=1000.0,
        PAPER_MAX_EXPOSURE_USD=10_000.0,
        PAPER_TP_PCT=20.0,
        PAPER_SL_PCT=10.0,
        PAPER_SLIPPAGE_BPS=50,
        PAPER_MAX_DURATION_HOURS=48,
        PAPER_MIN_MCAP=5_000_000,
        PAPER_MAX_MCAP_RANK=1500,
        PAPER_MAX_OPEN_TRADES=1000,
        PAPER_STARTUP_WARMUP_SECONDS=0,
        LEDGER_ENABLED=True,
        LEDGER_SAMPLE_SUPPRESSED=True,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path):
    return _settings(tmp_path)


@pytest.fixture
def engine(db, settings):
    return TradingEngine(mode="paper", db=db, settings=settings)


async def _insert_gainer(db, coin_id, market_cap, price=1.0):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (coin_id, coin_id.upper(), coin_id, 25.0, market_cap, 100_000.0, price, now),
    )
    await db._conn.commit()


async def _suppress_combo(db, combo_key):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT OR REPLACE INTO combo_performance
           (combo_key, window, trades, wins, losses, total_pnl_usd, avg_pnl_pct,
            win_rate_pct, suppressed, suppressed_at, parole_at,
            parole_trades_remaining, refresh_failures, last_refreshed)
           VALUES (?, '30d', 10, 0, 10, -100, -10, 0, 1, ?, NULL, NULL, 0, ?)""",
        (combo_key, now, now),
    )
    await db._conn.commit()


async def _ledger_rows(db, token_id):
    cur = await db._conn.execute(
        "SELECT kind, token_id, surface, price_at_emission, gate_verdicts, "
        "enrollment_status, label_status "
        "FROM signal_outcome_ledger WHERE token_id = ? ORDER BY id",
        (token_id,),
    )
    return await cur.fetchall()


async def _open_count(db):
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status = 'open'"
    )
    return (await cur.fetchone())[0]


async def _decision_reason_count(db, token_id, reason):
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM trade_decision_events "
        "WHERE token_id = ? AND reason = ?",
        (token_id, reason),
    )
    return (await cur.fetchone())[0]


# --------------------------------------------------------------------------
# 1. A suppressed dispatch records exactly ONE ledger row, tagged as a
#    dispatcher-layer suppressed block.
# --------------------------------------------------------------------------
async def test_suppressed_gainer_records_exactly_one_dispatcher_ledger_row(
    db, engine, settings
):
    await _insert_gainer(db, "supp-gainer", market_cap=10_000_000, price=2.5)
    await _suppress_combo(db, "gainers_early")

    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)

    rows = await _ledger_rows(db, "supp-gainer")
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "gated_out_sample"
    assert row["surface"] == "gainers_early"
    # price carried from the snapshot anchor
    assert row["price_at_emission"] == 2.5
    # gated_out_sample always enrolls the token for forward polling
    assert row["enrollment_status"] == "enrolled"
    assert row["label_status"] == "pending"

    verdicts = json.loads(row["gate_verdicts"])
    assert verdicts["reason"] == "suppressed"
    assert verdicts["source_layer"] == "dispatcher"
    assert verdicts["combo_key"] == "gainers_early"
    # the underlying should_open verdict rides along for per-combo attribution
    assert verdicts["suppression_reason"] == "suppressed"


# --------------------------------------------------------------------------
# 1b. A log-only suppression site (volume_spike previously emitted no dispatch
#     decision at all) now also records into the ledger.
# --------------------------------------------------------------------------
async def test_suppressed_volume_spike_records_dispatcher_ledger_row(
    db, engine, settings
):
    await _suppress_combo(db, "volume_spike")
    spike = VolumeSpike(
        coin_id="supp-vol",
        symbol="SV",
        name="SuppVol",
        current_volume=600_000,
        avg_volume_7d=100_000,
        spike_ratio=6.0,
        market_cap=20_000_000,
        price=1.0,
        detected_at=datetime.now(timezone.utc),
    )

    await trade_volume_spikes(engine, db, [spike], settings)

    rows = await _ledger_rows(db, "supp-vol")
    assert len(rows) == 1
    verdicts = json.loads(rows[0]["gate_verdicts"])
    assert rows[0]["surface"] == "volume_spike"
    assert verdicts["reason"] == "suppressed"
    assert verdicts["source_layer"] == "dispatcher"


# --------------------------------------------------------------------------
# 2. LEDGER_SAMPLE_SUPPRESSED=False records nothing (lane kill switch), and
#    LEDGER_ENABLED=False records nothing (global kill switch).
# --------------------------------------------------------------------------
async def test_flag_off_records_no_ledger_row(db, tmp_path):
    settings = _settings(tmp_path, LEDGER_SAMPLE_SUPPRESSED=False)
    engine = TradingEngine(mode="paper", db=db, settings=settings)
    await _insert_gainer(db, "flag-off", market_cap=10_000_000)
    await _suppress_combo(db, "gainers_early")

    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)

    assert await _ledger_rows(db, "flag-off") == []
    # block still happened
    assert await _open_count(db) == 0


async def test_ledger_disabled_records_no_ledger_row(db, tmp_path):
    settings = _settings(tmp_path, LEDGER_ENABLED=False)
    engine = TradingEngine(mode="paper", db=db, settings=settings)
    await _insert_gainer(db, "ledger-off", market_cap=10_000_000)
    await _suppress_combo(db, "gainers_early")

    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)

    assert await _ledger_rows(db, "ledger-off") == []
    assert await _open_count(db) == 0


# --------------------------------------------------------------------------
# 3. A ledger failure does NOT break suppression / dispatch (fail-soft).
# --------------------------------------------------------------------------
async def test_ledger_failure_does_not_break_suppression(
    db, engine, settings, monkeypatch
):
    async def _boom(*args, **kwargs):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(signals_mod, "_ledger_record_emission", _boom)

    await _insert_gainer(db, "boom-gainer", market_cap=10_000_000)
    await _suppress_combo(db, "gainers_early")

    # Must NOT raise despite the ledger writer blowing up.
    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)

    # Block still happened, and the pre-existing suppression decision event
    # was still written — recording is additive, not load-bearing.
    assert await _open_count(db) == 0
    assert await _decision_reason_count(db, "boom-gainer", "suppressed") == 1
    # No ledger row was written (the writer failed), proving the failure was
    # swallowed rather than partially committed / propagated.
    assert await _ledger_rows(db, "boom-gainer") == []


# --------------------------------------------------------------------------
# 4. Existing suppression behavior unchanged: block still happens AND the
#    prior trade_decision_events row is still written when recording is on.
# --------------------------------------------------------------------------
async def test_suppression_behavior_unchanged_recording_additive(db, engine, settings):
    await _insert_gainer(db, "additive-gainer", market_cap=10_000_000)
    await _suppress_combo(db, "gainers_early")

    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)

    # Pre-existing behavior: no trade opened + suppression decision event.
    assert await _open_count(db) == 0
    assert await _decision_reason_count(db, "additive-gainer", "suppressed") == 1
    # Additive behavior: exactly one ledger row alongside it.
    assert len(await _ledger_rows(db, "additive-gainer")) == 1
