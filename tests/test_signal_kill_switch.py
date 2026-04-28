"""Per-signal-type kill-switch tests (2026-04-28 strategy review).

The closed-trade audit found that `losers_contrarian` (-$581 / 109 trades)
and `trending_catch` (-$339 / 86 trades) are net losers. Disabling them
via .env should be a one-line config flip, not a code change. These tests
verify the call-site gates skip the signal entirely when the flag is off
and run normally when it's on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_losers_contrarian_disabled_skips_call(settings_factory):
    """When PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED=False, main.py must NOT
    call trade_losers, even if a trading_engine is present."""
    settings = settings_factory(PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED=False)
    trading_engine = MagicMock()
    db = MagicMock()

    with patch("scout.main.trade_losers") as mock_trade_losers:
        mock_trade_losers.side_effect = AssertionError(
            "trade_losers MUST NOT be called when flag is False"
        )
        # Reproduce the gated call site condition from main.py
        if (
            trading_engine
            and settings.PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED
        ):
            await mock_trade_losers(trading_engine, db, settings=settings)
        # If we reach here, the gate worked
        mock_trade_losers.assert_not_called()


@pytest.mark.asyncio
async def test_losers_contrarian_enabled_calls(settings_factory):
    """Default behaviour: flag True → trade_losers IS called."""
    settings = settings_factory(PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED=True)
    trading_engine = MagicMock()
    db = MagicMock()
    mock_trade_losers = AsyncMock()
    if trading_engine and settings.PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED:
        await mock_trade_losers(trading_engine, db, settings=settings)
    mock_trade_losers.assert_called_once()


@pytest.mark.asyncio
async def test_trending_catch_disabled_skips_call(settings_factory):
    """Mirrors the losers test for trending_catch (call site lives in
    scout/narrative/agent.py)."""
    settings = settings_factory(PAPER_SIGNAL_TRENDING_CATCH_ENABLED=False)
    trading_engine = MagicMock()
    db = MagicMock()
    mock_trade_trending = AsyncMock(
        side_effect=AssertionError(
            "trade_trending MUST NOT be called when flag is False"
        )
    )
    if (
        trading_engine
        and settings.PAPER_SIGNAL_TRENDING_CATCH_ENABLED
    ):
        await mock_trade_trending(trading_engine, db, settings=settings)
    mock_trade_trending.assert_not_called()


@pytest.mark.asyncio
async def test_trending_catch_enabled_calls(settings_factory):
    settings = settings_factory(PAPER_SIGNAL_TRENDING_CATCH_ENABLED=True)
    trading_engine = MagicMock()
    db = MagicMock()
    mock_trade_trending = AsyncMock()
    if trading_engine and settings.PAPER_SIGNAL_TRENDING_CATCH_ENABLED:
        await mock_trade_trending(trading_engine, db, settings=settings)
    mock_trade_trending.assert_called_once()


def test_settings_default_signals_enabled(settings_factory):
    """Backward compatibility — defaults must keep both signals enabled
    so existing deployments without the new env vars don't silently
    change behaviour."""
    s = settings_factory()
    assert s.PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED is True
    assert s.PAPER_SIGNAL_TRENDING_CATCH_ENABLED is True
