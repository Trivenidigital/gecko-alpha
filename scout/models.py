"""Data models for CoinPump Scout."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class MiroFishResult(BaseModel):
    """Result from MiroFish narrative simulation or Claude fallback."""

    narrative_score: int
    virality_class: str
    summary: str


class CandidateToken(BaseModel):
    """A candidate token detected by the ingestion pipeline.

    All fields from PRD Section 6.1. Optional scores/reports default to None
    and are populated as the token progresses through pipeline stages.
    """

    contract_address: str
    chain: str
    token_name: str
    ticker: str
    token_age_days: float = 0
    market_cap_usd: float = 0
    liquidity_usd: float = 0
    volume_24h_usd: float = 0
    holder_count: int = 0
    holder_growth_1h: int = 0
    social_mentions_24h: int = 0

    # Populated by pipeline stages
    quant_score: int | None = None
    narrative_score: int | None = None
    conviction_score: float | None = None
    mirofish_report: str | None = None
    virality_class: str | None = None
    alerted_at: datetime | None = None
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_dexscreener(cls, data: dict) -> "CandidateToken":
        """Parse a DexScreener pair object into a CandidateToken."""
        base_token = data.get("baseToken", {})
        pair_created_ms = data.get("pairCreatedAt")

        # Calculate token age in days
        token_age_days = 0.0
        if pair_created_ms:
            created_at = datetime.fromtimestamp(pair_created_ms / 1000, tz=timezone.utc)
            age_delta = datetime.now(timezone.utc) - created_at
            token_age_days = age_delta.total_seconds() / 86400

        return cls(
            contract_address=base_token.get("address", ""),
            chain=data.get("chainId", ""),
            token_name=base_token.get("name", ""),
            ticker=base_token.get("symbol", ""),
            token_age_days=token_age_days,
            market_cap_usd=float(data.get("fdv") or 0),
            liquidity_usd=float((data.get("liquidity") or {}).get("usd") or 0),
            volume_24h_usd=float((data.get("volume") or {}).get("h24") or 0),
            holder_count=0,
            holder_growth_1h=0,
        )

    @classmethod
    def from_geckoterminal(cls, data: dict, chain: str) -> "CandidateToken":
        """Parse a GeckoTerminal pool object into a CandidateToken."""
        attrs = data.get("attributes", {})
        relationships = data.get("relationships", {})

        # Extract contract address from relationship ID: "solana_0xaddr" -> "0xaddr"
        base_token_id = (
            relationships.get("base_token", {}).get("data", {}).get("id", "")
        )
        contract_address = (
            base_token_id.split("_", 1)[-1] if "_" in base_token_id else base_token_id
        )

        # Extract token name from pool name: "GeckoToken / SOL" -> "GeckoToken"
        pool_name = attrs.get("name", "")
        token_name = pool_name.split("/")[0].strip() if "/" in pool_name else pool_name

        # Calculate token age
        token_age_days = 0.0
        pool_created = attrs.get("pool_created_at")
        if pool_created:
            created_at = datetime.fromisoformat(pool_created.replace("Z", "+00:00"))
            age_delta = datetime.now(timezone.utc) - created_at
            token_age_days = age_delta.total_seconds() / 86400

        volume_data = attrs.get("volume_usd", {})

        return cls(
            contract_address=contract_address,
            chain=chain,
            token_name=token_name,
            ticker=token_name,  # GeckoTerminal doesn't provide ticker separately; use name as fallback
            market_cap_usd=float(attrs.get("fdv_usd") or 0),
            liquidity_usd=float(attrs.get("reserve_in_usd") or 0),
            volume_24h_usd=(
                float(volume_data.get("h24") or 0)
                if isinstance(volume_data, dict)
                else 0
            ),
            token_age_days=token_age_days,
            holder_count=0,
            holder_growth_1h=0,
        )
