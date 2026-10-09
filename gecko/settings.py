from __future__ import annotations

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_CHAINS = ("solana", "base", "ethereum", "bsc")


class GeckoSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GECKO_", env_file=".env", extra="ignore"
    )

    db_path: str = "gecko.db"
    chains: list[str] = list(SUPPORTED_CHAINS)
    min_liquidity_usd: float = 250_000
    min_mcap_usd: float = 500_000
    max_mcap_usd: float = 200_000_000
    survivor_hours: int = 24
    watchlist_cap: int = 50
    watchlist_max_age_days: int = 180
    f4_max_age_days: int = 90
    f4_min_vol_ratio: float = 2.0
    top10_disqualify_pct: float = 60.0
    dead_after_days: int = 7
    require_security_known: bool = False  # Plan 3 flips to True at cutover
    http_timeout_sec: float = 20
    ohlcv_max_per_run: int = 200
    gt_min_interval_sec: float = 2.1
    cg_min_interval_sec: float = 2.1  # Demo tier: 30 requests/min
    cg_first_page: int = 2  # page 1 is the top-250 mega caps
    cg_max_pages: int = 14
    cg_hourly_reserve: int = 100  # monthly calls the markets sweep leaves for hourly
    cg_max_lookups_per_run: int = 300  # /coins/{id} platform lookups per sweep
    coingecko_demo_api_key: SecretStr | None = None
    coingecko_monthly_cap: int = 8000
    telegram_bot_token: SecretStr | None = None
    telegram_health_chat_id: str | None = None
    young_chain_launch_dates: dict[str, str] = {}  # chain -> ISO date

    @field_validator("chains")
    @classmethod
    def _chains_supported(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if c not in SUPPORTED_CHAINS]
        if bad:
            raise ValueError(f"unsupported chains: {bad}")
        return v
