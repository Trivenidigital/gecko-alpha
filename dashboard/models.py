"""Pydantic response models for dashboard API."""

from typing import Literal

from pydantic import BaseModel, Field


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
    alert_market_cap: float | None = None
    price_change_pct: float | None = None
    check_price: float | None = None
    check_time: str | None = None


class WinRateResponse(BaseModel):
    total_outcomes: int = 0
    wins: int = 0
    win_rate_pct: float = 0
    avg_return_pct: float = 0


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


class SearchHit(BaseModel):
    canonical_id: str
    entity_kind: str = "token"
    symbol: str | None = None
    name: str | None = None
    chain: str | None = None
    contract_address: str | None = None
    sources: list[str] = []
    source_counts: dict[str, int] = {}
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    match_quality: str
    best_paper_trade_pnl_pct: float | None = None


class SearchResponse(BaseModel):
    query: str
    total_hits: int = 0
    hits: list[SearchHit] = []
    truncated: bool = False


LiveCandidateVerdict = Literal[
    "candidate_review",
    "watch",
    "blocked",
    "data_insufficient",
]
LiveCandidateEntryQuality = Literal[
    "fresh_entry",
    "acceptable_pullback",
    "already_faded",
    "already_ran",
    "too_stale",
    "data_insufficient",
]


class LiveCandidateResponse(BaseModel):
    disclaimer: str

    token_id: str
    symbol: str | None = None
    name: str | None = None
    chain: str | None = None

    open_trade_ids: list[int] = []
    recent_trade_ids: list[int] = []
    surfaces: list[str] = []
    actionable: int | None = None
    would_be_live: int | None = None

    opened_at: str | None = None
    entry_price: float | None = None
    pct_from_entry: float | None = None

    current_price: float | None = None
    market_cap: float | None = None
    price_change_24h: float | None = None
    price_updated_at: str | None = None
    price_is_stale: bool = False

    narrative_fit_score: int | None = None
    counter_risk_score: int | None = None
    counter_flags: list[dict | str] = []
    latest_chain_match: dict | None = None

    entry_quality: LiveCandidateEntryQuality
    verdict: LiveCandidateVerdict
    inclusion_reasons: list[str] = []
    risk_reasons: list[str] = []


class LiveCandidateMeta(BaseModel):
    read_only: bool = True
    not_trade_advice: bool = True
    experimental: bool = True
    generated_at: str
    window_hours: int
    limit: int
    open_trades_scanned: int
    rows_returned: int


class LiveCandidateCockpit(BaseModel):
    meta: LiveCandidateMeta
    rows: list[LiveCandidateResponse] = []


TradeInboxGroup = Literal["act_now", "watch", "already_ran", "blocked"]
TradeInboxWindowState = Literal["open", "closing", "late", "closed", "unknown"]
TradeInboxActionLabel = Literal[
    "REVIEW_NOW",
    "WATCH_PULLBACK",
    "TOO_LATE",
    "BLOCKED",
    "DATA_MISSING",
]


class TradeInboxRow(BaseModel):
    token_id: str
    symbol: str | None = None
    name: str | None = None
    chain: str | None = None

    group: TradeInboxGroup
    action_label: TradeInboxActionLabel
    window_state: TradeInboxWindowState
    trade_score: float
    sort_key: list[str | float | int] = Field(default_factory=list)
    why_now: list[str] = Field(default_factory=list)

    inclusion_reasons: list[str] = Field(default_factory=list)
    risk_reasons: list[str] = Field(default_factory=list)
    surfaces: list[str] = Field(default_factory=list)
    open_trade_ids: list[int] = Field(default_factory=list)
    recent_trade_ids: list[int] = Field(default_factory=list)
    actionable: int | None = None
    would_be_live: int | None = None
    block_reason_primary: str | None = None

    opened_at: str | None = None
    opened_age_hours: float | None = None
    pct_from_entry: float | None = None
    price_change_24h: float | None = None
    market_cap: float | None = None
    current_price: float | None = None
    entry_quality: str | None = None
    verdict: str | None = None
    price_updated_at: str | None = None
    price_is_stale: bool = False
    price_staleness_minutes: float | None = None


class TradeInboxMeta(BaseModel):
    read_only: bool = True
    not_trade_advice: bool = True
    experimental: bool = True
    generated_at: str
    window_hours: int
    limit_per_group: int
    rows_returned: int
    source_limit: int
    source_rows_considered: int
    open_trades_scanned: int
    source_truncated: bool = False
    group_counts: dict[str, int] = Field(default_factory=dict)
    group_hidden_counts: dict[str, int] = Field(default_factory=dict)
    block_reason_counts: dict[str, int] = Field(default_factory=dict)
    stale_warning_count: int = 0
    hard_stale_count: int = 0
    source: str = "live_candidates"


class TradeInboxResponse(BaseModel):
    meta: TradeInboxMeta
    groups: dict[TradeInboxGroup, list[TradeInboxRow]]
