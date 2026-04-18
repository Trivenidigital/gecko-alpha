"""Shared data models for the social-velocity tier.

Deliberately distinct from :class:`scout.models.CandidateToken` -- the
trading engine accepts only ``CandidateToken``. Passing a ``ResearchAlert``
would be a type error, not just a convention break. This is the structural
guardrail that keeps the research tier research-only (see design spec §3).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import NamedTuple, Optional

from pydantic import BaseModel, ConfigDict


class SpikeKind(str, Enum):
    """The three spike kinds the LunarCrush detector can fire.

    String values intentionally match the DB column suffixes
    (``fired_social_volume_24h`` etc.) so telemetry and storage stay in
    lock-step. Using an ``Enum`` prevents string-typo bugs in DB writes
    and tests (see design spec §3).
    """

    SOCIAL_VOLUME_24H = "social_volume_24h"
    GALAXY_JUMP = "galaxy_jump"
    INTERACTIONS_ACCEL = "interactions_accel"


class ResearchAlert(BaseModel):
    """A Telegram-bound, research-only social-velocity detection.

    This is the payload produced by the LunarCrush detector and consumed by
    the alerter. It carries every value needed to render the Telegram
    message and persist a ``social_signals`` row.
    """

    model_config = ConfigDict(frozen=False)

    # Identity
    coin_id: str
    symbol: str
    name: str

    # Detection
    spike_kinds: list[SpikeKind]
    social_spike_ratio: Optional[float] = None

    # Numeric context (all optional -- LunarCrush field drift or sparse data
    # should never block an alert from being dispatched).
    galaxy_score: Optional[float] = None
    galaxy_jump: Optional[float] = None
    social_volume_24h: Optional[float] = None
    social_volume_baseline: Optional[float] = None
    interactions_24h: Optional[float] = None
    interactions_ratio: Optional[float] = None
    sentiment: Optional[float] = None
    social_dominance: Optional[float] = None
    price_change_1h: Optional[float] = None
    price_change_24h: Optional[float] = None
    market_cap: Optional[float] = None
    current_price: Optional[float] = None

    detected_at: datetime


class BaselineState(NamedTuple):
    """Rolling baseline state for one coin.

    ``NamedTuple`` chosen over ``dataclass`` because the cache-update flow
    relies on ``_replace(...)`` to produce new snapshots without in-place
    mutation. Keeps the EWMA logic easy to reason about.
    """

    coin_id: str
    symbol: str
    avg_social_volume_24h: float
    avg_galaxy_score: float
    last_galaxy_score: Optional[float]
    interactions_ring: list[float]
    sample_count: int
    last_poll_at: Optional[datetime]
    last_updated: datetime
