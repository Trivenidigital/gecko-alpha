import pytest
from datetime import datetime, timezone
from pydantic import ValidationError
from scout.perp.schemas import PerpTick, PerpAnomaly, Exchange, AnomalyKind

def test_perp_tick_requires_upper_ticker_charset():
    with pytest.raises(ValidationError):
        PerpTick(
            exchange="binance", symbol="BTCUSDT", ticker="btc-lower",
            timestamp=datetime.now(timezone.utc),
        )

def test_perp_tick_rejects_oversized_ticker():
    with pytest.raises(ValidationError):
        PerpTick(
            exchange="binance", symbol="BTCUSDT", ticker="A" * 21,
            timestamp=datetime.now(timezone.utc),
        )

def test_perp_tick_rejects_oversized_symbol():
    with pytest.raises(ValidationError):
        PerpTick(
            exchange="binance", symbol="X" * 33, ticker="BTC",
            timestamp=datetime.now(timezone.utc),
        )

def test_perp_tick_happy_path():
    t = PerpTick(
        exchange="binance", symbol="BTCUSDT", ticker="BTC",
        funding_rate=0.0001, mark_price=50000.0, open_interest=12345.0,
        timestamp=datetime.now(timezone.utc),
    )
    assert t.exchange == "binance"
    assert t.ticker == "BTC"

def test_perp_anomaly_happy_path():
    a = PerpAnomaly(
        exchange="bybit", symbol="DOGEUSDT", ticker="DOGE",
        kind="oi_spike", magnitude=4.2, baseline=1.0,
        observed_at=datetime.now(timezone.utc),
    )
    assert a.kind == "oi_spike"


def test_perp_tick_rejects_unknown_exchange():
    from datetime import datetime, timezone
    import pytest
    from pydantic import ValidationError
    from scout.perp.schemas import PerpTick
    with pytest.raises(ValidationError):
        PerpTick(exchange="okx", symbol="BTCUSDT", ticker="BTC",
                 timestamp=datetime.now(timezone.utc))
