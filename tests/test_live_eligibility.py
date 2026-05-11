"""BL-NEW-LIVE-ELIGIBLE: tier rules + would_be_live writer tests."""

from __future__ import annotations

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.live_eligibility import (
    compute_would_be_live,
    matches_tier_1_or_2,
)
from scout.trading.paper import PaperTrader

_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "x",
    "ANTHROPIC_API_KEY": "x",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


# ---------- tier rule matcher (pure function, no DB) ----------


def test_tier1a_chain_completed_always_passes():
    """Tier 1a: signal_type='chain_completed' (any pattern) → True."""
    assert matches_tier_1_or_2(
        "chain_completed", {"pattern": "volume_breakout"}, 0, _settings()
    )
    assert matches_tier_1_or_2(
        "chain_completed", {"pattern": "full_conviction"}, 0, _settings()
    )


def test_tier1b_stack_3_passes_any_signal_type():
    """Tier 1b: conviction_stack >= 3 → True regardless of signal_type."""
    assert matches_tier_1_or_2("first_signal", {}, 3, _settings())
    assert matches_tier_1_or_2("trending_catch", {}, 5, _settings())


def test_tier1b_stack_2_does_not_pass():
    """conviction_stack=2 alone is NOT enough."""
    assert not matches_tier_1_or_2("first_signal", {}, 2, _settings())


def test_tier2a_volume_spike_passes():
    """Tier 2a: signal_type='volume_spike' → True."""
    assert matches_tier_1_or_2("volume_spike", {"spike_ratio": 5.1}, 0, _settings())


def test_tier2b_gainers_early_passes_at_thresholds():
    """Tier 2b: gainers_early with mcap≥10M AND 24h≥25% → True."""
    assert matches_tier_1_or_2(
        "gainers_early",
        {"mcap": 15_000_000, "price_change_24h": 30.0},
        0,
        _settings(),
    )


def test_tier2b_gainers_early_fails_low_mcap():
    """gainers_early with mcap<10M → False."""
    assert not matches_tier_1_or_2(
        "gainers_early",
        {"mcap": 5_000_000, "price_change_24h": 30.0},
        0,
        _settings(),
    )


def test_tier2b_gainers_early_fails_low_24h():
    """gainers_early with 24h<25% → False."""
    assert not matches_tier_1_or_2(
        "gainers_early",
        {"mcap": 15_000_000, "price_change_24h": 20.0},
        0,
        _settings(),
    )


def test_tier2b_gainers_early_handles_missing_keys():
    """Missing mcap or 24h → False, no crash."""
    assert not matches_tier_1_or_2("gainers_early", {}, 0, _settings())
    assert not matches_tier_1_or_2("gainers_early", {"mcap": None}, 0, _settings())


def test_other_signals_fail_without_stack():
    """first_signal, trending_catch, narrative_prediction, losers_contrarian,
    long_hold, tg_social all return False without stack ≥ 3."""
    for s in (
        "first_signal",
        "trending_catch",
        "narrative_prediction",
        "losers_contrarian",
        "long_hold",
        "tg_social",
    ):
        assert not matches_tier_1_or_2(s, {}, 0, _settings()), f"{s} should fail"
        assert not matches_tier_1_or_2(s, {}, 2, _settings()), f"{s} stack=2"


def test_tier_threshold_overrides_via_settings():
    """PAPER_TIER2_GAINERS_MIN_MCAP_USD and _24H_PCT are tunable via env."""
    s = _settings(
        PAPER_TIER2_GAINERS_MIN_MCAP_USD=50_000_000.0,
        PAPER_TIER2_GAINERS_MIN_24H_PCT=40.0,
    )
    assert not matches_tier_1_or_2(
        "gainers_early", {"mcap": 15_000_000, "price_change_24h": 30.0}, 0, s
    )
    assert matches_tier_1_or_2(
        "gainers_early", {"mcap": 60_000_000, "price_change_24h": 45.0}, 0, s
    )


# ---------- compute_would_be_live (with DB + cap) ----------


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "live_elig.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_compute_returns_0_for_non_tier_signal(db):
    """Signal that doesn't match tier rules → 0, regardless of slot availability."""
    result = await compute_would_be_live(
        db,
        signal_type="first_signal",
        signal_data={"quant_score": 10},
        conviction_stack=0,
        settings=_settings(),
    )
    assert result == 0


