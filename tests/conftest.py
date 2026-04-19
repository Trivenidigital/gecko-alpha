"""Shared test fixtures for CoinPump Scout."""

import pytest

from scout.config import Settings
from scout.models import CandidateToken


@pytest.fixture
def settings_factory():
    def _make(**overrides):
        defaults = dict(
            TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k"
        )
        defaults.update(overrides)
        return Settings(**defaults)

    return _make


@pytest.fixture
def token_factory():
    def _make(**overrides):
        defaults = dict(
            contract_address="0xtest",
            chain="solana",
            token_name="Test",
            ticker="TST",
            token_age_days=1.0,
            market_cap_usd=50000.0,
            liquidity_usd=10000.0,
            volume_24h_usd=80000.0,
            holder_count=100,
            holder_growth_1h=25,
        )
        defaults.update(overrides)
        return CandidateToken(**defaults)

    return _make
