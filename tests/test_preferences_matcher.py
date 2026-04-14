"""Tests for scout.preferences.matcher — personalized narrative matching."""

from __future__ import annotations

import pytest

from scout.preferences.matcher import should_alert_category, should_alert_token


class _FakeStrategy:
    """Minimal strategy stub with .get() method."""

    def __init__(self, overrides: dict | None = None) -> None:
        from scout.narrative.strategy import STRATEGY_DEFAULTS

        self._data: dict = dict(STRATEGY_DEFAULTS)
        if overrides:
            self._data.update(overrides)

    def get(self, key: str) -> object:
        return self._data[key]


# ── should_alert_category ───────────────────────────────────────────


class TestShouldAlertCategory:
    def test_mode_all_always_returns_true(self) -> None:
        strategy = _FakeStrategy({"user_alert_mode": "all"})
        assert should_alert_category("artificial-intelligence", strategy) is True
        assert should_alert_category("meme-token", strategy) is True

    def test_preferred_only_matches(self) -> None:
        strategy = _FakeStrategy(
            {
                "user_alert_mode": "preferred_only",
                "user_preferred_categories": ["artificial-intelligence", "depin"],
            }
        )
        assert should_alert_category("artificial-intelligence", strategy) is True
        assert should_alert_category("depin", strategy) is True
        assert should_alert_category("meme-token", strategy) is False

    def test_preferred_only_empty_list_blocks_all(self) -> None:
        strategy = _FakeStrategy(
            {
                "user_alert_mode": "preferred_only",
                "user_preferred_categories": [],
            }
        )
        assert should_alert_category("anything", strategy) is False

    def test_exclude_only_blocks_excluded(self) -> None:
        strategy = _FakeStrategy(
            {
                "user_alert_mode": "exclude_only",
                "user_excluded_categories": ["meme-token", "wrapped-tokens"],
            }
        )
        assert should_alert_category("meme-token", strategy) is False
        assert should_alert_category("wrapped-tokens", strategy) is False
        assert should_alert_category("artificial-intelligence", strategy) is True

    def test_exclude_only_empty_list_allows_all(self) -> None:
        strategy = _FakeStrategy(
            {
                "user_alert_mode": "exclude_only",
                "user_excluded_categories": [],
            }
        )
        assert should_alert_category("anything", strategy) is True

    def test_invalid_mode_falls_back_to_true(self) -> None:
        strategy = _FakeStrategy({"user_alert_mode": "bogus"})
        assert should_alert_category("anything", strategy) is True

    def test_default_strategy_allows_all(self) -> None:
        """Default STRATEGY_DEFAULTS has mode='all', so everything passes."""
        strategy = _FakeStrategy()
        assert should_alert_category("artificial-intelligence", strategy) is True


# ── should_alert_token ──────────────────────────────────────────────


class TestShouldAlertToken:
    def test_no_filters_allows_all(self) -> None:
        strategy = _FakeStrategy(
            {"user_min_market_cap": 0, "user_max_market_cap": 0}
        )
        assert should_alert_token(100.0, strategy) is True
        assert should_alert_token(1_000_000_000.0, strategy) is True

    def test_min_mcap_filter(self) -> None:
        strategy = _FakeStrategy(
            {"user_min_market_cap": 1_000_000, "user_max_market_cap": 0}
        )
        assert should_alert_token(500_000, strategy) is False
        assert should_alert_token(1_000_000, strategy) is True
        assert should_alert_token(5_000_000, strategy) is True

    def test_max_mcap_filter(self) -> None:
        strategy = _FakeStrategy(
            {"user_min_market_cap": 0, "user_max_market_cap": 10_000_000}
        )
        assert should_alert_token(5_000_000, strategy) is True
        assert should_alert_token(10_000_000, strategy) is True
        assert should_alert_token(10_000_001, strategy) is False

    def test_both_mcap_filters(self) -> None:
        strategy = _FakeStrategy(
            {"user_min_market_cap": 1_000_000, "user_max_market_cap": 10_000_000}
        )
        assert should_alert_token(500_000, strategy) is False
        assert should_alert_token(5_000_000, strategy) is True
        assert should_alert_token(15_000_000, strategy) is False

    def test_default_strategy_allows_all(self) -> None:
        strategy = _FakeStrategy()
        assert should_alert_token(42.0, strategy) is True
        assert should_alert_token(999_999_999.0, strategy) is True
