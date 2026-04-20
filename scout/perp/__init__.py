"""Binance/Bybit perp WebSocket anomaly detector (BL-054)."""

import re
from typing import NewType

TICKER_PATTERN = re.compile(r"^[A-Z0-9]{1,20}$")

# NewTypes for static analysis — runtime-transparent (just str).
# Thread through public signatures in scout/perp/ to catch accidental mixing.
Ticker = NewType("Ticker", str)
Symbol = NewType("Symbol", str)
