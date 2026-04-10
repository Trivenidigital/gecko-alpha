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

    # Conviction Chains
    CHAIN_CHECK_INTERVAL_SEC: int = 300  # 5 minutes
    CHAIN_MAX_WINDOW_HOURS: float = 24.0
    CHAIN_COOLDOWN_HOURS: float = 12.0
    CHAIN_EVENT_RETENTION_DAYS: int = 14
    CHAIN_ACTIVE_RETENTION_DAYS: int = 7
    CHAIN_ALERT_ON_COMPLETE: bool = True
    CHAIN_TOTAL_BOOST_CAP: int = 30
    # CHAINS_ENABLED is a bool kill-switch. Pydantic v2 coerces env strings
    # ("true"/"1"/"yes") to bool automatically.
    CHAINS_ENABLED: bool = False
    # LEARN phase lifecycle knobs
    CHAIN_MIN_TRIGGERS_FOR_STATS: int = 10
    CHAIN_PROMOTION_THRESHOLD: float = 0.45
    CHAIN_GRADUATION_MIN_TRIGGERS: int = 30
    CHAIN_GRADUATION_HIT_RATE: float = 0.55

    @field_validator(
        "CHAIN_PROMOTION_THRESHOLD", "CHAIN_GRADUATION_HIT_RATE"
    )
    @classmethod
    def _validate_hit_rate_thresholds(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @field_validator(
        "CHAIN_MIN_TRIGGERS_FOR_STATS", "CHAIN_GRADUATION_MIN_TRIGGERS"
    )
    @classmethod
    def _validate_min_triggers(cls, v: int) -> int:
        return max(1, v)

    @field_validator("CHAINS", mode="before")
    @classmethod
    def parse_chains(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [c.strip() for c in v.split(",") if c.strip()]
        return v

    @model_validator(mode="after")
    def validate_weights_sum(self) -> "Settings":
        total = self.QUANT_WEIGHT + self.NARRATIVE_WEIGHT
        if abs(total - 1.0) > 1e-9:
            msg = f"QUANT_WEIGHT ({self.QUANT_WEIGHT}) + NARRATIVE_WEIGHT ({self.NARRATIVE_WEIGHT}) = {total}, must sum to 1.0"
            raise ValueError(msg)
        return self


_CACHED_SETTINGS: "Settings | None" = None


def get_settings() -> "Settings":
    """Return a cached Settings instance (lazy-init).

    Not async-safe for the very first call during startup races. Call
    :func:`configure_cache` once at app startup to pre-populate the cache
    and avoid any race. Tests may monkeypatch this function to override
    the returned instance.
    """
    global _CACHED_SETTINGS
    if _CACHED_SETTINGS is None:
        _CACHED_SETTINGS = Settings()  # type: ignore[call-arg]
    return _CACHED_SETTINGS


def configure_cache(settings: "Settings") -> None:
    """Pre-populate the settings cache at startup to avoid races."""
    global _CACHED_SETTINGS
    _CACHED_SETTINGS = settings
