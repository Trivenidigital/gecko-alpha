"""BL-NEW-LIVE-HYBRID M1 v2.1: canonical-symbol extraction.

`canonical_from_ccxt_market(symbol)` extracts the canonical ticker from
a CCXT market symbol string. Used by `CCXTAdapter.fetch_venue_metadata`
and `RoutingLayer._on_demand_listings_fetch` to populate the
`symbol_aliases` and `venue_listings` tables consistently.

`lookup_canonical(db, venue, venue_pair)` does the reverse — given a
venue-specific pair, resolves to the canonical ticker via
`symbol_aliases` (or returns None if the alias hasn't been recorded yet).
"""

from __future__ import annotations

import structlog

from scout.db import Database

log = structlog.get_logger(__name__)


def canonical_from_ccxt_market(symbol: str) -> str:
    """Extract canonical ticker from CCXT market symbol string.

    Examples:
        BTC/USDT → BTC
        BTC/USDT:USDT (perp) → BTC
        1INCH/USDT → 1INCH
        ETH/USD → ETH

    Splits on '/'; takes [0]; then strips ':USDT' / ':USD' settlement
    suffix if present (CCXT perp notation embeds it after the quote).
    """
    base = symbol.split("/", 1)[0]
    return base.split(":", 1)[0]


async def lookup_canonical(db: Database, venue: str, venue_pair: str) -> str | None:
    """Reverse lookup: given a venue + venue_pair, return canonical
    ticker via symbol_aliases. Returns None if no alias recorded."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        "SELECT canonical FROM symbol_aliases " "WHERE venue = ? AND venue_symbol = ?",
        (venue, venue_pair),
    )
    row = await cur.fetchone()
    return row[0] if row is not None else None
