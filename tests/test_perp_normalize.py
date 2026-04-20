import pytest
from scout.perp.normalize import normalize_ticker

@pytest.mark.parametrize("raw,expected", [
    ("BTCUSDT",     "BTC"),
    ("BTCUSDC",     "BTC"),
    ("1000PEPEUSDT","PEPE"),
    ("DOGEUSD",     "DOGE"),      # inverse collapse
    ("ETH-PERP",    "ETH"),
    ("SOLBUSD",     "SOL"),
    ("btcusdt",     "BTC"),        # upper-casing
])
def test_normalize_ticker_happy(raw, expected):
    assert normalize_ticker(raw) == expected

@pytest.mark.parametrize("raw", [
    "../etc/passwd",
    "SYMBOL WITH SPACE",
    "A" * 33,
    "",
    "USDT",        # after strip => empty
    "1000USDT",    # strip 1000 + USDT => empty
])
def test_normalize_ticker_drops_malformed(raw):
    assert normalize_ticker(raw) is None
