"""Data models for the Market Briefing Agent."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class BriefingData(BaseModel):
    """Raw data collected from external APIs and internal DB."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    fear_greed: dict | None = None
    global_market: dict | None = None
    funding_rates: dict | None = None
    liquidations: dict | None = None
    defi_tvl: dict | None = None
    news: list[dict] | None = None
    internal: dict | None = None


class Briefing(BaseModel):
    """A complete briefing record (raw + synthesized)."""

    id: int | None = None
    briefing_type: str  # "morning", "evening", or "manual"
    raw_data: dict
    synthesis: str
    model_used: str
    tokens_used: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
