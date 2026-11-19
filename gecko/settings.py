from __future__ import annotations

import re

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_CHAINS = ("solana", "base", "ethereum", "bsc")
EST_NEXT_USD = (
    0.10  # research: assumed cost of the next dossier, checked against the cap
)
OPTIONAL_SECRETS = (  # credentials and chat ids: a blank value means unset
    "tg_api_hash",
    "coingecko_demo_api_key",
    "telegram_bot_token",
    "telegram_health_chat_id",
    "anthropic_api_key",
    "telegram_digest_chat_id",
)


class GeckoSettings(BaseSettings):
    """Settings from ``GECKO_*`` environment variables and ``.env.gecko``.

    Never ``.env``: the legacy pipeline owns that file and validates it with
    ``extra="forbid"``, so GECKO_ keys there would break it, and its keys
    must not leak in here.
    """

    # No env_ignore_empty: a blank NON-secret key must fail, not load a default
    # (a blank GECKO_REQUIRE_SECURITY_KNOWN= would silently read False).
    model_config = SettingsConfigDict(
        env_prefix="GECKO_", env_file=".env.gecko", extra="ignore"
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
    # open this long past due: closed unmeasured (irreversible). Floor 7 d, so the
    # 72 h forward_returns_backlog page always fires first.
    ledger_max_overdue_days: int = Field(14, ge=7)
    require_security_known: bool = False  # Plan 3 flips to True at cutover
    http_timeout_sec: float = 20
    ohlcv_max_per_run: int = 200
    # wall clock per run, under the 15 min timer: the rest waits for the next run
    ohlcv_run_budget_sec: int = Field(600, ge=60, le=780)
    gt_min_interval_sec: float = 2.1
    ds_min_interval_sec: float = 0.25  # spacing of every DexScreener request
    ds_refresh_max_per_run: int = 600  # known tokens re-read per run, oldest first
    candidate_max_age_days: int = 14  # unadmitted candidates then stop being read
    cg_min_interval_sec: float = 2.1  # Demo tier: 30 requests/min
    cg_first_page: int = 2  # page 1 is the top-250 mega caps
    cg_max_pages: int = 14
    cg_hourly_reserve: int = 100  # monthly calls the markets sweep leaves for hourly
    cg_max_lookups_per_run: int = 300  # /coins/{id} platform lookups per sweep
    cg_unmapped_ttl_days: int = Field(30, ge=1)  # an unmapped coin is re-looked-up
    gp_min_interval_sec: float = (
        2.0  # keyless GoPlus: <=30 calls/min, well under 150 CU/min
    )
    gp_max_per_run: int = 40  # tokens enriched per hourly run
    gp_daily_cap: int = (
        1000  # calls/day; calibrate from the GoPlus dashboard in the soak
    )
    gp_refresh_hours: int = (
        20  # active tokens re-read about daily (holder growth needs it)
    )
    tg_seed_max: int = 60  # called addresses offered to DexScreener per run
    tg_api_id: int = 0
    tg_api_hash: SecretStr | None = None
    tg_session_path: str = (
        ""  # absolute, outside the repo, mode 0600; gecko's OWN session
    )
    tg_channels: list[str] = []  # public handles, canonical "@foo" (see validator)
    tg_poll_interval_sec: int = Field(120, ge=30)  # faster invites FloodWait
    # newest messages read per channel per poll; 0 would read nothing, ok forever
    tg_catchup_limit: int = Field(200, ge=1)
    coingecko_demo_api_key: SecretStr | None = None
    coingecko_monthly_cap: int = 8000
    telegram_bot_token: SecretStr | None = None
    telegram_health_chat_id: str | None = None
    young_chain_launch_dates: dict[str, str] = {}  # chain -> ISO date

    # AI layer: research, digest, scorecard
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    research_model: str = "claude-sonnet-5-5"
    research_max_per_run: int = Field(10, ge=1)
    research_daily_usd_cap: float = Field(1.5, ge=EST_NEXT_USD)  # below: never runs
    research_max_searches: int = Field(5, ge=0, le=20)
    research_usd_per_mtok_in: float = Field(2.0, ge=0)
    research_usd_per_mtok_out: float = Field(10.0, ge=0)
    research_usd_per_search: float = Field(0.01, ge=0)
    research_max_tokens: int = Field(2000, ge=256, le=8192)
    telegram_digest_chat_id: str | None = None
    digest_top_n: int = Field(10, ge=1, le=50)
    scorecard_min_n: int = Field(20, ge=1)
    log_format: str = "json"  # "json" | "console"

    obs_retention_days: int = 180
    # >= 35: calls_this_month sums runs back to the 1st of the month
    runs_retention_days: int = Field(90, ge=35)

    @field_validator("obs_retention_days")
    @classmethod
    def _retention_floor(cls, v: int) -> int:
        # features read <=16 d back, admission 7 d, entry price 48 h: 30 d is the floor
        if v < 30:
            raise ValueError("retention must be >= 30 days")
        return v

    @field_validator(*OPTIONAL_SECRETS, mode="before")
    @classmethod
    def _blank_is_unset(cls, v):
        # `GECKO_X=` or whitespace (env or .env.gecko): unset, never an empty secret
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("chains")
    @classmethod
    def _chains_supported(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if c not in SUPPORTED_CHAINS]
        if bad:
            raise ValueError(f"unsupported chains: {bad}")
        return v

    @field_validator("log_format")
    @classmethod
    def _log_format_known(cls, v: str) -> str:
        if v not in ("json", "console"):
            raise ValueError(f"log_format must be 'json' or 'console', got {v!r}")
        return v

    @field_validator("anthropic_base_url")
    @classmethod
    def _anthropic_https(cls, v: str) -> str:
        if not v.startswith("https://"):  # the API key travels in a header
            raise ValueError(f"anthropic_base_url must start with https://, got {v!r}")
        return v

    @field_validator("tg_channels")
    @classmethod
    def _channels_canonical(cls, v: list[str]) -> list[str]:
        # "@foo": the key calls.event_id, source_id and the migrated history use;
        # "@Foo", "t.me/foo" and "https://t.me/Foo" are the same channel.
        out: list[str] = []
        for raw in v:
            h = raw.strip().lower()
            for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
                h = h.removeprefix(prefix)
            h = h.removeprefix("@").strip().removesuffix("/")
            # a malformed handle never resolves and never pages: refuse it here
            if not re.fullmatch(r"[a-z0-9_]+", h):
                raise ValueError(f"not a public telegram channel handle: {raw!r}")
            if f"@{h}" in out:
                raise ValueError(f"duplicate telegram channel: {raw!r}")
            out.append(f"@{h}")
        return out
