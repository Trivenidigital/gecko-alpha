"""Tests for MiroFish seed builder."""

from datetime import datetime, timezone, timedelta

from scout.mirofish.seed_builder import build_seed


def test_build_seed_returns_required_keys(token_factory):
    token = token_factory(
        token_name="TestCoin",
        social_mentions_24h=45,
        first_seen_at=datetime.now(timezone.utc) - timedelta(hours=3),
    )
    seed = build_seed(token)

    assert "token_name" in seed
    assert "ticker" in seed
    assert "chain" in seed
    assert "market_cap" in seed
    assert "age_hours" in seed
    assert "concept_description" in seed
    assert "social_snippets" in seed
    assert "prompt" in seed


def test_build_seed_values(token_factory):
    token = token_factory(
        token_name="MoonCoin", ticker="MOON", chain="ethereum", market_cap_usd=100000
    )
    seed = build_seed(token)

    assert seed["token_name"] == "MoonCoin"
    assert seed["ticker"] == "MOON"
    assert seed["chain"] == "ethereum"
    assert seed["market_cap"] == 100000


def test_build_seed_prompt_format(token_factory):
    token = token_factory(
        token_name="TestCoin", ticker="TST", chain="solana", market_cap_usd=50000
    )
    seed = build_seed(token)

    prompt = seed["prompt"]
    assert "Token: TestCoin (TST) on solana" in prompt
    assert "Market cap: $50,000" in prompt
    assert "Score the viral narrative potential" in prompt


def test_build_seed_age_hours(token_factory):
    token = token_factory(token_age_days=2.5)
    seed = build_seed(token)
    assert seed["age_hours"] == 60  # 2.5 days * 24


def test_build_seed_no_social_mentions(token_factory):
    token = token_factory(social_mentions_24h=0)
    seed = build_seed(token)
    assert seed["social_snippets"] == "None detected"


def test_build_seed_includes_signals_and_confidence(token_factory):
    """Seed includes signals_fired and signal_confidence when provided."""
    token = token_factory()
    signals = ["vol_liq_ratio", "holder_growth", "market_cap_range"]
    seed = build_seed(token, signals_fired=signals, signal_confidence="HIGH")

    assert seed["signals_fired"] == signals
    assert seed["signal_confidence"] == "HIGH"


def test_build_seed_omits_signals_when_not_provided(token_factory):
    """Seed omits signals fields when not provided."""
    token = token_factory()
    seed = build_seed(token)

    assert "signals_fired" not in seed
    assert "signal_confidence" not in seed
