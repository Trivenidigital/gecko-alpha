"""Tests for BL-053 CandidateToken field additions."""

from scout.models import CandidateToken


def test_cryptopanic_fields_default_to_none():
    t = CandidateToken(
        contract_address="0xtest",
        chain="ethereum",
        token_name="Test",
        ticker="TST",
    )
    assert t.news_count_24h is None
    assert t.latest_news_sentiment is None
    assert t.macro_news_flag is None
    assert t.news_tag_confidence is None


def test_cryptopanic_fields_accept_values():
    t = CandidateToken(
        contract_address="0xtest",
        chain="ethereum",
        token_name="Test",
        ticker="TST",
        news_count_24h=3,
        latest_news_sentiment="bullish",
        macro_news_flag=False,
        news_tag_confidence="ticker_only",
    )
    assert t.news_count_24h == 3
    assert t.latest_news_sentiment == "bullish"
    assert t.macro_news_flag is False
    assert t.news_tag_confidence == "ticker_only"


def test_cryptopanic_fields_literal_rejects_invalid():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CandidateToken(
            contract_address="0xtest",
            chain="ethereum",
            token_name="Test",
            ticker="TST",
            latest_news_sentiment="excited",  # not in Literal
        )


def test_cryptopanic_fields_roundtrip_serialization():
    t = CandidateToken(
        contract_address="0xtest",
        chain="ethereum",
        token_name="Test",
        ticker="TST",
        news_count_24h=2,
        latest_news_sentiment="bearish",
        macro_news_flag=True,
        news_tag_confidence="ticker_only",
    )
    dumped = t.model_dump()
    restored = CandidateToken(**dumped)
    assert restored.news_count_24h == 2
    assert restored.latest_news_sentiment == "bearish"
    assert restored.macro_news_flag is True
    assert restored.news_tag_confidence == "ticker_only"


def test_token_factory_default_has_none_news_fields(token_factory):
    """conftest token_factory() should not populate the new fields."""
    t = token_factory()
    assert t.news_count_24h is None
    assert t.latest_news_sentiment is None
    assert t.macro_news_flag is None
    assert t.news_tag_confidence is None
