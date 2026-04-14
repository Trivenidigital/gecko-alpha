"""Pydantic models for the Trending Snapshot Tracker."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TrendingSnapshot(BaseModel):
    """A single coin seen on CoinGecko /search/trending at a point in time."""

    coin_id: str  # CoinGecko slug e.g. "bless-network"
    symbol: str
    name: str
    market_cap_rank: int | None = None
    trending_score: float | None = None  # CoinGecko trending score/rank position
    snapshot_at: datetime


class TrendingComparison(BaseModel):
    """Comparison result: did our system detect this token before it trended?"""

    coin_id: str
    symbol: str
    name: str
    appeared_on_trending_at: datetime

    # Narrative agent detection
    detected_by_narrative: bool = False
    narrative_detected_at: datetime | None = None
    narrative_lead_minutes: float | None = None

    # Pipeline (candidates table) detection
    detected_by_pipeline: bool = False
    pipeline_detected_at: datetime | None = None
    pipeline_lead_minutes: float | None = None

    # Signal chain detection
    detected_by_chains: bool = False
    chains_detected_at: datetime | None = None
    chains_lead_minutes: float | None = None

    # Overall miss flag
    is_gap: bool = True  # True = we missed it entirely


class TrendingStats(BaseModel):
    """Aggregate stats for the trending tracker."""

    total_tracked: int = 0
    caught_before_trending: int = 0
    missed: int = 0
    hit_rate_pct: float = 0.0
    avg_lead_minutes: float | None = None
    best_lead_minutes: float | None = None
    by_narrative: int = 0
    by_pipeline: int = 0
    by_chains: int = 0
