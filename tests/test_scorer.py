"""Tests for quantitative scoring engine."""

import pytest

from scout.scorer import score, signal_confidence


# Scorer tests need liquidity_usd=20000 (above floor) and social_mentions_24h=0
# as defaults. These module-level helpers wrap conftest fixtures with scorer defaults.
_settings = None
_make_token = None


@pytest.fixture(autouse=True)
def _scorer_helpers(settings_factory, token_factory):
    """Wire conftest fixtures into module-level helpers for all tests."""
    global _settings, _make_token

    def _s(**overrides):
        return settings_factory(**overrides)

    def _mt(**overrides):
        defaults = dict(
            liquidity_usd=20000.0, social_mentions_24h=0,
        )
        defaults.update(overrides)
        return token_factory(**defaults)

    _settings = _s
    _make_token = _mt


class TestIndividualSignals:
    """Test each signal fires independently."""

    def test_vol_liq_ratio_fires(self):
        # volume/liquidity = 120000/20000 = 6× (> 5×)
        token = _make_token(volume_24h_usd=120000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=30, social_mentions_24h=0,
                            chain="ethereum")
        points, signals = score(token, _settings())
        assert "vol_liq_ratio" in signals
        # raw=30, normalized=int(30*100/183)=16, no multiplier (1 signal)
        assert points == 16

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
        # raw=25, normalized=int(25*100/183)=13
        assert points == 13

    def test_holder_growth_exactly_20(self):
        # > 20, not >= 20
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=20,
                            token_age_days=30, social_mentions_24h=0)
        points, signals = score(token, _settings())
        assert "holder_growth" not in signals

    def test_token_age_peak_window(self):
        """12-48h -> 15 pts raw (peak)."""
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=1.0, social_mentions_24h=0,
                            chain="ethereum")  # 24h = peak
        points, signals = score(token, _settings())
        assert "token_age" in signals
        # raw=15, normalized=int(15*100/183)=8
        assert points == 8

    def test_token_age_early(self):
        """3-12h -> 8 pts raw."""
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=0.25, social_mentions_24h=0,
                            chain="ethereum")  # 6h
        points, signals = score(token, _settings())
        assert "token_age" in signals
        # raw=8, normalized=int(8*100/183)=4
        assert points == 4

    def test_token_age_declining(self):
        """48h-7d -> 5 pts raw."""
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=4.0, social_mentions_24h=0,
                            chain="ethereum")
        points, signals = score(token, _settings())
        assert "token_age" in signals
        # raw=5, normalized=int(5*100/183)=2
        assert points == 2

    def test_token_age_too_new(self):
        """< 3h -> 0 pts."""
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=0.05, social_mentions_24h=0,
                            chain="ethereum")  # ~1.2h
        points, signals = score(token, _settings())
        assert "token_age" not in signals

    def test_token_age_too_old(self):
        """> 7 days -> 0 pts."""
        token = _make_token(volume_24h_usd=1000, liquidity_usd=20000,
                            market_cap_usd=999999, holder_growth_1h=0,
                            token_age_days=8.0, social_mentions_24h=0,
                            chain="ethereum")
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
        """Liquidity < $15K -> score 0, disqualified."""
        token = _make_token(
            liquidity_usd=10000,  # below 15K default
            volume_24h_usd=80000, market_cap_usd=50000,
            holder_growth_1h=25, token_age_days=2,
        )
        points, signals = score(token, _settings())
        assert points == 0
        assert "DISQUALIFIED_LOW_LIQUIDITY" in signals

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
            token_age_days=1.0, social_mentions_24h=0,
            chain="ethereum",
        )
        # newest first: [70, 60, 50] -> reversed = [50, 60, 70] strictly increasing
        points, signals = score(token, _settings(), historical_scores=[70, 60, 50])
        assert "score_velocity" in signals
        # raw = market_cap(8) + token_age(15) + velocity(10) = 33
        # normalized = int(33*100/183) = 18, 3 signals -> *1.15 = int(20.7) = 20
        assert points == 20

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
    """Test co-occurrence multiplier (3+ signals -> 1.15x)."""

    def test_three_signals_gets_multiplier(self):
        """3+ signals -> 1.15x multiplier."""
        token = _make_token(
            volume_24h_usd=120000, liquidity_usd=20000,  # vol_liq: +30
            holder_growth_1h=25,                          # holder: +25
            market_cap_usd=50000, token_age_days=1.0,     # cap: +8, age: +15
            social_mentions_24h=0, chain="ethereum",
        )
        points, signals = score(token, _settings())
        assert len(signals) >= 3
        # raw=78, normalized=int(78*100/183)=42, *1.15=int(48.3)=48
        assert points == 48

    def test_two_signals_no_multiplier(self):
        """2 signals -> no multiplier."""
        token = _make_token(
            volume_24h_usd=120000, liquidity_usd=20000,  # vol_liq: +30
            holder_growth_1h=0,
            market_cap_usd=999999, token_age_days=30,
            social_mentions_24h=0, chain="ethereum",
        )
        points, signals = score(token, _settings())
        assert len(signals) == 1  # only vol_liq
        # raw=30, normalized=int(30*100/183)=16, no multiplier
        assert points == 16

    def test_multiplier_capped_at_100(self):
        """Multiplier cannot push score above 100."""
        token = _make_token(
            volume_24h_usd=120000, liquidity_usd=20000,
            market_cap_usd=50000, holder_growth_1h=25,
            token_age_days=1.0, social_mentions_24h=60,
            txns_h1_buys=70, txns_h1_sells=30,
            price_change_1h=8.0, price_change_24h=12.0,
            vol_7d_avg=10000, cg_trending_rank=5,
            chain="solana",
        )
        points, signals = score(token, _settings())
        assert points <= 100


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
        # raw=15, normalized=int(15*100/183)=8
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


