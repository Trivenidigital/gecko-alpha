"""Pydantic models for the Narrative Rotation Agent."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator


class CategorySnapshot(BaseModel):
    category_id: str
    name: str
    market_cap: float
    market_cap_change_24h: float
    volume_24h: float
    coin_count: int | None = None
    market_regime: str | None = None
    snapshot_at: datetime


class CategoryAcceleration(BaseModel):
    category_id: str
    name: str
    current_velocity: float
    previous_velocity: float
    acceleration: float
    volume_24h: float
    volume_growth_pct: float
    coin_count_change: int | None = None
    is_heating: bool


class LaggardToken(BaseModel):
    coin_id: str
    symbol: str
    name: str
    market_cap: float
    price: float
    price_change_24h: float
    volume_24h: float
    category_id: str
    category_name: str


class NarrativePrediction(BaseModel):
    id: int | None = None
    category_id: str
    category_name: str
    coin_id: str
    symbol: str
    name: str
    market_cap_at_prediction: float
    price_at_prediction: float
    narrative_fit_score: int
    staying_power: str
    confidence: str
    reasoning: str
    market_regime: str
    trigger_count: int
    is_control: bool = False
    is_holdout: bool = False
    strategy_snapshot: dict
    strategy_snapshot_ab: dict | None = None
    predicted_at: datetime
    outcome_6h_price: float | None = None
    outcome_6h_change_pct: float | None = None
    outcome_6h_class: str | None = None
    outcome_24h_price: float | None = None
    outcome_24h_change_pct: float | None = None
    outcome_24h_class: str | None = None
    outcome_48h_price: float | None = None
    outcome_48h_change_pct: float | None = None
    outcome_48h_class: str | None = None
    peak_price: float | None = None
    peak_change_pct: float | None = None
    peak_at: datetime | None = None
    outcome_class: str | None = None
    outcome_reason: str | None = None
    eval_retry_count: int = 0
    evaluated_at: datetime | None = None

    @field_validator("narrative_fit_score")
    @classmethod
    def clamp_score(cls, v: int) -> int:
        return max(0, min(100, v))


class NarrativeSignal(BaseModel):
    id: int | None = None
    category_id: str
    category_name: str
    acceleration: float
    volume_growth_pct: float
    coin_count_change: int | None = None
    trigger_count: int = 1
    detected_at: datetime
    cooling_down_until: datetime


class StrategyValue(BaseModel):
    key: str
    value: str
    updated_at: datetime
    updated_by: str
    reason: str
    locked: bool = False
    min_bound: float | None = None
    max_bound: float | None = None


class LearnLog(BaseModel):
    id: int | None = None
    cycle_number: int
    cycle_type: str
    reflection_text: str
    changes_made: dict
    hit_rate_before: float
    hit_rate_after: float | None = None
    created_at: datetime
