"""Ticker normalization from exchange-native symbol to base-asset ticker."""

import re

_SUFFIXES = ("USDT", "USDC", "BUSD", "USD", "-PERP")
_QUOTE_ASSETS = frozenset({"USDT", "USDC", "BUSD", "USD"})
_VALID = re.compile(r"^[A-Z0-9]{1,20}$")


def normalize_ticker(symbol: str) -> str | None:
    """Return normalized base-asset ticker, or None if input is malformed.

    Rules:
      * Upper-case.
      * Strip one trailing suffix from {USDT, USDC, BUSD, USD, -PERP}.
      * Strip leading "1000" (Binance cosmetic multiplier convention).
      * Reject pure quote-currency strings (e.g. bare "USDT").
      * Reject strings that strip down to a pure numeric (e.g. "1000USDT" → "1000").
      * Validate against ``^[A-Z0-9]{1,20}$``.
    """
    if not isinstance(symbol, str):
        return None
    up = symbol.upper()
    for suffix in _SUFFIXES:
        if up.endswith(suffix) and len(up) > len(suffix):
            up = up[: -len(suffix)]
            break
    # Strip leading Binance cosmetic 1000-multiplier prefix.
    if up.startswith("1000") and len(up) >= 4:
        up = up[4:]
    # Reject empty, pure quote assets, or pure-numeric leftovers.
    if not up or up in _QUOTE_ASSETS or up.isdigit():
        return None
    if not _VALID.match(up):
        return None
    return up
