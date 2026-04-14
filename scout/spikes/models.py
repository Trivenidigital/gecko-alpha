"""Pydantic models for the Volume Spike Detector."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class VolumeSpike(BaseModel):
    """A detected volume spike for a single token."""

    coin_id: str
    symbol: str
    name: str
    current_volume: float
    avg_volume_7d: float
    spike_ratio: float  # current / avg (e.g. 5.2x)
    market_cap: float | None = None
    price: float | None = None
    price_change_24h: float | None = None
    detected_at: datetime
