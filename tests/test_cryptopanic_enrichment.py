"""Tests for CryptoPanic candidate enrichment (tagging)."""

import pytest

from scout.config import Settings
from scout.models import CandidateToken
from scout.news.cryptopanic import enrich_candidates_with_news
from scout.news.schemas import CryptoPanicPost


def _settings(**overrides):
    defaults = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        CRYPTOPANIC_MACRO_MIN_CURRENCIES=4,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _token(ticker: str) -> CandidateToken:
    return CandidateToken(
        contract_address="0x" + ticker.lower(),
        chain="ethereum",
        token_name=ticker,
        ticker=ticker,
    )


def _post(pid: int, published_at: str, currencies: list[str], pos=0, neg=0):
    return CryptoPanicPost(
        post_id=pid,
        title="t",
        url=f"u/{pid}",
        published_at=published_at,
        currencies=currencies,
        votes_positive=pos,
        votes_negative=neg,
    )


def test_no_posts_leaves_fields_none():
    s = _settings()
    tokens = [_token("PEPE")]
    out = enrich_candidates_with_news(tokens, [], s)
    assert out[0].news_count_24h is None
    assert out[0].latest_news_sentiment is None
    assert out[0].macro_news_flag is None
    assert out[0].news_tag_confidence is None


def test_no_match_leaves_fields_none():
    s = _settings()
    tokens = [_token("PEPE")]
    posts = [_post(1, "2026-04-20T10:00:00Z", ["DOGE"], pos=5)]
    out = enrich_candidates_with_news(tokens, posts, s)
    assert out[0].news_count_24h is None
    assert out[0].news_tag_confidence is None


def test_case_insensitive_match():
    s = _settings()
    tokens = [_token("PEPE")]
    posts = [_post(1, "2026-04-20T10:00:00Z", ["pepe"], pos=5)]
    out = enrich_candidates_with_news(tokens, posts, s)
    assert out[0].news_count_24h == 1
    assert out[0].latest_news_sentiment == "bullish"
    assert out[0].macro_news_flag is False
    assert out[0].news_tag_confidence == "ticker_only"


def test_counts_multiple_matches():
    s = _settings()
    tokens = [_token("BTC")]
    posts = [
        _post(1, "2026-04-20T10:00:00Z", ["BTC"], pos=5, neg=1),
        _post(2, "2026-04-20T08:00:00Z", ["BTC"], pos=0, neg=3),
    ]
    out = enrich_candidates_with_news(tokens, posts, s)
    assert out[0].news_count_24h == 2


def test_sentiment_comes_from_most_recent_post():
    s = _settings()
    tokens = [_token("BTC")]
    posts = [
        _post(1, "2026-04-20T08:00:00Z", ["BTC"], pos=5, neg=0),  # older, bullish
        _post(2, "2026-04-20T10:00:00Z", ["BTC"], pos=0, neg=5),  # newer, bearish
    ]
    out = enrich_candidates_with_news(tokens, posts, s)
    assert out[0].latest_news_sentiment == "bearish"


def test_macro_flag_true_if_any_matched_post_is_macro():
    s = _settings(CRYPTOPANIC_MACRO_MIN_CURRENCIES=4)
    tokens = [_token("ETH")]
    posts = [
        _post(1, "2026-04-20T10:00:00Z", ["ETH"]),  # token-specific
        _post(2, "2026-04-20T09:00:00Z", ["BTC", "ETH", "SOL", "AVAX"]),  # macro
    ]
    out = enrich_candidates_with_news(tokens, posts, s)
    assert out[0].macro_news_flag is True


def test_empty_currencies_list_is_macro():
    s = _settings()
    tokens = [_token("ETH")]
    posts = [_post(1, "2026-04-20T10:00:00Z", [])]  # no currencies → macro → no match
    out = enrich_candidates_with_news(tokens, posts, s)
    # Macro post with no currencies doesn't match any specific ticker
    assert out[0].news_count_24h is None


def test_preserves_existing_fields():
    s = _settings()
    tokens = [
        CandidateToken(
            contract_address="0xbtc",
            chain="ethereum",
            token_name="BTC",
            ticker="BTC",
            quant_score=42,
        )
    ]
    posts = [_post(1, "2026-04-20T10:00:00Z", ["BTC"], pos=5)]
    out = enrich_candidates_with_news(tokens, posts, s)
    assert out[0].quant_score == 42  # untouched
