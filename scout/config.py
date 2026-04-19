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
    HEARTBEAT_INTERVAL_SECONDS: int = 300  # BL-033: periodic heartbeat summary
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
    # Minimum absolute 24h price change (%) required for momentum_ratio to fire.
    # Prevents stablecoin peg-wobble (0.05%/0.08% -> ratio 0.625 > 0.6) from triggering.
    MOMENTUM_MIN_24H_CHANGE_PCT: float = 3.0
    MIN_VOL_ACCEL_RATIO: float = 5.0
    COINGECKO_API_KEY: str = ""
    COINGECKO_RATE_LIMIT_PER_MIN: int = 25  # buffer under 30/min free tier

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

    # -------- Market Briefing Agent --------
    BRIEFING_ENABLED: bool = False              # opt-in, not default-on
    BRIEFING_HOURS_UTC: str = "6,18"            # comma-separated hours (6am + 6pm)
    BRIEFING_MODEL: str = "claude-sonnet-4-6"
    BRIEFING_TELEGRAM_ENABLED: bool = True
    COINGLASS_API_KEY: str = ""                 # free tier, register at coinglass.com

    # -------- 7-Day Momentum Scanner --------
    MOMENTUM_7D_ENABLED: bool = True
    MOMENTUM_7D_MIN_CHANGE: float = 100.0       # min 7d change % to flag (100% = doubled)
    MOMENTUM_7D_MAX_MCAP: float = 500_000_000   # filter out mega caps
    MOMENTUM_7D_MIN_VOLUME: float = 100_000     # min $100K 24h volume — weeds out illiquid junk

    # -------- Volume Spike Detector --------
    VOLUME_SPIKE_ENABLED: bool = True
    VOLUME_SPIKE_RATIO: float = 5.0
    VOLUME_SPIKE_MAX_MCAP: float = 500_000_000

    # -------- Velocity Alerter (CoinGecko 1h early-pump detection) --------
    # Research-only alerts for tokens pumping hard in the last hour.
    # No paper trade dispatch -- Telegram plain-text only.
    VELOCITY_ALERTS_ENABLED: bool = False
    VELOCITY_MIN_1H_PCT: float = 30.0          # minimum 1h % change to flag
    VELOCITY_MIN_MCAP: float = 500_000         # skip dust
    VELOCITY_MAX_MCAP: float = 50_000_000      # skip mega-caps
    VELOCITY_MIN_VOL_MCAP_RATIO: float = 0.2   # vol_24h / mcap -- liquidity sanity
    VELOCITY_DEDUP_HOURS: int = 4              # re-alert cooldown per coin
    VELOCITY_TOP_N: int = 10                   # max alerts per cycle

    # -------- Top Gainers Tracker --------
    GAINERS_TRACKER_ENABLED: bool = True
    GAINERS_MIN_CHANGE: float = 20.0
    GAINERS_MAX_MCAP: float = 500_000_000

    # -------- Top Losers Tracker --------
    LOSERS_TRACKER_ENABLED: bool = False
    LOSERS_MIN_DROP: float = -15.0
    LOSERS_MAX_MCAP: float = 500_000_000

    # -------- Trending Snapshot Tracker --------
    TRENDING_SNAPSHOT_ENABLED: bool = True
    TRENDING_COMPARISON_INTERVAL: int = 21600  # 6 hours in seconds

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

    # -------- LunarCrush Social-Velocity Alerter --------
    # Research-only social-velocity signals (Telegram plain-text, no paper
    # trade dispatch). Double kill-switch: either LUNARCRUSH_ENABLED=false or
    # empty LUNARCRUSH_API_KEY disables the loop entirely.
    LUNARCRUSH_ENABLED: bool = False
    LUNARCRUSH_API_KEY: str = ""
    LUNARCRUSH_BASE_URL: str = "https://lunarcrush.com/api4/public"
    LUNARCRUSH_POLL_INTERVAL: int = 300                 # 5 min (default / normal)
    LUNARCRUSH_POLL_INTERVAL_SOFT: int = 600            # 10 min (used after 80% credits)
    LUNARCRUSH_RATE_LIMIT_PER_MIN: int = 9              # under hard 10/min
    LUNARCRUSH_DAILY_CREDIT_BUDGET: int = 2000          # free tier cap
    LUNARCRUSH_CREDIT_SOFT_PCT: float = 0.80            # downshift at 80%
    LUNARCRUSH_CREDIT_HARD_PCT: float = 0.95            # stop at 95%
    LUNARCRUSH_SOCIAL_SPIKE_RATIO: float = 2.0
    LUNARCRUSH_GALAXY_JUMP: float = 10.0
    LUNARCRUSH_INTERACTIONS_ACCEL: float = 3.0
    LUNARCRUSH_DEDUP_HOURS: int = 4
    LUNARCRUSH_TOP_N: int = 10
    LUNARCRUSH_BASELINE_MIN_HOURS: int = 24             # warmup wall-clock, interval-aware
    LUNARCRUSH_BASELINE_MIN_SAMPLES: int = 288          # EWMA alpha denominator
    LUNARCRUSH_CHECKPOINT_EVERY_N_POLLS: int = 12       # 60 min
    LUNARCRUSH_RETENTION_DAYS: int = 30
    # After N consecutive uncaught-crash-then-restart cycles, leave the
    # social tier down rather than thrash against a broken environment.
    LUNARCRUSH_MAX_CONSECUTIVE_RESTARTS: int = 5

    # -------- Paper Trading Engine --------
    TRADING_ENABLED: bool = False                  # master switch
    TRADING_MODE: str = "paper"                    # "paper" or "live"
    PAPER_TRADE_AMOUNT_USD: float = 1000.0         # per trade (paper)
    PAPER_MAX_EXPOSURE_USD: float = 10000.0        # max total open (paper)
    PAPER_TP_PCT: float = 20.0                     # take profit %
    PAPER_SL_PCT: float = 10.0                     # stop loss % (positive: 10.0 = 10%)
    PAPER_MAX_DURATION_HOURS: int = 48             # auto-expire
    PAPER_TP_SELL_PCT: float = 70.0               # sell 70% at TP, keep 30% as long_hold
    PAPER_SLIPPAGE_BPS: int = 50                   # 0.5% slippage simulation
    PAPER_MIN_MCAP: float = 5_000_000             # min $5M mcap to paper trade (filters junk)
    PAPER_MAX_MCAP_RANK: int = 1500                # skip trending coins below rank 1500 (illiquid)
    # Hard cap on concurrent open positions. Prevents restart-bursts and
    # survives env changes to PAPER_MAX_EXPOSURE_USD / PAPER_TRADE_AMOUNT_USD.
    PAPER_MAX_OPEN_TRADES: int = 10
    # Cooldown after service start: refuse to open new paper trades during
    # this window so a restart doesn't replay every currently-qualifying
    # candidate as a fresh signal. A live trader doesn't bulk-enter on reboot.
    PAPER_STARTUP_WARMUP_SECONDS: int = 180
    TRADING_DIGEST_HOUR_UTC: int = 0               # midnight digest
    TRADING_EVAL_INTERVAL: int = 1800              # 30 min eval cycle

    # Feedback-loop (Sprint 1, spec 2026-04-18)
    FEEDBACK_SUPPRESSION_MIN_TRADES: int = 20
    FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT: float = 30.0
    FEEDBACK_PAROLE_DAYS: int = 14
    FEEDBACK_PAROLE_RETEST_TRADES: int = 5
    FEEDBACK_MIN_LEADERBOARD_TRADES: int = 10
    FEEDBACK_MISSED_WINNER_MIN_PCT: float = 50.0
    FEEDBACK_MISSED_WINNER_MIN_MCAP: float = 5_000_000
    FEEDBACK_MISSED_WINNER_WINDOW_MIN: int = 30
    FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN: int = 60
    FEEDBACK_WEEKLY_DIGEST_WEEKDAY: int = 6
    FEEDBACK_WEEKLY_DIGEST_HOUR: int = 9
    FEEDBACK_COMBO_REFRESH_HOUR: int = 3
    FEEDBACK_FALLBACK_ALERT_THRESHOLD: int = 5
    FEEDBACK_FALLBACK_ALERT_COOLDOWN_SEC: int = 900
    FEEDBACK_CHRONIC_FAILURE_THRESHOLD: int = 3

    @field_validator("PAPER_SL_PCT")
    @classmethod
    def _validate_paper_sl_pct(cls, v: float) -> float:
        if v < 0:
            raise ValueError(
                "sl_pct must be positive, e.g. 10.0 for 10% stop loss"
            )
        return v

    @field_validator("PAPER_TP_PCT")
    @classmethod
    def _validate_paper_tp_pct(cls, v: float) -> float:
        if v < 0:
            raise ValueError(
                "tp_pct must be positive, e.g. 20.0 for 20% take profit"
            )
        return v

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

    @field_validator("HEARTBEAT_INTERVAL_SECONDS")
    @classmethod
    def _validate_heartbeat(cls, v: int) -> int:
        if v <= 0:
            return 300  # default fallback
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
