"""Pydantic models for Second-Wave Detection."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SecondWaveCandidate(BaseModel):
    contract_address: str
    chain: str
    token_name: str
    ticker: str

    # Prior pump data
    coingecko_id: str | None = None
    peak_quant_score: int
    peak_signals_fired: list[str]
    first_seen_at: datetime
    original_alert_at: datetime | None = None
    original_market_cap: float
    alert_market_cap: float

    # Cooldown data
    days_since_first_seen: float
    price_drop_from_peak_pct: float

    # Re-accumulation signals
    current_price: float
    current_market_cap: float
    current_volume_24h: float | None = None
    price_vs_alert_pct: float
    volume_vs_cooldown_avg: float
    price_is_stale: bool = False

    # Scoring
    reaccumulation_score: int
    reaccumulation_signals: list[str]

    # Metadata
    detected_at: datetime
    alerted_at: datetime | None = None
