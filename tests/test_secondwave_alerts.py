"""Tests for second-wave Telegram alert formatting."""
from datetime import datetime, timezone

from scout.secondwave.alerts import format_secondwave_alert


def _base_candidate() -> dict:
    return {
        "contract_address": "0xabc",
        "chain": "ethereum",
        "token_name": "Test Token",
        "ticker": "TEST",
        "coingecko_id": None,
        "peak_quant_score": 80,
        "peak_signals_fired": ["momentum_ratio", "vol_acceleration"],
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
        "original_alert_at": datetime.now(timezone.utc).isoformat(),
        "original_market_cap": 1_000_000.0,
        "alert_market_cap": 2_000_000.0,
        "days_since_first_seen": 5.3,
        "price_drop_from_peak_pct": -40.0,
        "current_price": 0.8,
        "current_market_cap": 1_200_000.0,
        "current_volume_24h": 500_000.0,
        "price_vs_alert_pct": 80.0,
        "volume_vs_cooldown_avg": 3.1,
        "price_is_stale": False,
        "reaccumulation_score": 85,
        "reaccumulation_signals": ["sufficient_drawdown", "price_recovery", "strong_prior_signal"],
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "alerted_at": datetime.now(timezone.utc).isoformat(),
    }


def test_format_basic_alert_contains_all_sections():
    msg = format_secondwave_alert(_base_candidate())
    assert "\U0001F504" in msg  # refresh emoji
    assert "Second Wave" in msg
    assert "Test Token" in msg
    assert "TEST" in msg
    assert "85/100" in msg
    assert "sufficient_drawdown" in msg
    assert "price_recovery" in msg
    assert "-40.0" in msg
    assert "80.0" in msg
    assert "RESEARCH ONLY" in msg


def test_format_stale_price_marker():
    c = _base_candidate()
    c["price_is_stale"] = True
    msg = format_secondwave_alert(c)
    assert "stale" in msg.lower()


def test_format_missing_optional_fields():
    c = _base_candidate()
    c["current_volume_24h"] = None
    c["peak_signals_fired"] = []
    msg = format_secondwave_alert(c)
    assert "Test Token" in msg
    assert "n/a" in msg.lower() or "0" in msg
