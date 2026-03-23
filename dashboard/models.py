"""Pydantic response models for dashboard API."""

from pydantic import BaseModel


class CandidateResponse(BaseModel):
    contract_address: str
    token_name: str | None = None
    ticker: str | None = None
    chain: str | None = None
    market_cap_usd: float | None = None
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    quant_score: int | None = None
    narrative_score: int | None = None
    conviction_score: float | None = None
    signals_fired: list[str] = []
    alerted_at: str | None = None
    first_seen_at: str | None = None


class AlertResponse(BaseModel):
    contract_address: str
    token_name: str | None = None
    ticker: str | None = None
    chain: str | None = None
    conviction_score: float | None = None
    alerted_at: str | None = None
    market_cap_usd: float | None = None


class SignalHitRate(BaseModel):
    signal_name: str
    fired_count: int = 0
    total_candidates_today: int = 0


class StatusResponse(BaseModel):
    pipeline_status: str = "running"
    tokens_scanned_session: int = 0
    candidates_today: int = 0
    mirofish_jobs_today: int = 0
    mirofish_cap: int = 50
    alerts_today: int = 0
    cg_calls_this_minute: int = 0
    cg_rate_limit: int = 30


class FunnelResponse(BaseModel):
    ingested: int = 0
    aggregated: int = 0
    scored: int = 0
    safety_passed: int = 0
    mirofish_run: int = 0
    alerted: int = 0
