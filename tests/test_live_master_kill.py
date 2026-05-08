"""BL-NEW-LIVE-HYBRID M1: master kill + per-token aggregator + override."""

from __future__ import annotations

import pytest

from scout.config import Settings

# Required fields for Settings construction in tests (per project convention,
# see tests/conftest.py settings_factory + tests/test_config.py).
_REQUIRED = dict(
    TELEGRAM_BOT_TOKEN="t",
    TELEGRAM_CHAT_ID="c",
    ANTHROPIC_API_KEY="k",
)


class TestLiveTradingSettings:
    def test_master_kill_defaults_off(self):
        assert Settings(_env_file=None, **_REQUIRED).LIVE_TRADING_ENABLED is False

    def test_max_open_positions_per_token_default(self):
        assert (
            Settings(_env_file=None, **_REQUIRED).LIVE_MAX_OPEN_POSITIONS_PER_TOKEN == 1
        )

    def test_override_replace_only_default(self):
        assert Settings(_env_file=None, **_REQUIRED).LIVE_OVERRIDE_REPLACE_ONLY is False


class TestLiveTradingValidators:
    def test_max_open_positions_per_token_must_be_at_least_1(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            Settings(
                _env_file=None,
                **_REQUIRED,
                LIVE_MAX_OPEN_POSITIONS_PER_TOKEN=0,
            )
