"""Application configuration via Pydantic BaseSettings."""

from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Scanner
    SCAN_INTERVAL_SECONDS: int = 60
    MIN_SCORE: int = 60
    CONVICTION_THRESHOLD: int = 70
    QUANT_WEIGHT: float = 0.6
    NARRATIVE_WEIGHT: float = 0.4

    # Token filters
    MIN_MARKET_CAP: float = 10_000
    MAX_MARKET_CAP: float = 500_000
    MAX_TOKEN_AGE_DAYS: int = 7
    MIN_LIQUIDITY_USD: float = 15_000
    MIN_VOL_LIQ_RATIO: float = 5.0
    BUY_PRESSURE_THRESHOLD: float = 0.65
    CO_OCCURRENCE_MIN_SIGNALS: int = 3
    CO_OCCURRENCE_MULTIPLIER: float = 1.15
    CHAINS: list[str] = ["solana", "base", "ethereum"]

    # CoinGecko
    MOMENTUM_RATIO_THRESHOLD: float = 0.6
    MIN_VOL_ACCEL_RATIO: float = 5.0
    COINGECKO_API_KEY: str = ""

    # MiroFish
    MIROFISH_URL: str = "http://localhost:5001"
    MIROFISH_TIMEOUT_SEC: int = 180
    MAX_MIROFISH_JOBS_PER_DAY: int = 50

    # Alerts
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_CHAT_ID: str
    DISCORD_WEBHOOK_URL: str = ""

    # Holder enrichment (optional)
    HELIUS_API_KEY: str = ""
    MORALIS_API_KEY: str = ""

    # Database
    DB_PATH: Path = Path("scout.db")

    # Anthropic fallback
    ANTHROPIC_API_KEY: str

    # Narrative Rotation Agent
    NARRATIVE_POLL_INTERVAL: int = 1800
    NARRATIVE_EVAL_INTERVAL: int = 21600
    NARRATIVE_DIGEST_HOUR_UTC: int = 0
    NARRATIVE_LEARN_HOUR_UTC: int = 1
    NARRATIVE_WEEKLY_LEARN_DAY: int = 6
    NARRATIVE_ENABLED: bool = False
    NARRATIVE_SNAPSHOT_RETENTION_DAYS: int = 7
    NARRATIVE_SCORING_MODEL: str = "claude-haiku-4-5"
    NARRATIVE_LEARN_MODEL: str = "claude-sonnet-4-6"

    # Counter-Narrative Scoring
    COUNTER_ENABLED: bool = True
    COUNTER_MODEL: str = "claude-haiku-4-5"
    COUNTER_SUPPRESS_THRESHOLD: int = 100

    # -------- Second-Wave Detection --------
    SECONDWAVE_ENABLED: bool = False
    SECONDWAVE_POLL_INTERVAL: int = 1800
    SECONDWAVE_MIN_PRIOR_SCORE: int = 60
    SECONDWAVE_COOLDOWN_MIN_DAYS: int = 3
    SECONDWAVE_COOLDOWN_MAX_DAYS: int = 14
    SECONDWAVE_MIN_DRAWDOWN_PCT: float = 30.0
    SECONDWAVE_MIN_RECOVERY_PCT: float = 70.0
    SECONDWAVE_VOL_PICKUP_RATIO: float = 2.0
    SECONDWAVE_ALERT_THRESHOLD: int = 50
    SECONDWAVE_DEDUP_DAYS: int = 7
    SECONDWAVE_MIN_VOLUME_POINTS: int = 2

    @field_validator("CHAINS", mode="before")
    @classmethod
    def parse_chains(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [c.strip() for c in v.split(",") if c.strip()]
        return v

    @field_validator("SECONDWAVE_ALERT_THRESHOLD")
    @classmethod
    def _validate_secondwave_threshold(cls, v: int) -> int:
        """Clamp the alert threshold to the legal 0..100 score range."""
        return max(0, min(100, v))

    @field_validator("SECONDWAVE_MIN_VOLUME_POINTS")
    @classmethod
    def _validate_secondwave_min_vol_points(cls, v: int) -> int:
        """Enforce a minimum of 2 volume snapshots before firing volume_pickup."""
        return max(2, int(v))

    @model_validator(mode="after")
    def validate_weights_sum(self) -> "Settings":
        total = self.QUANT_WEIGHT + self.NARRATIVE_WEIGHT
        if abs(total - 1.0) > 1e-9:
            msg = f"QUANT_WEIGHT ({self.QUANT_WEIGHT}) + NARRATIVE_WEIGHT ({self.NARRATIVE_WEIGHT}) = {total}, must sum to 1.0"
            raise ValueError(msg)
        return self
