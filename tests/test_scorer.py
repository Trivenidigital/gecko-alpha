"""Tests for quantitative scoring engine."""

import pytest

from scout.config import Settings
from scout.models import CandidateToken
from scout.scorer import score, signal_confidence


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
        liquidity_usd=20000.0, volume_24h_usd=80000.0,
        holder_count=100, holder_growth_1h=25,
        social_mentions_24h=0,
    )
    defaults.update(overrides)
    return CandidateToken(**defaults)


class TestIndividualSignals:
    """Test each signal fires independently."""

    def test_vol_liq_ratio_fires(self):
        # volume/liquidity = 120000/20000 = 6× (> 5×)
        token = _make_token(volume_24h_usd=120000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0,
                            chain="ethereum")  # no solana bonus
        points, signals = score(token, _settings())
        assert "vol_liq_ratio" in signals
        # raw=30, normalized=int(30*100/178)=16, *0.8=12
        assert points == 12

    def test_vol_liq_ratio_does_not_fire(self):
        # volume/liquidity = 20000/20000 = 1× (< 5×)
        token = _make_token(volume_24h_usd=20000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "vol_liq_ratio" not in signals

    def test_market_cap_range_fires_sweet_spot(self):
        """$10K-$100K -> 8 pts raw (peak tier)."""
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=50000, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0,
                            chain="ethereum")
        points, signals = score(token, _settings())
        assert "market_cap_range" in signals

    def test_market_cap_range_mid_tier(self):
        """$100K-$250K -> 5 pts raw."""
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=150000, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0,
                            chain="ethereum")
        points, signals = score(token, _settings())
        assert "market_cap_range" in signals

    def test_market_cap_range_low_tier(self):
        """$250K-$500K -> 2 pts raw."""
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=400000, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0,
                            chain="ethereum")
        points, signals = score(token, _settings())
        assert "market_cap_range" in signals

    def test_market_cap_below_range(self):
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=5000, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "market_cap_range" not in signals

    def test_market_cap_above_range(self):
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=600000, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "market_cap_range" not in signals

    def test_holder_growth_fires(self):
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=25,
                            token_age_days=30, social_mentions_24h=0,
                            chain="ethereum")
        points, signals = score(token, _settings())
        assert "holder_growth" in signals
        # raw=25, normalized=int(25*100/178)=14
        assert points == 14

    def test_holder_growth_exactly_20(self):
        # > 20, not >= 20
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=20,
                            token_age_days=30, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "holder_growth" not in signals

    def test_token_age_peak_window(self):
        """1-3 days -> 10 pts raw."""
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=2.0, social_mentions_24h=0,
                            chain="ethereum")
        points, signals = score(token, _settings())
        assert "token_age" in signals
        # raw=10, normalized=int(10*100/178)=5
        assert points == 5

    def test_token_age_early(self):
        """12-24h -> 5 pts raw."""
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=0.6, social_mentions_24h=0,
                            chain="ethereum")
        points, signals = score(token, _settings())
        assert "token_age" in signals
        # raw=5, normalized=int(5*100/178)=2
        assert points == 2

    def test_token_age_declining(self):
        """3-5 days -> 5 pts raw."""
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=4.0, social_mentions_24h=0,
                            chain="ethereum")
        points, signals = score(token, _settings())
        assert "token_age" in signals
        # raw=5, normalized=int(5*100/178)=2
        assert points == 2

    def test_token_age_too_new(self):
        """< 12h -> 0 pts."""
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=0.3, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "token_age" not in signals

    def test_token_age_too_old(self):
        """> 5 days -> 0 pts."""
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=7.0, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "token_age" not in signals

    def test_social_mentions_fires(self):
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=60,
                            chain="ethereum")
        points, signals = score(token, _settings())
        assert "social_mentions" in signals
        # raw=15, normalized=int(15*100/178)=8
        assert points == 8

    def test_social_mentions_zero(self):
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0,
                            chain="ethereum")
        points, signals = score(token, _settings())
        assert "social_mentions" not in signals
        assert points == 0


