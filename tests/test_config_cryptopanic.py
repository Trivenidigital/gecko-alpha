"""Tests for BL-053 CryptoPanic config additions."""

import os
from unittest.mock import patch

from scout.config import Settings


def test_cryptopanic_defaults():
    s = Settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.CRYPTOPANIC_ENABLED is False
    assert s.CRYPTOPANIC_API_TOKEN == ""
    assert s.CRYPTOPANIC_FETCH_FILTER == "hot"
    assert s.CRYPTOPANIC_MACRO_MIN_CURRENCIES == 4
    assert s.CRYPTOPANIC_SCORING_ENABLED is False
    assert s.CRYPTOPANIC_RETENTION_DAYS == 7


def test_cryptopanic_env_overrides():
    env = {
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_CHAT_ID": "c",
        "ANTHROPIC_API_KEY": "k",
        "CRYPTOPANIC_ENABLED": "true",
        "CRYPTOPANIC_API_TOKEN": "abc123",
        "CRYPTOPANIC_FETCH_FILTER": "important",
        "CRYPTOPANIC_MACRO_MIN_CURRENCIES": "5",
        "CRYPTOPANIC_SCORING_ENABLED": "true",
        "CRYPTOPANIC_RETENTION_DAYS": "14",
    }
    with patch.dict(os.environ, env, clear=False):
        s = Settings()
    assert s.CRYPTOPANIC_ENABLED is True
    assert s.CRYPTOPANIC_API_TOKEN == "abc123"
    assert s.CRYPTOPANIC_FETCH_FILTER == "important"
    assert s.CRYPTOPANIC_MACRO_MIN_CURRENCIES == 5
    assert s.CRYPTOPANIC_SCORING_ENABLED is True
    assert s.CRYPTOPANIC_RETENTION_DAYS == 14
