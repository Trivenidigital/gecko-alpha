"""symbol_normalize — canonical symbol extraction + venue-pair lookup.

Per design v2.1: canonical tickers are lowercase base assets (no quote pair).
Examples:
- BTC/USDT → btc
- BTC/USDT:USDT (perp) → btc
- 1INCH/USDT → 1inch
- $PEPE-SOL/USDC (Solana memecoin) → pepe

Venue-symbol lookup queries symbol_aliases table for venue-specific symbols:
- canonical='btc', venue='binance' → 'BTCUSDT'
- canonical='eth', venue='kraken' → 'XETHUSDT'
"""

from __future__ import annotations

from typing import Any

import structlog

from scout.db import Database

log = structlog.get_logger(__name__)


def canonical_from_ccxt_market(
    market: str | dict,
    base_asset_override: str | None = None,
) -> str:
    """Extract canonical ticker from CCXT market symbol or object.

    Args:
        market: CCXT market symbol string (e.g. 'BTC/USDT') or dict
                with 'symbol' key.
        base_asset_override: If provided, use this as the base asset
                             instead of extracting from symbol.

    Returns:
        Lowercase canonical ticker (base asset only, no quote pair).

    Examples:
        canonical_from_ccxt_market('BTC/USDT') → 'btc'
        canonical_from_ccxt_market('BTC/USDT:USDT') → 'btc'
        canonical_from_ccxt_market('1INCH/USDT') → '1inch'
        canonical_from_ccxt_market('$PEPE-SOL/USDC') → 'pepe'
    """
    # Extract symbol string if dict was passed
    if isinstance(market, dict):
        symbol_str = market.get("symbol", "")
        if not symbol_str and base_asset_override:
            return base_asset_override.lower()
    else:
        symbol_str = market

    # If base_asset_override provided, use it directly
    if base_asset_override:
        return base_asset_override.lower()

    # Extract base asset (before '/')
    base = symbol_str.split("/")[0]

    # Strip settlement suffix (perp notation) — e.g. "BTC:USDT" → "BTC"
    base = base.split(":")[0]

    # Handle Solana memecoin convention: $PREFIX-SOL → PREFIX
    # or plain SYMBOL-SOL → SYMBOL (also drop -SOL suffix for any chain)
    if base.startswith("$"):
        base = base[1:]  # Remove $ prefix

    if "-SOL" in base.upper():
        base = base.split("-SOL")[0]

    return base.lower()


async def lookup_canonical(
    db: Database,
    canonical: str,
    venue: str,
) -> str | None:
    """Query symbol_aliases table for venue-specific pair.

    Args:
        db: Database connection.
        canonical: Canonical ticker (e.g. 'btc').
        venue: Venue name (e.g. 'binance').

    Returns:
        Venue-specific pair (e.g. 'BTCUSDT') if found, None otherwise.

    Raises:
        Exception: Database query errors are propagated.

    Notes:
        - Lookup is case-insensitive on canonical (both sides)
        - Returns None if not found (caller should fallback to
          on-demand fetch from routing layer)
    """
    cur = await db._conn.execute(
        """
        SELECT venue_symbol FROM symbol_aliases
        WHERE LOWER(canonical) = LOWER(?) AND venue = ?
        LIMIT 1
        """,
        (canonical, venue),
    )
    row = await cur.fetchone()

    if row:
        return row[0]

    return None
