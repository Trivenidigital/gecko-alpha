"""Pydantic models for the conviction chain tracker."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ChainEvent(BaseModel):
    """A single signal event emitted by any module."""

    id: int | None = None
    token_id: str
    pipeline: str  # "narrative" | "memecoin"
    event_type: str
    event_data: dict
    source_module: str
    created_at: datetime


class ChainStep(BaseModel):
    """One step in a chain pattern definition."""

    step_number: int
    event_type: str
    condition: str | None = None
    max_hours_after_anchor: float
    max_hours_after_previous: float | None = None


class ChainPattern(BaseModel):
    """A configurable chain pattern definition."""

    id: int | None = None
    name: str
    description: str
    steps: list[ChainStep]
    min_steps_to_trigger: int
    conviction_boost: int
    alert_priority: str  # "high" | "medium" | "low"
    is_active: bool = True
    historical_hit_rate: float | None = None
    total_triggers: int = 0
    total_hits: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ActiveChain(BaseModel):
    """Tracks an in-progress chain for a specific token."""

    id: int | None = None
    token_id: str
    pipeline: str
    pattern_id: int
    pattern_name: str
    steps_matched: list[int]
    step_events: dict[int, int]  # step_number -> signal_event_id
    anchor_time: datetime
    last_step_time: datetime
    is_complete: bool = False
    completed_at: datetime | None = None
    created_at: datetime


class ChainMatch(BaseModel):
    """A completed chain — stored for LEARN phase and boost application."""

    id: int | None = None
    token_id: str
    pipeline: str
    pattern_id: int
    pattern_name: str
    steps_matched: int
    total_steps: int
    anchor_time: datetime
    completed_at: datetime
    chain_duration_hours: float
    conviction_boost: int
    outcome_class: str | None = None
    outcome_change_pct: float | None = None
    evaluated_at: datetime | None = None
