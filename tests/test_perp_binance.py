import json
from pathlib import Path
from scout.perp.binance import parse_frame

FIXTURES = Path(__file__).parent / "fixtures" / "perp"

def test_parse_markprice_array_yields_two_ticks():
    raw = json.loads((FIXTURES / "binance_markprice.json").read_text())
    ticks = list(parse_frame(raw))
    assert len(ticks) == 2
    btc, pepe = ticks
    assert btc.ticker == "BTC"
    assert btc.mark_price == 50000.0
    assert btc.funding_rate == 0.0001
    assert pepe.ticker == "PEPE"
    assert pepe.funding_rate == -0.0003

def test_parse_openinterest_yields_one_tick():
    raw = json.loads((FIXTURES / "binance_openinterest.json").read_text())
    ticks = list(parse_frame(raw))
    assert len(ticks) == 1
    assert ticks[0].open_interest == 123456.789
    assert ticks[0].ticker == "BTC"

def test_parse_frame_drops_malformed():
    assert list(parse_frame({"garbage": True})) == []
    assert list(parse_frame({"stream": "unknown", "data": {}})) == []
