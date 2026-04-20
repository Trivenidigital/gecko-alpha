from datetime import datetime, timezone
from scout.perp.anomaly import classify_funding_flip, classify_oi_spike


def test_funding_flip_positive_to_negative():
    a = classify_funding_flip(
        prev_rate=0.0002,
        new_rate=-0.0001,
        exchange="binance",
        symbol="BTCUSDT",
        ticker="BTC",
        observed_at=datetime.now(timezone.utc),
        min_magnitude_pct=0.01,
    )
    assert a is not None and a.kind == "funding_flip"


def test_funding_flip_below_magnitude_gate():
    assert (
        classify_funding_flip(
            prev_rate=0.00009,
            new_rate=-0.00001,
            exchange="binance",
            symbol="BTCUSDT",
            ticker="BTC",
            observed_at=datetime.now(timezone.utc),
            min_magnitude_pct=0.05,
        )
        is None
    )


def test_funding_flip_same_sign():
    assert (
        classify_funding_flip(
            prev_rate=0.0001,
            new_rate=0.0002,
            exchange="binance",
            symbol="BTCUSDT",
            ticker="BTC",
            observed_at=datetime.now(timezone.utc),
            min_magnitude_pct=0.01,
        )
        is None
    )


def test_funding_flip_no_prev():
    assert (
        classify_funding_flip(
            prev_rate=None,
            new_rate=0.0001,
            exchange="binance",
            symbol="BTCUSDT",
            ticker="BTC",
            observed_at=datetime.now(timezone.utc),
            min_magnitude_pct=0.01,
        )
        is None
    )


def test_oi_spike_triggered():
    a = classify_oi_spike(
        current_oi=400.0,
        baseline_oi=100.0,
        exchange="binance",
        symbol="BTCUSDT",
        ticker="BTC",
        observed_at=datetime.now(timezone.utc),
        sample_count=40,
        min_samples=30,
        spike_ratio=3.0,
    )
    assert a is not None
    assert abs(a.magnitude - 4.0) < 1e-9


def test_oi_spike_cold_warmup_gate():
    assert (
        classify_oi_spike(
            current_oi=400.0,
            baseline_oi=100.0,
            exchange="binance",
            symbol="BTCUSDT",
            ticker="BTC",
            observed_at=datetime.now(timezone.utc),
            sample_count=5,
            min_samples=30,
            spike_ratio=3.0,
        )
        is None
    )


def test_oi_spike_below_ratio():
    assert (
        classify_oi_spike(
            current_oi=200.0,
            baseline_oi=100.0,
            exchange="binance",
            symbol="BTCUSDT",
            ticker="BTC",
            observed_at=datetime.now(timezone.utc),
            sample_count=40,
            min_samples=30,
            spike_ratio=3.0,
        )
        is None
    )


def test_oi_spike_no_baseline():
    assert (
        classify_oi_spike(
            current_oi=400.0,
            baseline_oi=None,
            exchange="binance",
            symbol="BTCUSDT",
            ticker="BTC",
            observed_at=datetime.now(timezone.utc),
            sample_count=40,
            min_samples=30,
            spike_ratio=3.0,
        )
        is None
    )


def test_funding_flip_nan_new_rate_returns_none():
    """NaN and Inf new_rate must be rejected before any math."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert (
            classify_funding_flip(
                prev_rate=0.0002,
                new_rate=bad,
                exchange="binance",
                symbol="BTCUSDT",
                ticker="BTC",
                observed_at=datetime.now(timezone.utc),
                min_magnitude_pct=0.01,
            )
            is None
        )


def test_funding_flip_nan_prev_rate_returns_none():
    """NaN prev_rate must be rejected (sign comparison would silently misbehave)."""
    for bad in (float("nan"), float("inf")):
        assert (
            classify_funding_flip(
                prev_rate=bad,
                new_rate=-0.0002,
                exchange="binance",
                symbol="BTCUSDT",
                ticker="BTC",
                observed_at=datetime.now(timezone.utc),
                min_magnitude_pct=0.01,
            )
            is None
        )


def test_oi_spike_nan_current_oi_returns_none():
    """NaN and Inf current_oi must be rejected before ratio computation."""
    for bad in (float("nan"), float("inf")):
        assert (
            classify_oi_spike(
                current_oi=bad,
                baseline_oi=100.0,
                exchange="binance",
                symbol="BTCUSDT",
                ticker="BTC",
                observed_at=datetime.now(timezone.utc),
                sample_count=40,
                min_samples=30,
                spike_ratio=3.0,
            )
            is None
        )
