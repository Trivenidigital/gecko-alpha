"""Tests for BL-051 velocity_boost signal."""

from scout.scorer import score


def test_velocity_boost_fires_above_threshold(token_factory, settings_factory):
    settings = settings_factory(MIN_BOOST_TOTAL_AMOUNT=500.0)
    token = token_factory(
        liquidity_usd=20_000,  # above MIN_LIQUIDITY_USD default
        boost_total_amount=1500.0,
        boost_rank=1,
    )
    points, signals = score(token, settings)
    assert "velocity_boost" in signals


def test_velocity_boost_silent_below_threshold(token_factory, settings_factory):
    settings = settings_factory(MIN_BOOST_TOTAL_AMOUNT=500.0)
    token = token_factory(
        liquidity_usd=20_000,
        boost_total_amount=100.0,
    )
    points, signals = score(token, settings)
    assert "velocity_boost" not in signals


def test_velocity_boost_silent_when_none(token_factory, settings_factory):
    settings = settings_factory(MIN_BOOST_TOTAL_AMOUNT=500.0)
    token = token_factory(
        liquidity_usd=20_000,
        boost_total_amount=None,
    )
    points, signals = score(token, settings)
    assert "velocity_boost" not in signals


def test_velocity_boost_at_threshold_fires(token_factory, settings_factory):
    settings = settings_factory(MIN_BOOST_TOTAL_AMOUNT=500.0)
    token = token_factory(
        liquidity_usd=20_000,
        boost_total_amount=500.0,
    )
    points, signals = score(token, settings)
    # Condition is `>= MIN_BOOST_TOTAL_AMOUNT` per spec §6.
    assert "velocity_boost" in signals


def test_velocity_boost_isolated_score_is_9(token_factory, settings_factory):
    """A token whose ONLY signal is velocity_boost scores int(20*100/203) == 9
    (no co-occurrence multiplier — only 1 signal)."""
    settings = settings_factory(MIN_BOOST_TOTAL_AMOUNT=500.0)

    base_kwargs = dict(
        contract_address="0xdiff",
        chain="ethereum",  # avoid solana_bonus
        token_name="X",
        ticker="X",
        market_cap_usd=0,
        liquidity_usd=20_000,
        volume_24h_usd=0,  # no vol_liq_ratio
        holder_count=0,
        holder_growth_1h=0,
        token_age_days=100,  # past peak, no token_age signal
    )
    t_no_boost = token_factory(**base_kwargs, boost_total_amount=None)
    t_boost = token_factory(**base_kwargs, boost_total_amount=1500.0)

    pts_no, sig_no = score(t_no_boost, settings)
    pts_yes, sig_yes = score(t_boost, settings)

    assert "velocity_boost" not in sig_no
    assert sig_yes == ["velocity_boost"]
    assert pts_no == 0
    assert pts_yes == int(20 * 100 / 203)  # == 9
