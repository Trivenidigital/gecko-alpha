"""Binance/Bybit perp WebSocket anomaly detector (BL-054)."""

import re

TICKER_PATTERN = re.compile(r"^[A-Z0-9]{1,20}$")