class TestLiquidityFloorExemption:
    """CoinGecko-chain tokens are exempt from the liquidity floor.

    The exemption keys off ``chain == 'coingecko'`` — not trending rank.
    CG-listed tokens have ``liquidity_usd=0`` because there is no on-chain
    pool; their real liquidity lives on CEX order books. The liquidity
    floor is meant for DEX memecoins only.
    """

    def test_cg_trending_exempt_from_liquidity_floor(self):
        """Token with cg_trending_rank set bypasses liquidity floor."""
        token = _make_token(
            liquidity_usd=0, market_cap_usd=0,
            cg_trending_rank=3, chain="coingecko",
            volume_24h_usd=0, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "DISQUALIFIED_LOW_LIQUIDITY" not in signals
        assert "cg_trending_rank" in signals

    def test_no_trending_rank_gets_disqualified(self):
        """Token without trending rank and low liquidity is disqualified."""
        token = _make_token(
            liquidity_usd=5000,
            volume_24h_usd=80000, market_cap_usd=50000,
            holder_growth_1h=25, token_age_days=1.0,
            chain="ethereum",
        )
        points, signals = score(token, _settings())
        assert points == 0
        assert "DISQUALIFIED_LOW_LIQUIDITY" in signals

    def test_coingecko_chain_exempt_without_trending_rank(self):
        """CG-listed tokens are exempt even when not in trending top-10.

        CG tokens have no on-chain pool liquidity data (liquidity_usd=0)
        but are listed on major exchanges with real order-book depth.
        The liquidity floor is designed for DEX memecoins, not CEX-listed.
        """
        token = _make_token(
            liquidity_usd=0,
            volume_24h_usd=50_000_000,
            market_cap_usd=1_000_000_000,
            cg_trending_rank=None,
            chain="coingecko",
            holder_growth_1h=0,
            token_age_days=365,
            social_mentions_24h=0,
        )
        points, signals = score(token, _settings())
        assert "DISQUALIFIED_LOW_LIQUIDITY" not in signals


class TestBuyPressureConfigurable:
    """BL-011: Buy pressure threshold is configurable."""

    def test_custom_buy_pressure_threshold(self):
        """Higher threshold -> signal doesn't fire at 66%."""
        token = _make_token(
            txns_h1_buys=66, txns_h1_sells=34,
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
            chain="ethereum",
        )
        # Default 0.65 -> fires (66% > 65%)
        _, signals_default = score(token, _settings())
        assert "buy_pressure" in signals_default

        # Custom 0.70 -> doesn't fire (66% < 70%)
        _, signals_strict = score(token, _settings(BUY_PRESSURE_THRESHOLD=0.70))
        assert "buy_pressure" not in signals_strict


class TestAgeBellCurveBoundaries:
    """BL-012: Age bell curve boundary conditions."""

    def test_exactly_3_hours(self):
        """Exactly 3h -> should get 8 pts (3-12h band)."""
        token = _make_token(
            token_age_days=3.0 / 24, volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0, social_mentions_24h=0,
            chain="ethereum",
        )
        _, signals = score(token, _settings())
        assert "token_age" in signals

    def test_exactly_12_hours(self):
        """Exactly 12h -> peak band (15 pts)."""
        token = _make_token(
            token_age_days=12.0 / 24, volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0, social_mentions_24h=0,
            chain="ethereum",
        )
        _, signals = score(token, _settings())
        assert "token_age" in signals
        # 12h is in the 12-48h peak band

    def test_exactly_48_hours(self):
        """Exactly 48h -> still in peak band."""
        token = _make_token(
            token_age_days=2.0, volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0, social_mentions_24h=0,
            chain="ethereum",
        )
        _, signals = score(token, _settings())
        assert "token_age" in signals

    def test_just_over_48_hours(self):
        """48h + 1min -> declining band (5 pts)."""
        token = _make_token(
            token_age_days=2.01, volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0, social_mentions_24h=0,
            chain="ethereum",
        )
        points, signals = score(token, _settings())
        assert "token_age" in signals
        # 5 pts raw -> normalized=int(5*100/183)=2
        assert points == 2

    def test_exactly_7_days(self):
        """Exactly 7d -> still in declining band."""
        token = _make_token(
            token_age_days=7.0, volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0, social_mentions_24h=0,
            chain="ethereum",
        )
        _, signals = score(token, _settings())
        assert "token_age" in signals

    def test_just_over_7_days(self):
        """7d + 1h -> 0 pts."""
        token = _make_token(
            token_age_days=7.05, volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0, social_mentions_24h=0,
            chain="ethereum",
        )
        _, signals = score(token, _settings())
        assert "token_age" not in signals


class TestCoOccurrenceConfigurable:
    """BL-014: Co-occurrence multiplier configuration."""

    def test_configurable_min_signals(self):
        """Custom CO_OCCURRENCE_MIN_SIGNALS=4 -> 3 signals get no multiplier."""
        token = _make_token(
            volume_24h_usd=120000, liquidity_usd=20000,
            holder_growth_1h=25, market_cap_usd=50000,
            token_age_days=1.0, social_mentions_24h=0,
            chain="ethereum",
        )
        # With default (3) -> multiplier fires
        p_default, s_default = score(token, _settings())
        assert len(s_default) >= 3

        # With min_signals=5 -> no multiplier
        p_strict, _ = score(token, _settings(CO_OCCURRENCE_MIN_SIGNALS=5))
        assert p_strict < p_default  # lower without multiplier

    def test_configurable_multiplier_value(self):
        """Custom CO_OCCURRENCE_MULTIPLIER changes the boost."""
        token = _make_token(
            volume_24h_usd=120000, liquidity_usd=20000,
            holder_growth_1h=25, market_cap_usd=50000,
            token_age_days=1.0, social_mentions_24h=0,
            chain="ethereum",
        )
        p_115, _ = score(token, _settings(CO_OCCURRENCE_MULTIPLIER=1.15))
        p_130, _ = score(token, _settings(CO_OCCURRENCE_MULTIPLIER=1.30))
        assert p_130 > p_115

    def test_four_signals_same_multiplier_as_three(self):
        """Multiplier is flat at 1.15x whether 3 or 4 signals fire."""
        token_3 = _make_token(
            volume_24h_usd=120000, liquidity_usd=20000,
            holder_growth_1h=25, market_cap_usd=50000,
            token_age_days=30, social_mentions_24h=0,
            chain="ethereum",
        )
        token_4 = _make_token(
            volume_24h_usd=120000, liquidity_usd=20000,
            holder_growth_1h=25, market_cap_usd=50000,
            token_age_days=1.0, social_mentions_24h=0,
            chain="ethereum",
        )
        p3, s3 = score(token_3, _settings())
        p4, s4 = score(token_4, _settings())
        # Both should have multiplier applied (>=3 signals)
        assert len(s3) >= 3
        assert len(s4) >= 3


class TestNormalization:
    """BL-016: Score normalization correctness."""

    def test_max_raw_constant_matches_actual_max(self):
        """SCORER_MAX_RAW should equal sum of all max signal points."""
        from scout.scorer import SCORER_MAX_RAW
        # 30+8+25+15+15+15+20+25+15+5+10 = 183
        assert SCORER_MAX_RAW == 183

    def test_single_signal_normalized_correctly(self):
        """A single 30-point signal normalizes to int(30*100/183)=16."""
        token = _make_token(
            volume_24h_usd=120000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
            chain="ethereum",
        )
        points, signals = score(token, _settings())
        assert len(signals) == 1
        assert points == int(30 * 100 / 183)

    def test_all_signals_normalize_to_100(self):
        """All signals firing -> capped at 100 after multiplier."""
        token = _make_token(
            volume_24h_usd=120000, liquidity_usd=20000,
            market_cap_usd=50000, holder_growth_1h=25,
            token_age_days=1.0, social_mentions_24h=60,
            txns_h1_buys=70, txns_h1_sells=30,
            price_change_1h=8.0, price_change_24h=12.0,
            vol_7d_avg=10000, cg_trending_rank=5,
            chain="solana",
        )
        points, _ = score(token, _settings())
        assert points == 100

    def test_zero_signals_normalize_to_zero(self):
        """No signals -> 0 points."""
        token = _make_token(
            volume_24h_usd=1000, liquidity_usd=20000,
            market_cap_usd=999999, holder_growth_1h=0,
            token_age_days=30, social_mentions_24h=0,
            chain="ethereum",
        )
        points, _ = score(token, _settings())
        assert points == 0


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
