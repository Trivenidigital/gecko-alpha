"""Tests for scout.config module."""

from pathlib import Path

import pytest

from scout.config import Settings


def test_settings_loads_defaults():
    s = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        TELEGRAM_CHAT_ID="test-chat",
        ANTHROPIC_API_KEY="test-key",
    )
    assert s.SCAN_INTERVAL_SECONDS == 60
    assert s.MIN_SCORE == 60
    assert s.CONVICTION_THRESHOLD == 70
    assert s.QUANT_WEIGHT == 0.6
    assert s.NARRATIVE_WEIGHT == 0.4
    assert s.MIN_MARKET_CAP == 10_000
    assert s.MAX_MARKET_CAP == 500_000
    assert s.MAX_TOKEN_AGE_DAYS == 7
    assert s.MIN_VOL_LIQ_RATIO == 5.0
    assert s.CHAINS == ["solana", "base", "ethereum"]
    assert s.MIROFISH_URL == "http://localhost:5001"
    assert s.MIROFISH_TIMEOUT_SEC == 180
    assert s.MAX_MIROFISH_JOBS_PER_DAY == 50
    assert s.DB_PATH == Path("scout.db")
    assert isinstance(s.DB_PATH, Path)
    assert s.HELIUS_API_KEY == ""
    assert s.MORALIS_API_KEY == ""
    assert s.DISCORD_WEBHOOK_URL == ""


def test_settings_chains_parsing_from_string():
    s = Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        CHAINS="solana,polygon",
    )
    assert s.CHAINS == ["solana", "polygon"]


def test_settings_chains_parsing_from_list():
    s = Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        CHAINS=["base", "ethereum"],
    )
    assert s.CHAINS == ["base", "ethereum"]


def test_settings_custom_overrides():
    s = Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        MIN_SCORE=40,
        CONVICTION_THRESHOLD=80,
        SCAN_INTERVAL_SECONDS=30,
        MAX_MIROFISH_JOBS_PER_DAY=100,
    )
    assert s.MIN_SCORE == 40
    assert s.CONVICTION_THRESHOLD == 80
    assert s.SCAN_INTERVAL_SECONDS == 30
    assert s.MAX_MIROFISH_JOBS_PER_DAY == 100


def test_coingecko_config_defaults():
    """CoinGecko config knobs have correct defaults."""
    s = Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    assert s.MOMENTUM_RATIO_THRESHOLD == 0.6
    assert s.MIN_VOL_ACCEL_RATIO == 5.0


def test_settings_weight_sum_validation():
    with pytest.raises(ValueError, match="must sum to 1.0"):
        Settings(
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            QUANT_WEIGHT=0.7,
            NARRATIVE_WEIGHT=0.4,
        )
