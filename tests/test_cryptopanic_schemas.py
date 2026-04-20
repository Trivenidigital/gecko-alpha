"""Tests for CryptoPanicPost schema + classification helpers."""

from scout.news.schemas import (
    CryptoPanicPost,
    classify_sentiment,
    classify_macro,
    parse_post,
)


def test_parse_post_minimal():
    raw = {
        "id": 123,
        "title": "Hello",
        "url": "https://cryptopanic.com/news/123",
        "published_at": "2026-04-20T12:00:00Z",
        "currencies": [{"code": "BTC", "title": "Bitcoin"}],
        "votes": {"positive": 5, "negative": 1},
    }
    post = parse_post(raw)
    assert post.post_id == 123
    assert post.title == "Hello"
    assert post.currencies == ["BTC"]
    assert post.votes_positive == 5
    assert post.votes_negative == 1


def test_parse_post_currencies_null_treated_as_empty():
    raw = {
        "id": 9,
        "title": "t",
        "url": "u",
        "published_at": "2026-04-20T00:00:00Z",
        "currencies": None,
        "votes": {},
    }
    post = parse_post(raw)
    assert post.currencies == []


def test_parse_post_skips_missing_code():
    raw = {
        "id": 9,
        "title": "t",
        "url": "u",
        "published_at": "2026-04-20T00:00:00Z",
        "currencies": [{"code": "BTC"}, {"title": "no code"}, {"code": ""}],
        "votes": {},
    }
    post = parse_post(raw)
    assert post.currencies == ["BTC"]


def test_parse_post_missing_required_returns_none():
    raw = {"id": 9}  # no title/url/published_at
    assert parse_post(raw) is None


def test_sentiment_bullish():
    assert classify_sentiment(positive=5, negative=1) == "bullish"


def test_sentiment_bearish():
    assert classify_sentiment(positive=0, negative=3) == "bearish"


def test_sentiment_neutral_when_both_zero():
    assert classify_sentiment(positive=0, negative=0) == "neutral"


def test_sentiment_neutral_at_tie():
    assert classify_sentiment(positive=4, negative=4) == "neutral"


def test_sentiment_neutral_when_delta_below_threshold():
    assert classify_sentiment(positive=3, negative=2) == "neutral"  # delta=1 < 2


def test_sentiment_exact_threshold_bullish():
    assert classify_sentiment(positive=3, negative=1) == "bullish"  # delta=2 meets >=


def test_macro_empty_currencies_is_macro():
    assert classify_macro([], threshold=4) is True


def test_macro_below_threshold_not_macro():
    assert classify_macro(["BTC", "ETH", "SOL"], threshold=4) is False


def test_macro_at_threshold_is_macro():
    assert classify_macro(["BTC", "ETH", "SOL", "AVAX"], threshold=4) is True
