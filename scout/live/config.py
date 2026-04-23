"""LiveConfig — typed wrapper over Settings for live-trading knobs.

Single source of truth for fallback logic (LIVE_* → PAPER_* → default).
Consumers never read Settings directly.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from scout.config import Settings


class LiveConfig:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    @property
    def mode(self) -> Literal["paper", "shadow", "live"]:
        return self._s.LIVE_MODE

    def is_signal_enabled(self, signal_type: str) -> bool:
        return signal_type.lower() in self._s.live_signal_allowlist_set

    def resolve_size_usd(self, signal_type: str) -> Decimal:
        return self._s.live_signal_sizes_map.get(
            signal_type.lower(),
            self._s.LIVE_TRADE_AMOUNT_USD,
        )

    def resolve_tp_pct(self) -> Decimal:
        return (
            self._s.LIVE_TP_PCT
            if self._s.LIVE_TP_PCT is not None
            else self._s.PAPER_TP_PCT
        )

    def resolve_sl_pct(self) -> Decimal:
        return (
            self._s.LIVE_SL_PCT
            if self._s.LIVE_SL_PCT is not None
            else self._s.PAPER_SL_PCT
        )

    def resolve_max_duration_hours(self) -> int:
        return (
            self._s.LIVE_MAX_DURATION_HOURS
            or self._s.PAPER_MAX_DURATION_HOURS
        )
