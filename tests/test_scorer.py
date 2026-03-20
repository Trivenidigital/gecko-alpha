"""Tests for quantitative scoring engine."""

import pytest

from scout.config import Settings
from scout.models import CandidateToken
from scout.scorer import score


def _settings(**overrides) -> Settings:
    defaults = dict(
        TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_token(**overrides) -> CandidateToken:
    defaults = dict(
        contract_address="0xtest", chain="solana", token_name="Test",
        ticker="TST", token_age_days=1.0, market_cap_usd=50000.0,
        liquidity_usd=10000.0, volume_24h_usd=80000.0,
        holder_count=100, holder_growth_1h=25,
        social_mentions_24h=0,
    )
    defaults.update(overrides)
    return CandidateToken(**defaults)


class TestIndividualSignals:
    """Test each signal fires independently."""

    def test_vol_liq_ratio_fires(self):
        # volume/liquidity = 80000/10000 = 8× (> 5×)
        token = _make_token(volume_24h_usd=80000, liquidity_usd=10000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "vol_liq_ratio" in signals
        assert points == 30

    def test_vol_liq_ratio_does_not_fire(self):
        # volume/liquidity = 20000/10000 = 2× (< 5×)
        token = _make_token(volume_24h_usd=20000, liquidity_usd=10000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "vol_liq_ratio" not in signals

    def test_market_cap_range_fires(self):
        token = _make_token(volume_24h_usd=1000, liquidity_usd=10000,
                            market_cap_usd=50000, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "market_cap_range" in signals
        assert points == 20

    def test_market_cap_below_range(self):
        token = _make_token(volume_24h_usd=1000, liquidity_usd=10000,
                            market_cap_usd=5000, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "market_cap_range" not in signals

    def test_market_cap_above_range(self):
        token = _make_token(volume_24h_usd=1000, liquidity_usd=10000,
                            market_cap_usd=600000, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "market_cap_range" not in signals

    def test_holder_growth_fires(self):
        token = _make_token(volume_24h_usd=1000, liquidity_usd=10000,
                            market_cap_usd=999999, holder_growth_1h=25,
                            token_age_days=30, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "holder_growth" in signals
        assert points == 25

    def test_holder_growth_exactly_20(self):
        # > 20, not >= 20
        token = _make_token(volume_24h_usd=1000, liquidity_usd=10000,
                            market_cap_usd=999999, holder_growth_1h=20,
                            token_age_days=30, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "holder_growth" not in signals

    def test_token_age_fires(self):
        token = _make_token(volume_24h_usd=1000, liquidity_usd=10000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=3, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "token_age" in signals
        assert points == 10

    def test_token_age_exactly_7(self):
        # < 7, not <= 7
        token = _make_token(volume_24h_usd=1000, liquidity_usd=10000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=7, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "token_age" not in signals

    def test_social_mentions_fires(self):
        token = _make_token(volume_24h_usd=1000, liquidity_usd=10000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=60)
        points, signals = score(token, _settings())
        assert "social_mentions" in signals
        assert points == 15

    def test_social_mentions_zero(self):
        token = _make_token(volume_24h_usd=1000, liquidity_usd=10000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "social_mentions" not in signals
        assert points == 0


class TestCombinedScoring:
    """Test combined signal behavior."""

    def test_all_signals_fire(self):
        token = _make_token(
            volume_24h_usd=80000, liquidity_usd=10000,
            market_cap_usd=50000, holder_growth_1h=25,
            token_age_days=3, social_mentions_24h=60,
        )
        points, signals = score(token, _settings())
        assert points == 100
        assert len(signals) == 5

    def test_no_signals_fire(self):
        token = _make_token(
            volume_24h_usd=1000, liquidity_usd=10000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert points == 0
        assert signals == []

    def test_returns_tuple(self):
        token = _make_token()
        result = score(token, _settings())
        assert isinstance(result, tuple)
        assert isinstance(result[0], int)
        assert isinstance(result[1], list)


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_zero_liquidity(self):
        """Zero liquidity -> vol/liq ratio undefined -> no points."""
        token = _make_token(
            volume_24h_usd=80000, liquidity_usd=0,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "vol_liq_ratio" not in signals

    def test_zero_volume(self):
        token = _make_token(
            volume_24h_usd=0, liquidity_usd=10000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "vol_liq_ratio" not in signals

    def test_custom_thresholds(self):
        """Scoring uses settings for thresholds, not hardcoded values."""
        settings = _settings(MIN_VOL_LIQ_RATIO=10.0, MAX_TOKEN_AGE_DAYS=3)
        token = _make_token(
            volume_24h_usd=80000, liquidity_usd=10000,  # ratio 8x < 10x
            market_cap_usd=50000, holder_growth_1h=25,
            token_age_days=2, social_mentions_24h=0,
        )
        points, signals = score(token, settings)
        assert "vol_liq_ratio" not in signals  # 8x < 10x threshold
        assert "token_age" in signals  # 2 < 3


class TestCoinGeckoSignals:
    """Test CoinGecko-specific scoring signals."""

    def test_momentum_ratio_signal_fires(self):
        """1h/24h ratio > 0.6 -> +20 pts."""
        token = _make_token(
            price_change_1h=8.0, price_change_24h=12.0,
            volume_24h_usd=1000, liquidity_usd=10000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        # ratio = 8/12 = 0.67 > 0.6
        points, signals = score(token, _settings())
        assert "momentum_ratio" in signals

    def test_momentum_ratio_none_safe(self):
        """price_change_1h=None -> 0 pts, no exception."""
        token = _make_token(
            price_change_1h=None, price_change_24h=10.0,
            volume_24h_usd=1000, liquidity_usd=10000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "momentum_ratio" not in signals

    def test_vol_acceleration_signal_fires(self):
        """volume/7d_avg > 5.0 -> +25 pts."""
        token = _make_token(
            volume_24h_usd=500_000, vol_7d_avg=80_000,
            liquidity_usd=10000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        # ratio = 500k/80k = 6.25 > 5.0
        points, signals = score(token, _settings())
        assert "vol_acceleration" in signals

    def test_cg_trending_rank_signal_fires(self):
        """cg_trending_rank=5 (<=10) -> +15 pts."""
        token = _make_token(
            cg_trending_rank=5,
            volume_24h_usd=1000, liquidity_usd=10000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "cg_trending_rank" in signals

    def test_cg_trending_rank_over_10(self):
        """cg_trending_rank=11 (>10) -> 0 pts."""
        token = _make_token(
            cg_trending_rank=11,
            volume_24h_usd=1000, liquidity_usd=10000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "cg_trending_rank" not in signals

    def test_momentum_ratio_negative_prices_no_fire(self):
        """Both negative prices: ratio > 0.6 but this is a crash, not a pump."""
        token = _make_token(
            price_change_1h=-8.0, price_change_24h=-10.0,
            volume_24h_usd=1000, liquidity_usd=10000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        # ratio = -8/-10 = 0.8 > 0.6, but both negative = crash
        points, signals = score(token, _settings())
        assert "momentum_ratio" not in signals

    def test_score_capped_at_100(self):
        """Raw score exceeding 100 is capped to 100."""
        # Fire all 8 signals: raw = 30+20+25+10+15+20+25+15 = 160
        token = _make_token(
            volume_24h_usd=80000, liquidity_usd=10000,  # vol_liq_ratio: +30
            market_cap_usd=50000,                        # market_cap_range: +20
            holder_growth_1h=25,                         # holder_growth: +25
            token_age_days=3,                            # token_age: +10
            social_mentions_24h=60,                      # social_mentions: +15
            price_change_1h=8.0, price_change_24h=12.0,  # momentum_ratio: +20
            vol_7d_avg=10000,                            # vol_acceleration: +25 (80k/10k=8>5)
            cg_trending_rank=5,                          # cg_trending_rank: +15
        )
        points, signals = score(token, _settings())
        assert points == 100
        assert len(signals) == 8
