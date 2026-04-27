"""Pydantic models for BL-064 TG social signals."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ResolutionState(str, Enum):
    """Three-state resolution outcome.

    RESOLVED            Token fully resolved + enriched + safety verdict known.
    UNRESOLVED_TRANSIENT  CG/DexScreener miss; retry once after the configured delay.
                        Brand-new gems often need a minute for indexing to catch up.
    UNRESOLVED_TERMINAL Still unresolved after retry. Alert-only, no trade.
    """

    RESOLVED = "RESOLVED"
    UNRESOLVED_TRANSIENT = "UNRESOLVED_TRANSIENT"
    UNRESOLVED_TERMINAL = "UNRESOLVED_TERMINAL"


class ContractRef(BaseModel):
    """A blockchain contract reference parsed from a TG message."""

    chain: str  # 'solana', 'ethereum', 'base', etc.
    address: str

    @property
    def normalized(self) -> str:
        """Lowercase + chain-prefixed key for dedup-style lookups."""
        addr = self.address.lower() if self.chain != "solana" else self.address
        return f"{self.chain}:{addr}"


class ParsedMessage(BaseModel):
    """Output of parser.parse_message — pure regex extraction."""

    cashtags: list[str] = Field(default_factory=list)  # normalized to upper, no '$'
    contracts: list[ContractRef] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.cashtags and not self.contracts


class ResolvedToken(BaseModel):
    """Output of resolver — token resolved + enriched + safety verdict.

    Safety state is encoded by THREE booleans rather than a 2-bit pair so
    cashtag-only resolutions don't masquerade as "failed safety check":
      safety_skipped_no_ca=True → no CA available; gate 2 (no_ca) handles it
      safety_check_completed=True, safety_pass=True  → verified safe
      safety_check_completed=True, safety_pass=False → GoPlus said unsafe
      safety_check_completed=False                   → GoPlus 5xx/timeout
    """

    token_id: str
    symbol: str
    chain: str | None = None
    contract_address: str | None = None
    mcap: float | None = None
    price_usd: float | None = None
    volume_24h_usd: float | None = None
    age_days: float | None = None
    safety_pass: bool = False
    safety_check_completed: bool = False  # FAIL-CLOSED discriminator (BL-063 lesson)
    safety_skipped_no_ca: bool = False  # cashtag-only — no CA to check


class ResolutionResult(BaseModel):
    """Wrapper combining resolution state + (optional) resolved tokens."""

    state: ResolutionState
    tokens: list[ResolvedToken] = Field(default_factory=list)
    candidates_top3: list[ResolvedToken] = Field(
        default_factory=list
    )  # cashtag-only ambiguous
    error_text: str | None = None


class AdmissionDecision(BaseModel):
    """Output of dispatcher gates — captures why a trade did/didn't dispatch."""

    dispatch_trade: bool
    blocked_gate: str | None = None  # 'dedup_open', 'no_ca', 'channel_disabled', etc.
    reason: str | None = None


class TgSocialMessageRow(BaseModel):
    """Pydantic mirror of the tg_social_messages row for typed reads."""

    id: int | None = None
    channel_handle: str
    msg_id: int
    posted_at: datetime
    sender: str | None = None
    text: str | None = None
    cashtags: str | None = None  # JSON-serialized
    contracts: str | None = None
    urls: str | None = None
    parsed_at: datetime


AlertProvenance = Literal["curator", "pipeline"]
ListenerState = Literal["running", "disabled_floodwait", "auth_lost", "stopped"]
