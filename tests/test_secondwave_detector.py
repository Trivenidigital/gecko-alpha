"""Tests for second-wave scoring and detection."""
from datetime import datetime, timezone

import pytest

from scout.secondwave.detector import (
    build_secondwave_candidate,
    score_reaccumulation,
)


_SW_DEFAULTS = dict(
    SECONDWAVE_MIN_DRAWDOWN_PCT=30.0,
    SECONDWAVE_MIN_RECOVERY_PCT=70.0,
    SECONDWAVE_VOL_PICKUP_RATIO=2.0,
    SECONDWAVE_ALERT_THRESHOLD=50,
)


def test_score_full_house(settings_factory):
    settings = settings_factory(**_SW_DEFAULTS)
    candidate = {"peak_quant_score": 80}
    score, signals = score_reaccumulation(
        candidate,
        current_price=0.8,
        current_volume=5000.0,
        current_market_cap=1_200_000.0,
        alert_market_cap=2_000_000.0,  # 40% drawdown
        alert_price=1.0,                # 80% recovery
        volume_history=[1000.0, 1000.0, 1000.0],  # 5x pickup
        settings=settings,
    )
    assert score == 100
    assert set(signals) == {
        "sufficient_drawdown",
        "price_recovery",
        "volume_pickup",
        "strong_prior_signal",
    }


def test_score_below_threshold_dex_token(settings_factory):
    settings = settings_factory(**_SW_DEFAULTS)
    candidate = {"peak_quant_score": 78}
    score, signals = score_reaccumulation(
        candidate,
        current_price=1.0,
        current_volume=None,
        current_market_cap=1_900_000.0,  # only 5% drawdown — no signal
        alert_market_cap=2_000_000.0,
        alert_price=1.0,
        volume_history=[],
        settings=settings,
    )
    # price_recovery (35) + strong_prior_signal (15) = 50 -> at threshold boundary
    assert "sufficient_drawdown" not in signals
    assert "price_recovery" in signals
    assert "strong_prior_signal" in signals
    assert score == 50


def test_score_insufficient_volume_history(settings_factory):
    settings = settings_factory(**_SW_DEFAULTS)
    candidate = {"peak_quant_score": 80}
    score, signals = score_reaccumulation(
        candidate,
        current_price=0.8,
        current_volume=5000.0,
        current_market_cap=1_200_000.0,
        alert_market_cap=2_000_000.0,
        alert_price=1.0,
        volume_history=[1000.0],  # only 1 snapshot — skip volume_pickup
        settings=settings,
    )
    assert "volume_pickup" not in signals
    assert score == 80  # 30 + 35 + 15


def test_score_weak_drawdown_no_recovery(settings_factory):
    settings = settings_factory(**_SW_DEFAULTS)
    candidate = {"peak_quant_score": 50}  # too weak for strong_prior
    score, signals = score_reaccumulation(
        candidate,
        current_price=0.5,   # 50% of alert — below recovery threshold
        current_volume=None,
        current_market_cap=1_900_000.0,  # 5% drawdown — below
        alert_market_cap=2_000_000.0,
        alert_price=1.0,
        volume_history=[],
        settings=settings,
    )
    assert score == 0
    assert signals == []


def test_score_zero_alert_price_safe(settings_factory):
    settings = settings_factory(**_SW_DEFAULTS)
    candidate = {"peak_quant_score": 80}
    score, signals = score_reaccumulation(
        candidate,
        current_price=0.8,
        current_volume=None,
        current_market_cap=1_200_000.0,
        alert_market_cap=2_000_000.0,
        alert_price=0.0,  # division guard
        volume_history=[],
        settings=settings,
    )
    assert "price_recovery" not in signals  # guarded
    assert "sufficient_drawdown" in signals


def test_build_secondwave_candidate():
    scan_row = {
        "contract_address": "0xabc",
        "chain": "ethereum",
        "token_name": "Tok",
        "ticker": "TK",
        "coingecko_id": None,
        "peak_quant_score": 80,
        "alert_market_cap": 2_000_000.0,
        "alert_price": 1.0,
        "alerted_at": datetime.now(timezone.utc).isoformat(),
    }
    cand = build_secondwave_candidate(
        scan_row=scan_row,
        score=85,
        signals=["sufficient_drawdown", "price_recovery"],
        current_price=0.8,
        current_volume=5000.0,
        current_market_cap=1_200_000.0,
        volume_history=[1000.0, 1000.0, 1000.0],
        price_is_stale=False,
    )
    assert cand["contract_address"] == "0xabc"
    assert cand["reaccumulation_score"] == 85
    assert cand["price_vs_alert_pct"] == 80.0
    assert cand["price_drop_from_peak_pct"] == -40.0
    assert cand["volume_vs_cooldown_avg"] == 5.0
    assert cand["price_is_stale"] is False
    assert cand["days_since_first_seen"] >= 0
