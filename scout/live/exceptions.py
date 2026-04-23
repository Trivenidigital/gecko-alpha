"""Typed exception hierarchy for the live-trading pipeline (spec §10.1).

Raised by Binance adapter, resolver, gates, engine. Callers (shadow evaluator,
reconciliation, LiveEngine) discriminate on type rather than message string.
"""
from __future__ import annotations


class LiveError(Exception):
    """Root of the live-trading exception hierarchy."""


class VenueTransientError(LiveError):
    """5xx, network error, or exhausted retry. Caller may retry later."""


class VenueNotResolved(LiveError):
    """Symbol cannot be mapped to a Binance pair. Terminal for this trade."""


class DepthInsufficient(LiveError):
    """Orderbook too shallow to fill requested size at any slippage."""


class RateLimitError(VenueTransientError):
    """429 response or used-weight >= 95%. Subclass of transient."""


class KillSwitchActive(LiveError):
    """Kill switch was active at gate check — trade must not proceed."""
