import json
from datetime import datetime, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.paper import PaperTrader


def _settings(**overrides):
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="x",
        TELEGRAM_CHAT_ID="x",
        ANTHROPIC_API_KEY="x",
        **overrides,
    )


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "paper_actionability.db")
    await d.initialize()
    yield d
    await d.close()


async def _open(trader, db, *, signal_type, signal_data, signal_combo=None):
    trade_id = await trader.execute_buy(
        db=db,
        token_id=f"{signal_type}-tok",
        symbol="TOK",
        name="Token",
        chain="coingecko",
        signal_type=signal_type,
        signal_data=signal_data,
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo=signal_combo or signal_type,
        settings=_settings(),
    )
    cur = await db._conn.execute(
        "SELECT actionable, actionability_reason, actionability_version "
        "FROM paper_trades WHERE id=?",
        (trade_id,),
    )
    return await cur.fetchone()


@pytest.mark.asyncio
async def test_execute_buy_stamps_actionable_true_for_narrative_prediction(db):
    row = await _open(
        PaperTrader(),
        db,
        signal_type="narrative_prediction",
        signal_data={"mcap": 20_000_000},
    )
    assert row["actionable"] == 1
    assert row["actionability_reason"] == "v1_pass_core_signal_mcap_10_50m"
    assert row["actionability_version"] == "v1"


@pytest.mark.asyncio
async def test_execute_buy_stamps_actionable_false_for_gainers_early_5_10m(db):
    row = await _open(
        PaperTrader(),
        db,
        signal_type="gainers_early",
        signal_data={"mcap": 7_000_000},
    )
    assert row["actionable"] == 0
    assert row["actionability_reason"] == "v1_block_gainers_early_mcap_5_10m"
    assert row["actionability_version"] == "v1"


@pytest.mark.asyncio
async def test_execute_buy_without_settings_still_classifies_actionability(db):
    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id="compat",
        symbol="CMP",
        name="Compat",
        chain="coingecko",
        signal_type="narrative_prediction",
        signal_data={"mcap": 20_000_000},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="narrative_prediction",
    )
    cur = await db._conn.execute(
        "SELECT actionable, actionability_reason, actionability_version "
        "FROM paper_trades WHERE id=?",
        (trade_id,),
    )
    row = await cur.fetchone()
    assert row["actionable"] == 1
    assert row["actionability_reason"] == "v1_pass_core_signal_mcap_10_50m"
    assert row["actionability_version"] == "v1"


@pytest.mark.asyncio
async def test_execute_buy_without_settings_still_classifies_long_hold_non_actionable(
    db,
):
    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id="long-hold",
        symbol="LH",
        name="Long Hold",
        chain="coingecko",
        signal_type="long_hold",
        signal_data={"mcap": 20_000_000},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="long_hold",
    )
    cur = await db._conn.execute(
        "SELECT actionable, actionability_reason, actionability_version "
        "FROM paper_trades WHERE id=?",
        (trade_id,),
    )
    row = await cur.fetchone()
    assert row["actionable"] == 0
    assert row["actionability_reason"] == "v1_block_unknown_signal_type"
    assert row["actionability_version"] == "v1"


@pytest.mark.asyncio
async def test_actionability_enrichment_does_not_mutate_persisted_signal_data(db):
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(coin_id, current_price, market_cap, updated_at) VALUES (?, ?, ?, ?)",
        ("immut", 1.0, 20_000_000, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()
    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id="immut",
        symbol="IMM",
        name="Immutable",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="volume_spike",
    )
    cur = await db._conn.execute(
        "SELECT signal_data, actionable FROM paper_trades WHERE id=?",
        (trade_id,),
    )
    row = await cur.fetchone()
    assert json.loads(row["signal_data"]) == {"spike_ratio": 12.3}
    assert row["actionable"] == 1


@pytest.mark.asyncio
async def test_gainers_early_stack_failure_fails_closed_but_opens(db, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("forced stack failure")

    monkeypatch.setattr("scout.trading.paper.compute_stack", boom)
    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id="stack-fail",
        symbol="SF",
        name="Stack Fail",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={"mcap": 80_000_000},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="gainers_early",
    )
    assert trade_id is not None
    cur = await db._conn.execute(
        "SELECT actionable, actionability_reason FROM paper_trades WHERE id=?",
        (trade_id,),
    )
    row = await cur.fetchone()
    assert row["actionable"] == 0
    assert row["actionability_reason"] == "v1_block_gainers_early_stack_unavailable"
