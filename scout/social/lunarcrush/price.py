"""Price-change enrichment from the CoinGecko raw-markets cache.

Uses the in-process ``scout.ingestion.coingecko.last_raw_markets`` list
(updated once per scan cycle) -- zero extra HTTP calls, zero DB reads.
Falls back to ``(None, None)`` when no match is found; callers render
``price: —`` rather than blocking or crashing (design spec §8.1).
"""

from __future__ import annotations

from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


def _f(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_price_change_1h(
    symbol: Optional[str],
    coin_id: Optional[str],
) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Return ``(price_change_1h, price_change_24h, cg_slug)`` or
    ``(None, None, None)``.

    Match priority: exact ``coin_id`` first (the CoinGecko slug is the
    more specific key), then case-insensitive symbol equality. Entries
    that aren't dicts or that raise on field access are skipped rather
    than crashing -- LunarCrush is a best-effort context source.

    The third tuple element is the matched CoinGecko slug so alert
    rendering can build a correct ``coingecko.com/en/coins/<slug>`` link
    even when the LunarCrush ``coin_id`` is numeric (spec §9).
    """
    # Import lazily to avoid circular imports at module load.
    from scout.ingestion import coingecko as cg

    try:
        raw = list(cg.last_raw_markets or [])
    except (AttributeError, TypeError):
        # AttributeError: module still loading (last_raw_markets not yet bound).
        # TypeError: non-iterable stored (shouldn't happen, but be safe).
        # Let ImportError propagate -- that's a real wiring bug.
        logger.debug("price_enrichment_raw_access_error")
        return None, None, None

    target_symbol = (symbol or "").lower()
    target_id = (coin_id or "").lower()

    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            entry_id = (entry.get("id") or "").lower()
            entry_symbol = (entry.get("symbol") or "").lower()
            if target_id and entry_id and target_id == entry_id:
                return (
                    _f(entry.get("price_change_percentage_1h_in_currency")),
                    _f(entry.get("price_change_percentage_24h")),
                    entry_id,
                )
            if target_symbol and entry_symbol and target_symbol == entry_symbol:
                return (
                    _f(entry.get("price_change_percentage_1h_in_currency")),
                    _f(entry.get("price_change_percentage_24h")),
                    entry_id or None,
                )
        except (AttributeError, TypeError, ValueError):
            # Best-effort enrichment -- swallow at debug level rather than
            # spamming per-entry WARNING noise on every cycle.
            logger.debug("price_enrichment_entry_error")
            continue
    return None, None, None
