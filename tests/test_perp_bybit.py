import json
from pathlib import Path
from scout.perp.bybit import parse_frame

FIXTURES = Path(__file__).parent / "fixtures" / "perp"


def test_parse_bybit_ticker_snapshot():
    raw = json.loads((FIXTURES / "bybit_ticker.json").read_text())
    ticks = list(parse_frame(raw))
    assert len(ticks) == 1
    t = ticks[0]
    assert t.ticker == "BTC"
    assert t.funding_rate == 0.0001
    assert t.open_interest == 12345.678
    assert t.open_interest_usd == 617283900.0
    assert t.mark_price == 50000.0


def test_parse_bybit_pong_ignored():
    assert list(parse_frame({"op": "pong"})) == []


def test_parse_bybit_garbage_dropped():
    assert list(parse_frame({"topic": "orderbook.1.BTCUSDT", "data": {}})) == []
