"""Round-2 validation bounds for scout/config.py (PR-H).

Extends PR-F's CRITICAL set with MEDIUM-priority fields per the
post-autodev review (PR #242) config audit, plus two URL-format
field_validators that catch missing schemes at startup.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scout.config import Settings


@pytest.fixture
def _min_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")


class TestThresholdBounds:
    def test_buy_pressure_above_one_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("BUY_PRESSURE_THRESHOLD", "1.5")
        with pytest.raises(ValidationError, match="BUY_PRESSURE_THRESHOLD"):
            Settings()

    def test_momentum_ratio_above_one_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("MOMENTUM_RATIO_THRESHOLD", "2.0")
        with pytest.raises(ValidationError, match="MOMENTUM_RATIO_THRESHOLD"):
            Settings()

    def test_buy_pressure_negative_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("BUY_PRESSURE_THRESHOLD", "-0.1")
        with pytest.raises(ValidationError):
            Settings()


class TestIntervalBounds:
    def test_narrative_poll_too_small_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("NARRATIVE_POLL_INTERVAL", "0")
        with pytest.raises(ValidationError, match="NARRATIVE_POLL_INTERVAL"):
            Settings()

    def test_chain_check_interval_too_small_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("CHAIN_CHECK_INTERVAL_SEC", "0")
        with pytest.raises(ValidationError, match="CHAIN_CHECK_INTERVAL_SEC"):
            Settings()

    def test_chain_max_window_zero_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("CHAIN_MAX_WINDOW_HOURS", "0")
        with pytest.raises(ValidationError, match="CHAIN_MAX_WINDOW_HOURS"):
            Settings()


class TestUrlValidators:
    def test_mirofish_url_missing_scheme_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("MIROFISH_URL", "localhost:5001")
        with pytest.raises(ValidationError, match="MIROFISH_URL"):
            Settings()

    def test_mirofish_url_with_scheme_passes(self, _min_env, monkeypatch):
        for v in ("http://localhost:5001", "https://mirofish.internal:5001"):
            monkeypatch.setenv("MIROFISH_URL", v)
            s = Settings()
            assert s.MIROFISH_URL == v

    def test_discord_webhook_empty_passes(self, _min_env, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
        s = Settings()
        assert s.DISCORD_WEBHOOK_URL == ""

    def test_discord_webhook_http_only_rejected(self, _min_env, monkeypatch):
        # Discord webhooks are https-only; http:// is a misconfig.
        monkeypatch.setenv(
            "DISCORD_WEBHOOK_URL", "http://discord.com/api/webhooks/xxx"
        )
        with pytest.raises(ValidationError, match="DISCORD_WEBHOOK_URL"):
            Settings()

    def test_discord_webhook_https_passes(self, _min_env, monkeypatch):
        monkeypatch.setenv(
            "DISCORD_WEBHOOK_URL",
            "https://discord.com/api/webhooks/12345/abcdef",
        )
        s = Settings()
        assert s.DISCORD_WEBHOOK_URL.startswith("https://")
