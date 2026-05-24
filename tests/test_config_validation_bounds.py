"""Validation bound regression tests for scout/config.py.

Eight production-readiness bounds added to Settings fields that previously
accepted any int/float, surfacing misconfig at scanner startup rather than
as confusing runtime symptoms (instant-loop CPU burn, signal-filter bypass,
credit-soft-cap inversion, etc.).

Each test creates a Settings instance with the offending value and asserts
that Pydantic raises ValidationError. Settings is constructed with the
minimum required secret env to avoid unrelated validation noise.
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from scout.config import Settings


@pytest.fixture
def _min_env(monkeypatch):
    """Provide the minimum env vars Settings requires.

    Without these, Pydantic raises ValidationError on missing secrets,
    not on the bound we're trying to verify.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")


class TestScannerBounds:
    def test_scan_interval_zero_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("SCAN_INTERVAL_SECONDS", "0")
        with pytest.raises(ValidationError, match="SCAN_INTERVAL_SECONDS"):
            Settings()

    def test_scan_interval_negative_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("SCAN_INTERVAL_SECONDS", "-1")
        with pytest.raises(ValidationError):
            Settings()

    def test_scan_interval_default_passes(self, _min_env):
        s = Settings()
        assert s.SCAN_INTERVAL_SECONDS == 60


class TestScoreBounds:
    def test_min_score_obscenely_large_rejected(self, _min_env, monkeypatch):
        # le=10_000 admits 999 (the test-sentinel for disable) but catches
        # accidental "MIN_SCORE=99999999".
        monkeypatch.setenv("MIN_SCORE", "99999999")
        with pytest.raises(ValidationError, match="MIN_SCORE"):
            Settings()

    def test_min_score_negative_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("MIN_SCORE", "-5")
        with pytest.raises(ValidationError, match="MIN_SCORE"):
            Settings()

    def test_min_score_sentinel_disable_passes(self, _min_env, monkeypatch):
        # 999 is the canonical "disable this gate" sentinel used in tests.
        monkeypatch.setenv("MIN_SCORE", "999")
        s = Settings()
        assert s.MIN_SCORE == 999

    def test_conviction_threshold_obscenely_large_rejected(
        self, _min_env, monkeypatch
    ):
        monkeypatch.setenv("CONVICTION_THRESHOLD", "99999999")
        with pytest.raises(ValidationError, match="CONVICTION_THRESHOLD"):
            Settings()

    def test_conviction_threshold_at_boundary_passes(self, _min_env, monkeypatch):
        for value in ("0", "50", "100", "999"):
            monkeypatch.setenv("CONVICTION_THRESHOLD", value)
            s = Settings()
            assert s.CONVICTION_THRESHOLD == int(value)


class TestMirofishBounds:
    def test_mirofish_timeout_zero_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("MIROFISH_TIMEOUT_SEC", "0")
        with pytest.raises(ValidationError, match="MIROFISH_TIMEOUT_SEC"):
            Settings()


class TestChainBounds:
    def test_chain_unhealthy_rate_above_one_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("CHAIN_TRACKER_UNHEALTHY_FAILURE_RATE", "1.5")
        with pytest.raises(
            ValidationError, match="CHAIN_TRACKER_UNHEALTHY_FAILURE_RATE"
        ):
            Settings()

    def test_chain_unhealthy_rate_negative_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("CHAIN_TRACKER_UNHEALTHY_FAILURE_RATE", "-0.1")
        with pytest.raises(ValidationError):
            Settings()

    def test_chain_persistent_failure_zero_rejected(self, _min_env, monkeypatch):
        # gt=0 — 0 disables the aging logic silently.
        monkeypatch.setenv("CHAIN_OUTCOME_PERSISTENT_FAILURE_HOURS", "0")
        with pytest.raises(
            ValidationError, match="CHAIN_OUTCOME_PERSISTENT_FAILURE_HOURS"
        ):
            Settings()


class TestLunarcrushBounds:
    def test_credit_soft_above_one_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("LUNARCRUSH_CREDIT_SOFT_PCT", "1.2")
        with pytest.raises(ValidationError, match="LUNARCRUSH_CREDIT_SOFT_PCT"):
            Settings()

    def test_credit_hard_above_one_rejected(self, _min_env, monkeypatch):
        monkeypatch.setenv("LUNARCRUSH_CREDIT_HARD_PCT", "1.5")
        with pytest.raises(ValidationError, match="LUNARCRUSH_CREDIT_HARD_PCT"):
            Settings()
