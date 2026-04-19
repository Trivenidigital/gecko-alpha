"""Tests for SecondWaveCandidate model."""

from datetime import datetime, timezone

from scout.secondwave.models import SecondWaveCandidate


def test_secondwave_candidate_minimal():
    cand = SecondWaveCandidate(
        contract_address="0xabc",
        chain="ethereum",
        token_name="Test Token",
        ticker="TEST",
        peak_quant_score=75,
        peak_signals_fired=["momentum_ratio", "vol_acceleration"],
        first_seen_at=datetime.now(timezone.utc),
        original_market_cap=1_000_000.0,
        alert_market_cap=2_000_000.0,
        days_since_first_seen=5.2,
        price_drop_from_peak_pct=-42.5,
        current_price=0.00012,
        current_market_cap=1_200_000.0,
        current_volume_24h=500_000.0,
        price_vs_alert_pct=75.0,
        volume_vs_cooldown_avg=3.1,
        reaccumulation_score=85,
        reaccumulation_signals=[
            "sufficient_drawdown",
            "price_recovery",
            "volume_pickup",
            "strong_prior_signal",
        ],
        detected_at=datetime.now(timezone.utc),
    )
    assert cand.coingecko_id is None
    assert cand.alerted_at is None
    assert cand.reaccumulation_score == 85
    assert "price_recovery" in cand.reaccumulation_signals


def test_secondwave_candidate_with_coingecko_id():
    cand = SecondWaveCandidate(
        contract_address="0xdef",
        chain="solana",
        token_name="CG Token",
        ticker="CGT",
        coingecko_id="cg-token",
        peak_quant_score=80,
        peak_signals_fired=[],
        first_seen_at=datetime.now(timezone.utc),
        original_market_cap=500_000.0,
        alert_market_cap=1_500_000.0,
        days_since_first_seen=7.0,
        price_drop_from_peak_pct=-50.0,
        current_price=0.5,
        current_market_cap=750_000.0,
        current_volume_24h=100_000.0,
        price_vs_alert_pct=60.0,
        volume_vs_cooldown_avg=2.0,
        reaccumulation_score=65,
        reaccumulation_signals=["sufficient_drawdown", "strong_prior_signal"],
        detected_at=datetime.now(timezone.utc),
    )
    assert cand.coingecko_id == "cg-token"


def test_secondwave_candidate_stale_price():
    """price_is_stale=True must round-trip through the model."""
    cand = SecondWaveCandidate(
        contract_address="0xstale",
        chain="ethereum",
        token_name="Stale Token",
        ticker="STL",
        peak_quant_score=70,
        peak_signals_fired=["momentum_ratio"],
        first_seen_at=datetime.now(timezone.utc),
        original_market_cap=1_000_000.0,
        alert_market_cap=2_000_000.0,
        days_since_first_seen=6.0,
        price_drop_from_peak_pct=0.0,
        current_price=1.0,
        current_market_cap=2_000_000.0,
        current_volume_24h=None,
        price_vs_alert_pct=100.0,
        volume_vs_cooldown_avg=0.0,
        price_is_stale=True,
        reaccumulation_score=50,
        reaccumulation_signals=["price_recovery", "strong_prior_signal"],
        detected_at=datetime.now(timezone.utc),
    )
    assert cand.price_is_stale is True
    assert cand.current_volume_24h is None