@pytest.mark.asyncio
async def test_compute_returns_1_for_tier1_when_under_cap(db):
    """chain_completed with 0 open trades → 1."""
    result = await compute_would_be_live(
        db,
        signal_type="chain_completed",
        signal_data={"pattern": "volume_breakout"},
        conviction_stack=0,
        settings=_settings(),
    )
    assert result == 1


@pytest.mark.asyncio
async def test_compute_returns_0_when_cap_full(db):
    """Tier-1/2 signal but PAPER_LIVE_ELIGIBLE_SLOTS=2 already open → 0."""
    # Pre-insert 2 open would_be_live=1 rows.
    for tid in (1, 2):
        await db._conn.execute(
            "INSERT INTO paper_trades (id, token_id, symbol, name, chain, "
            "signal_type, signal_data, entry_price, amount_usd, quantity, "
            "tp_pct, sl_pct, tp_price, sl_price, status, opened_at, "
            "would_be_live) "
            "VALUES (?, ?, 'T', 'T', 'cg', 'chain_completed', '{}', "
            "1.0, 100.0, 100.0, 20.0, 15.0, 1.2, 0.85, 'open', '2026-05-11T00:00Z', 1)",
            (tid, f"tok-{tid}"),
        )
    await db._conn.commit()

    result = await compute_would_be_live(
        db,
        signal_type="volume_spike",
        signal_data={"spike_ratio": 6.0},
        conviction_stack=0,
        settings=_settings(PAPER_LIVE_ELIGIBLE_SLOTS=2),
    )
    assert result == 0


@pytest.mark.asyncio
async def test_compute_ignores_closed_trades_in_cap(db):
    """would_be_live=1 rows that are CLOSED don't count toward the cap."""
    await db._conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
        "signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct, "
        "tp_price, sl_price, status, opened_at, would_be_live) "
        "VALUES ('closed-tok', 'T', 'T', 'cg', 'chain_completed', '{}', "
        "1.0, 100.0, 100.0, 20.0, 15.0, 1.2, 0.85, 'closed_tp', '2026-05-11T00:00Z', 1)",
    )
    await db._conn.commit()

    result = await compute_would_be_live(
        db,
        signal_type="volume_spike",
        signal_data={"spike_ratio": 6.0},
        conviction_stack=0,
        settings=_settings(PAPER_LIVE_ELIGIBLE_SLOTS=1),
    )
    assert result == 1, "closed would_be_live=1 must not occupy a live slot"


@pytest.mark.asyncio
async def test_compute_handles_db_error_gracefully(db):
    """If the count query somehow fails, return 0 — never block paper open."""
    await db.close()  # force db._conn to be effectively unusable
    result = await compute_would_be_live(
        db,
        signal_type="chain_completed",
        signal_data={},
        conviction_stack=0,
        settings=_settings(),
    )
    assert result == 0


# ---------- end-to-end: PaperTrader stamps would_be_live ----------


@pytest.fixture
def trader():
    return PaperTrader()


@pytest.mark.asyncio
async def test_execute_buy_stamps_would_be_live_for_chain_completed(db, trader):
    """End-to-end: chain_completed signal → would_be_live=1 on the row."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="memecoin1",
        symbol="MEME",
        name="Meme",
        chain="coingecko",
        signal_type="chain_completed",
        signal_data={"pattern": "volume_breakout"},
        current_price=0.01,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=15.0,
        signal_combo="chain_completed",
        settings=_settings(),
    )
    cur = await db._conn.execute(
        "SELECT would_be_live FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_execute_buy_stamps_zero_for_non_tier_signal(db, trader):
    """End-to-end: first_signal → would_be_live=0."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="micro1",
        symbol="MIC",
        name="Micro",
        chain="coingecko",
        signal_type="first_signal",
        signal_data={"quant_score": 10},
        current_price=0.01,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=15.0,
        signal_combo="first_signal",
        settings=_settings(),
    )
    cur = await db._conn.execute(
        "SELECT would_be_live FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_execute_buy_without_settings_stamps_zero(db, trader):
    """Backwards compatibility: callers that don't pass settings (old tests)
    get would_be_live=0, paper open succeeds normally."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="back-compat",
        symbol="BC",
        name="BC",
        chain="coingecko",
        signal_type="chain_completed",
        signal_data={"pattern": "x"},
        current_price=1.0,
        amount_usd=100.0,
        tp_pct=20.0,
        sl_pct=15.0,
        signal_combo="chain_completed",
        # settings intentionally omitted
    )
    assert trade_id is not None
    cur = await db._conn.execute(
        "SELECT would_be_live FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0] == 0
