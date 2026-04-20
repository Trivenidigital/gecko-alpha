import pytest
from datetime import datetime, timezone
from pydantic import ValidationError
from scout.perp.schemas import PerpTick, PerpAnomaly, Exchange, AnomalyKind


def test_perp_tick_requires_upper_ticker_charset():
    with pytest.raises(ValidationError):
        PerpTick(
            exchange="binance",
            symbol="BTCUSDT",
            ticker="btc-lower",
            timestamp=datetime.now(timezone.utc),
        )


def test_perp_tick_rejects_oversized_ticker():
    with pytest.raises(ValidationError):
        PerpTick(
            exchange="binance",
            symbol="BTCUSDT",
            ticker="A" * 21,
            timestamp=datetime.now(timezone.utc),
        )


def test_perp_tick_rejects_oversized_symbol():
    with pytest.raises(ValidationError):
        PerpTick(
            exchange="binance",
            symbol="X" * 33,
            ticker="BTC",
            timestamp=datetime.now(timezone.utc),
        )


def test_perp_tick_happy_path():
    t = PerpTick(
        exchange="binance",
        symbol="BTCUSDT",
        ticker="BTC",
        funding_rate=0.0001,
        mark_price=50000.0,
        open_interest=12345.0,
        timestamp=datetime.now(timezone.utc),
    )
    assert t.exchange == "binance"
    assert t.ticker == "BTC"


def test_perp_anomaly_happy_path():
    a = PerpAnomaly(
        exchange="bybit",
        symbol="DOGEUSDT",
        ticker="DOGE",
        kind="oi_spike",
        magnitude=4.2,
        baseline=1.0,
        observed_at=datetime.now(timezone.utc),
    )
    assert a.kind == "oi_spike"


def test_perp_tick_rejects_unknown_exchange():
    from datetime import datetime, timezone
    import pytest
    from pydantic import ValidationError
    from scout.perp.schemas import PerpTick

    with pytest.raises(ValidationError):
        PerpTick(
            exchange="okx",
            symbol="BTCUSDT",
            ticker="BTC",
            timestamp=datetime.now(timezone.utc),
        )


def test_perp_tick_rejects_nan_funding_rate():
    """Schema must reject NaN in funding_rate (allow_inf_nan=False guard)."""
    import math

    with pytest.raises(ValidationError):
        PerpTick(
            exchange="binance",
            symbol="BTCUSDT",
            ticker="BTC",
            funding_rate=float("nan"),
            timestamp=datetime.now(timezone.utc),
        )


def test_perp_tick_rejects_inf_open_interest():
    """Schema must reject Inf in open_interest (allow_inf_nan=False guard)."""
    with pytest.raises(ValidationError):
        PerpTick(
            exchange="bybit",
            symbol="BTCUSDT",
            ticker="BTC",
            open_interest=float("inf"),
            timestamp=datetime.now(timezone.utc),
        )


def test_perp_tick_extra_fields_ignored():
    """ConfigDict extra='ignore' must silently drop unknown fields."""
    t = PerpTick(
        exchange="binance",
        symbol="BTCUSDT",
        ticker="BTC",
        unknown_field="should_be_dropped",
        timestamp=datetime.now(timezone.utc),
    )
    assert not hasattr(t, "unknown_field")


def test_perp_anomaly_extra_fields_ignored():
    """ConfigDict extra='ignore' on PerpAnomaly must silently drop unknown fields."""
    a = PerpAnomaly(
        exchange="bybit",
        symbol="DOGEUSDT",
        ticker="DOGE",
        kind="funding_flip",
        magnitude=0.1,
        unknown_field="should_be_dropped",
        observed_at=datetime.now(timezone.utc),
    )
    assert not hasattr(a, "unknown_field")
