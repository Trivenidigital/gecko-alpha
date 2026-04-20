"""Tests for BL-053 gated CryptoPanic scoring signal."""

from scout.scorer import score


def _tagged_token(token_factory, **extra):
    defaults = dict(
        liquidity_usd=50000.0,
        volume_24h_usd=50000.0,
        news_count_24h=2,
        latest_news_sentiment="bullish",
        macro_news_flag=False,
        news_tag_confidence="ticker_only",
    )
    defaults.update(extra)
    return token_factory(**defaults)


def test_signal_silent_when_flag_false(settings_factory, token_factory):
    s = settings_factory(CRYPTOPANIC_SCORING_ENABLED=False)
    token = _tagged_token(token_factory)
    _, signals = score(token, s)
    assert "cryptopanic_bullish" not in signals


def test_signal_fires_when_flag_true_and_conditions_met(
    settings_factory, token_factory
):
    s = settings_factory(CRYPTOPANIC_SCORING_ENABLED=True)
    token = _tagged_token(token_factory)
    _, signals = score(token, s)
    assert "cryptopanic_bullish" in signals


def test_signal_silent_when_bearish(settings_factory, token_factory):
    s = settings_factory(CRYPTOPANIC_SCORING_ENABLED=True)
    token = _tagged_token(token_factory, latest_news_sentiment="bearish")
    _, signals = score(token, s)
    assert "cryptopanic_bullish" not in signals


def test_signal_silent_when_macro(settings_factory, token_factory):
    s = settings_factory(CRYPTOPANIC_SCORING_ENABLED=True)
    token = _tagged_token(token_factory, macro_news_flag=True)
    _, signals = score(token, s)
    assert "cryptopanic_bullish" not in signals


def test_signal_silent_when_no_news(settings_factory, token_factory):
    s = settings_factory(CRYPTOPANIC_SCORING_ENABLED=True)
    token = _tagged_token(token_factory, news_count_24h=0)
    _, signals = score(token, s)
    assert "cryptopanic_bullish" not in signals


def test_score_still_bounded_100_even_when_flag_active(settings_factory, token_factory):
    """Guard: enabling the gated signal without bumping SCORER_MAX_RAW should
    never produce a score > 100 because of the min(points, 100) ceiling."""
    s = settings_factory(CRYPTOPANIC_SCORING_ENABLED=True)
    token = _tagged_token(token_factory)
    points, _ = score(token, s)
    assert 0 <= points <= 100
