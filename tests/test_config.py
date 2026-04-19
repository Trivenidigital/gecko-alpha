"""Tests for scout.config module."""

from pathlib import Path

import pytest

from scout.config import Settings


def test_settings_loads_defaults():
    s = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        TELEGRAM_CHAT_ID="test-chat",
        ANTHROPIC_API_KEY="test-key",
        HELIUS_API_KEY="",
        _env_file=None,
    )
    assert s.SCAN_INTERVAL_SECONDS == 60
    assert s.MIN_SCORE == 60
    assert s.CONVICTION_THRESHOLD == 70
    assert s.QUANT_WEIGHT == 0.6
    assert s.NARRATIVE_WEIGHT == 0.4
    assert s.MIN_MARKET_CAP == 10_000
    assert s.MAX_MARKET_CAP == 500_000
    assert s.MAX_TOKEN_AGE_DAYS == 7
    assert s.MIN_LIQUIDITY_USD == 15_000
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


def test_feedback_loop_defaults(monkeypatch):
    """All feedback-loop settings have sensible defaults per spec §8."""
    monkeypatch.delenv("FEEDBACK_SUPPRESSION_MIN_TRADES", raising=False)
    monkeypatch.delenv("FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT", raising=False)
    monkeypatch.delenv("FEEDBACK_PAROLE_DAYS", raising=False)
    monkeypatch.delenv("FEEDBACK_PAROLE_RETEST_TRADES", raising=False)
    monkeypatch.delenv("FEEDBACK_MIN_LEADERBOARD_TRADES", raising=False)
    monkeypatch.delenv("FEEDBACK_MISSED_WINNER_MIN_PCT", raising=False)
    monkeypatch.delenv("FEEDBACK_MISSED_WINNER_MIN_MCAP", raising=False)
    monkeypatch.delenv("FEEDBACK_MISSED_WINNER_WINDOW_MIN", raising=False)
    monkeypatch.delenv("FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN", raising=False)
    monkeypatch.delenv("FEEDBACK_WEEKLY_DIGEST_WEEKDAY", raising=False)
    monkeypatch.delenv("FEEDBACK_WEEKLY_DIGEST_HOUR", raising=False)
    monkeypatch.delenv("FEEDBACK_COMBO_REFRESH_HOUR", raising=False)
    monkeypatch.delenv("FEEDBACK_FALLBACK_ALERT_THRESHOLD", raising=False)
    monkeypatch.delenv("FEEDBACK_FALLBACK_ALERT_COOLDOWN_SEC", raising=False)
    monkeypatch.delenv("FEEDBACK_CHRONIC_FAILURE_THRESHOLD", raising=False)

    from scout.config import Settings
    s = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
    )
    assert s.FEEDBACK_SUPPRESSION_MIN_TRADES == 20
    assert s.FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT == 30.0
    assert s.FEEDBACK_PAROLE_DAYS == 14
    assert s.FEEDBACK_PAROLE_RETEST_TRADES == 5
    assert s.FEEDBACK_MIN_LEADERBOARD_TRADES == 10
    assert s.FEEDBACK_MISSED_WINNER_MIN_PCT == 50.0
    assert s.FEEDBACK_MISSED_WINNER_MIN_MCAP == 5_000_000
    assert s.FEEDBACK_MISSED_WINNER_WINDOW_MIN == 30
    assert s.FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN == 60
    assert s.FEEDBACK_WEEKLY_DIGEST_WEEKDAY == 6
    assert s.FEEDBACK_WEEKLY_DIGEST_HOUR == 9
    assert s.FEEDBACK_COMBO_REFRESH_HOUR == 3
    assert s.FEEDBACK_FALLBACK_ALERT_THRESHOLD == 5
    assert s.FEEDBACK_FALLBACK_ALERT_COOLDOWN_SEC == 900
    assert s.FEEDBACK_CHRONIC_FAILURE_THRESHOLD == 3
