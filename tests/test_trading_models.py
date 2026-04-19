"""Tests for paper trading Pydantic models."""

from datetime import datetime, timezone

import pytest

from scout.trading.models import PaperTrade, TradeSummary


def test_paper_trade_required_fields():
    now = datetime.now(timezone.utc)
    trade = PaperTrade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        entry_price=50000.0,
        amount_usd=1000.0,
        quantity=0.02,
        tp_pct=20.0,
        sl_pct=10.0,
        tp_price=60000.0,
        sl_price=45000.0,
        opened_at=now,
    )
    assert trade.id is None
    assert trade.status == "open"
    assert trade.exit_price is None
    assert trade.pnl_usd is None
    assert trade.peak_price is None


def test_paper_trade_all_checkpoints_nullable():
    now = datetime.now(timezone.utc)
    trade = PaperTrade(
        token_id="ethereum",
        symbol="ETH",
        name="Ethereum",
        chain="coingecko",
        signal_type="narrative_prediction",
        signal_data={"fit": 85},
        entry_price=3000.0,
        amount_usd=1000.0,
        quantity=0.333,
        tp_pct=20.0,
        sl_pct=10.0,
        tp_price=3600.0,
        sl_price=2700.0,
        opened_at=now,
    )
    assert trade.checkpoint_1h_price is None
    assert trade.checkpoint_6h_price is None
    assert trade.checkpoint_24h_price is None
    assert trade.checkpoint_48h_price is None


def test_paper_trade_closed_state():
    now = datetime.now(timezone.utc)
    trade = PaperTrade(
        token_id="solana",
        symbol="SOL",
        name="Solana",
        chain="coingecko",
        signal_type="trending_catch",
        signal_data={"trending_rank": 3},
        entry_price=100.0,
        amount_usd=1000.0,
        quantity=10.0,
        tp_pct=20.0,
        sl_pct=10.0,
        tp_price=120.0,
        sl_price=90.0,
        status="closed_tp",
        exit_price=121.0,
        exit_reason="take_profit",
        pnl_usd=210.0,
        pnl_pct=21.0,
        opened_at=now,
        closed_at=now,
    )
    assert trade.status == "closed_tp"
    assert trade.pnl_usd == 210.0


def test_sl_pct_must_be_positive():
    """sl_pct uses positive convention: 10.0 means 10% stop loss."""
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="sl_pct must be positive"):
        PaperTrade(
            token_id="bitcoin",
            symbol="BTC",
            name="Bitcoin",
            chain="coingecko",
            signal_type="volume_spike",
            signal_data={},
            entry_price=50000.0,
            amount_usd=1000.0,
            quantity=0.02,
            tp_pct=20.0,
            sl_pct=-10.0,
            tp_price=60000.0,
            sl_price=45000.0,
            opened_at=now,
        )


def test_tp_pct_must_be_positive():
    """tp_pct must be positive."""
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="tp_pct must be positive"):
        PaperTrade(
            token_id="bitcoin",
            symbol="BTC",
            name="Bitcoin",
            chain="coingecko",
            signal_type="volume_spike",
            signal_data={},
            entry_price=50000.0,
            amount_usd=1000.0,
            quantity=0.02,
            tp_pct=-20.0,
            sl_pct=10.0,
            tp_price=60000.0,
            sl_price=45000.0,
            opened_at=now,
        )


def test_trade_summary_required_fields():
    summary = TradeSummary(
        date="2026-04-19",
        trades_opened=12,
        trades_closed=8,
        wins=5,
        losses=3,
        total_pnl_usd=340.0,
        best_trade_pnl=450.0,
        worst_trade_pnl=-120.0,
        avg_pnl_pct=4.25,
        win_rate_pct=62.5,
        by_signal_type={
            "volume_spike": {"trades": 5, "pnl": 230, "win_rate": 65},
        },
    )
    assert summary.trades_opened == 12
    assert summary.by_signal_type["volume_spike"]["pnl"] == 230


def test_config_paper_sl_pct_positive():
    """PAPER_SL_PCT in Settings must reject negative values."""
    from scout.config import Settings

    with pytest.raises(ValueError, match="sl_pct must be positive"):
        Settings(
            TELEGRAM_BOT_TOKEN="test",
            TELEGRAM_CHAT_ID="test",
            ANTHROPIC_API_KEY="test",
            PAPER_SL_PCT=-5.0,
        )


def test_config_paper_tp_pct_positive():
    """PAPER_TP_PCT in Settings must reject negative values."""
    from scout.config import Settings

    with pytest.raises(ValueError, match="tp_pct must be positive"):
        Settings(
            TELEGRAM_BOT_TOKEN="test",
            TELEGRAM_CHAT_ID="test",
            ANTHROPIC_API_KEY="test",
            PAPER_TP_PCT=-5.0,
        )


def test_config_paper_slippage_bps_default():
    """PAPER_SLIPPAGE_BPS defaults to 50."""
    from scout.config import Settings

    s = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
    )
    assert s.PAPER_SLIPPAGE_BPS == 50
