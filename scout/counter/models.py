from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator


class RedFlag(BaseModel):
    """A single risk flag raised during counter-narrative analysis."""

    flag: str
    severity: str
    detail: str

    @field_validator("severity", mode="before")
    @classmethod
    def _default_severity(cls, v: str) -> str:
        if v not in ("low", "medium", "high"):
            return "medium"
        return v


class CounterScore(BaseModel):
    """Aggregated counter-narrative score for a trade candidate."""

    risk_score: int | None = None
    red_flags: list[RedFlag] = []
    counter_argument: str = ""
    data_completeness: str = ""
    counter_scored_at: datetime = datetime(1970, 1, 1)

    @field_validator("risk_score", mode="before")
    @classmethod
    def _clamp_risk_score(cls, v: int | None) -> int | None:
        if v is None:
            return v
        return max(0, min(100, v))