class TestCombinedScoring:
    """Test combined signal behavior."""

    def test_all_base_signals_fire(self):
        token = _make_token(
            volume_24h_usd=120000, liquidity_usd=20000,
            market_cap_usd=50000, holder_growth_1h=25,
            token_age_days=2, social_mentions_24h=60,
            txns_h1_buys=70, txns_h1_sells=30,
        )
        points, signals = score(token, _settings())
        assert len(signals) >= 6
        assert points > 0

    def test_no_signals_fire(self):
        token = _make_token(
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
            chain="ethereum",
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


class TestHardDisqualifiers:
    """Test hard disqualifier pre-filters."""

    def test_liquidity_below_floor_returns_zero(self):
        """Liquidity < $15K -> score 0, no signals."""
        token = _make_token(
            liquidity_usd=10000,  # below 15K default
            volume_24h_usd=80000, market_cap_usd=50000,
            holder_growth_1h=25, token_age_days=2,
        )
        points, signals = score(token, _settings())
        assert points == 0
        assert signals == []

    def test_liquidity_at_floor_passes(self):
        """Liquidity >= $15K -> normal scoring."""
        token = _make_token(
            liquidity_usd=15000,
            volume_24h_usd=1000, market_cap_usd=50000,
            holder_growth_1h=0, token_age_days=2,
            social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert points > 0  # at least market_cap_range + token_age

    def test_liquidity_floor_configurable(self):
        """Custom MIN_LIQUIDITY_USD threshold."""
        settings = _settings(MIN_LIQUIDITY_USD=5000)
        token = _make_token(
            liquidity_usd=6000,
            volume_24h_usd=1000, market_cap_usd=50000,
            holder_growth_1h=0, token_age_days=2,
            social_mentions_24h=0,
        )
        points, signals = score(token, settings)
        assert points > 0


class TestScoreVelocity:
    """Test score velocity bonus."""

    def test_velocity_fires_on_rising_scores(self):
        """3 strictly increasing scores -> +10 pts."""
        token = _make_token(
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=50000, holder_growth_1h=0,
            token_age_days=2, social_mentions_24h=0,
            chain="ethereum",
        )
        # newest first: [70, 60, 50] -> reversed = [50, 60, 70] strictly increasing
        points, signals = score(token, _settings(), historical_scores=[70, 60, 50])
        assert "score_velocity" in signals
        # raw = market_cap(8) + token_age(10) + velocity(10) = 28
        # normalized = int(28*100/178) = 15
        assert points == 15

    def test_velocity_no_fire_flat(self):
        """Flat scores -> no bonus."""
        token = _make_token(
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings(), historical_scores=[60, 60, 60])
        assert "score_velocity" not in signals

    def test_velocity_no_fire_declining(self):
        """Declining scores -> no bonus."""
        token = _make_token(
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings(), historical_scores=[50, 60, 70])
        assert "score_velocity" not in signals

    def test_velocity_no_fire_insufficient_history(self):
        """< 3 scores -> no bonus."""
        token = _make_token(
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings(), historical_scores=[70, 60])
        assert "score_velocity" not in signals

    def test_velocity_none_historical_scores(self):
        """None historical_scores -> no bonus, no error."""
        token = _make_token(
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings(), historical_scores=None)
        assert "score_velocity" not in signals


class TestCoOccurrence:
    """Test co-occurrence multiplier."""

    def test_vol_liq_plus_holder_growth_bonus(self):
        """Both vol_liq_ratio + holder_growth -> 1.2x multiplier."""
        token = _make_token(
            volume_24h_usd=120000, liquidity_usd=20000,
            holder_growth_1h=25,
            market_cap_usd=999999, token_age_days=30,
            social_mentions_24h=0, chain="ethereum",
        )
        points, signals = score(token, _settings())
        # raw=55, normalized=int(55*100/178)=30, *1.2=int(36)=36
        assert points == 36
        assert "vol_liq_ratio" in signals
        assert "holder_growth" in signals

    def test_vol_liq_alone_penalty(self):
        """vol_liq_ratio without holder_growth -> 0.8x multiplier."""
        token = _make_token(
            volume_24h_usd=120000, liquidity_usd=20000,
            holder_growth_1h=0,
            market_cap_usd=999999, token_age_days=30,
            social_mentions_24h=0, chain="ethereum",
        )
        points, signals = score(token, _settings())
        # raw=30, normalized=int(30*100/178)=16, *0.8=int(12.8)=12
        assert points == 12
        assert "vol_liq_ratio" in signals
        assert "holder_growth" not in signals

    def test_no_vol_liq_no_multiplier(self):
        """No vol_liq_ratio -> no multiplier applied."""
        token = _make_token(
            volume_24h_usd=1000, liquidity_usd=20000,
            holder_growth_1h=25,
            market_cap_usd=50000, token_age_days=2,
            social_mentions_24h=0, chain="ethereum",
        )
        points, signals = score(token, _settings())
        # raw=25+8+10=43, normalized=int(43*100/178)=24, no multiplier
        assert points == 24
        assert "vol_liq_ratio" not in signals


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
            volume_24h_usd=0, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "vol_liq_ratio" not in signals

    def test_custom_thresholds(self):
        """Scoring uses settings for thresholds, not hardcoded values."""
        settings = _settings(MIN_VOL_LIQ_RATIO=10.0)
        token = _make_token(
            volume_24h_usd=160000, liquidity_usd=20000,  # ratio 8x < 10x
            market_cap_usd=50000, holder_growth_1h=25,
            token_age_days=2, social_mentions_24h=0,
        )
        points, signals = score(token, settings)
        assert "vol_liq_ratio" not in signals  # 8x < 10x threshold
        assert "token_age" in signals  # 2 days = peak window
        assert points > 0


class TestBuyPressureSignal:
    """Test buy pressure ratio signal."""

    def test_buy_pressure_fires(self):
        """buy_ratio > 65% -> +15 pts raw."""
        token = _make_token(
            txns_h1_buys=70, txns_h1_sells=30,
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
            chain="ethereum",
        )
        points, signals = score(token, _settings())
        assert "buy_pressure" in signals
        # raw=15, normalized=int(15*100/178)=8
        assert points == 8

    def test_buy_pressure_does_not_fire_balanced(self):
        """buy_ratio = 50% -> no points."""
        token = _make_token(
            txns_h1_buys=50, txns_h1_sells=50,
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "buy_pressure" not in signals

    def test_buy_pressure_none_safe(self):
        """txns_h1_buys=None -> no points, no exception."""
        token = _make_token(
            txns_h1_buys=None, txns_h1_sells=None,
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "buy_pressure" not in signals

    def test_buy_pressure_zero_txns(self):
        """Zero total txns -> no division error."""
        token = _make_token(
            txns_h1_buys=0, txns_h1_sells=0,
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "buy_pressure" not in signals


class TestCoinGeckoSignals:
    """Test CoinGecko-specific scoring signals."""

    def test_momentum_ratio_signal_fires(self):
        """1h/24h ratio > 0.6 -> +20 pts."""
        token = _make_token(
            price_change_1h=8.0, price_change_24h=12.0,
            volume_24h_usd=1000, liquidity_usd=20000,
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
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "momentum_ratio" not in signals

    def test_vol_acceleration_signal_fires(self):
        """volume/7d_avg > 5.0 -> +25 pts."""
        token = _make_token(
            volume_24h_usd=500_000, vol_7d_avg=80_000,
            liquidity_usd=20000,
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
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "cg_trending_rank" in signals

    def test_cg_trending_rank_over_10(self):
        """cg_trending_rank=11 (>10) -> 0 pts."""
        token = _make_token(
            cg_trending_rank=11,
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "cg_trending_rank" not in signals

    def test_momentum_ratio_negative_prices_no_fire(self):
        """Both negative prices: ratio > 0.6 but this is a crash, not a pump."""
        token = _make_token(
            price_change_1h=-8.0, price_change_24h=-10.0,
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        # ratio = -8/-10 = 0.8 > 0.6, but both negative = crash
        points, signals = score(token, _settings())
        assert "momentum_ratio" not in signals

    def test_solana_bonus_fires(self):
        """Solana chain -> +5 pts."""
        token = _make_token(
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
            chain="solana",
        )
        points, signals = score(token, _settings())
        assert "solana_bonus" in signals
        # raw=5, normalized=int(5*100/178)=2
        assert points == 2

    def test_no_solana_bonus_for_ethereum(self):
        """Non-solana chain -> no bonus."""
        token = _make_token(
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
            chain="ethereum",
        )
        points, signals = score(token, _settings())
        assert "solana_bonus" not in signals

    def test_score_capped_at_100(self):
        """All signals firing stays within 0-100 range."""
        token = _make_token(
            volume_24h_usd=120000, liquidity_usd=20000,
            market_cap_usd=50000,
            holder_growth_1h=25,
            token_age_days=2,
            social_mentions_24h=60,
            txns_h1_buys=70, txns_h1_sells=30,
            price_change_1h=8.0, price_change_24h=12.0,
            vol_7d_avg=10000,
            cg_trending_rank=5,
            chain="solana",  # +5 solana bonus
        )
        points, signals = score(token, _settings())
        assert points == 100
        assert len(signals) >= 9


class TestSignalConfidence:
    """Test signal_confidence helper."""

    def test_high_confidence(self):
        assert signal_confidence(["a", "b", "c"]) == "HIGH"
        assert signal_confidence(["a", "b", "c", "d"]) == "HIGH"

    def test_medium_confidence(self):
        assert signal_confidence(["a", "b"]) == "MEDIUM"

    def test_low_confidence(self):
        assert signal_confidence(["a"]) == "LOW"
        assert signal_confidence([]) == "LOW"
