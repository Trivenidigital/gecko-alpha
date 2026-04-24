"""Application configuration via Pydantic BaseSettings."""

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
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
    BRIEFING_ENABLED: bool = False  # opt-in, not default-on
    BRIEFING_HOURS_UTC: str = "6,18"  # comma-separated hours (6am + 6pm)
    BRIEFING_MODEL: str = "claude-sonnet-4-6"
    BRIEFING_TELEGRAM_ENABLED: bool = True
    COINGLASS_API_KEY: str = ""  # free tier, register at coinglass.com

    # -------- 7-Day Momentum Scanner --------
    MOMENTUM_7D_ENABLED: bool = True
    MOMENTUM_7D_MIN_CHANGE: float = 100.0  # min 7d change % to flag (100% = doubled)
    MOMENTUM_7D_MAX_MCAP: float = 500_000_000  # filter out mega caps
    MOMENTUM_7D_MIN_VOLUME: float = (
        100_000  # min $100K 24h volume — weeds out illiquid junk
    )

    # -------- Volume Spike Detector --------
    VOLUME_SPIKE_ENABLED: bool = True
    VOLUME_SPIKE_RATIO: float = 5.0
    VOLUME_SPIKE_MAX_MCAP: float = 500_000_000

    # -------- Velocity Alerter (CoinGecko 1h early-pump detection) --------
    # Research-only alerts for tokens pumping hard in the last hour.
    # No paper trade dispatch -- Telegram plain-text only.
    VELOCITY_ALERTS_ENABLED: bool = False
    VELOCITY_MIN_1H_PCT: float = 30.0  # minimum 1h % change to flag
    VELOCITY_MIN_MCAP: float = 500_000  # skip dust
    VELOCITY_MAX_MCAP: float = 50_000_000  # skip mega-caps
    VELOCITY_MIN_VOL_MCAP_RATIO: float = 0.2  # vol_24h / mcap -- liquidity sanity
    VELOCITY_DEDUP_HOURS: int = 4  # re-alert cooldown per coin
    VELOCITY_TOP_N: int = 10  # max alerts per cycle

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

    # -------- GeckoTerminal Per-Chain Trending (BL-052) --------
    GT_TRENDING_TOP_N: int = 10

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
    LUNARCRUSH_POLL_INTERVAL: int = 300  # 5 min (default / normal)
    LUNARCRUSH_POLL_INTERVAL_SOFT: int = 600  # 10 min (used after 80% credits)
    LUNARCRUSH_RATE_LIMIT_PER_MIN: int = 9  # under hard 10/min
    LUNARCRUSH_DAILY_CREDIT_BUDGET: int = 2000  # free tier cap
    LUNARCRUSH_CREDIT_SOFT_PCT: float = 0.80  # downshift at 80%
    LUNARCRUSH_CREDIT_HARD_PCT: float = 0.95  # stop at 95%
    LUNARCRUSH_SOCIAL_SPIKE_RATIO: float = 2.0
    LUNARCRUSH_GALAXY_JUMP: float = 10.0
    LUNARCRUSH_INTERACTIONS_ACCEL: float = 3.0
    LUNARCRUSH_DEDUP_HOURS: int = 4
    LUNARCRUSH_TOP_N: int = 10
    LUNARCRUSH_BASELINE_MIN_HOURS: int = 24  # warmup wall-clock, interval-aware
    LUNARCRUSH_BASELINE_MIN_SAMPLES: int = 288  # EWMA alpha denominator
    LUNARCRUSH_CHECKPOINT_EVERY_N_POLLS: int = 12  # 60 min
    LUNARCRUSH_RETENTION_DAYS: int = 30
    # After N consecutive uncaught-crash-then-restart cycles, leave the
    # social tier down rather than thrash against a broken environment.
    LUNARCRUSH_MAX_CONSECUTIVE_RESTARTS: int = 5

    # -------- CryptoPanic News Feed (BL-053) --------
    # Research-only news tagging for candidate tokens. Free CryptoPanic v1 tier
    # requires a free API token; if empty, fetch short-circuits to [] without
    # hitting the network. Scoring signal exists but is gated by
    # CRYPTOPANIC_SCORING_ENABLED (off by default); flipping it on in a future
    # PR will require a SCORER_MAX_RAW bump from 183 to 193.
    CRYPTOPANIC_ENABLED: bool = False
    CRYPTOPANIC_API_TOKEN: str = ""
    CRYPTOPANIC_FETCH_FILTER: str = "hot"  # hot|rising|bullish|bearish|important
    CRYPTOPANIC_MACRO_MIN_CURRENCIES: int = 4
    CRYPTOPANIC_SCORING_ENABLED: bool = False
    CRYPTOPANIC_RETENTION_DAYS: int = 7

    # -------- Paper Trading Engine --------
    TRADING_ENABLED: bool = False  # master switch
    TRADING_MODE: str = "paper"  # "paper" or "live"
    PAPER_TRADE_AMOUNT_USD: float = 1000.0  # per trade (paper)
    PAPER_MAX_EXPOSURE_USD: float = 10000.0  # max total open (paper)
    PAPER_TP_PCT: float = 20.0  # take profit %
    PAPER_SL_PCT: float = 15.0  # BL-061: widened from 10.0
    PAPER_MAX_DURATION_HOURS: int = 48  # auto-expire
    PAPER_TP_SELL_PCT: float = 70.0  # sell 70% at TP, keep 30% as long_hold
    PAPER_SLIPPAGE_BPS: int = 50  # 0.5% slippage simulation
    PAPER_MIN_MCAP: float = 5_000_000  # min $5M mcap to paper trade (filters junk)
    # Upper mcap cap for paper trades. Large caps (BTC, ETH, SOL, AAVE...) rarely
    # pump fast enough to hit PAPER_TP_PCT within PAPER_MAX_DURATION_HOURS, so
    # they consume slots without producing wins. Signals/alerts still fire —
    # this knob only gates the paper-trade entry path.
    PAPER_MAX_MCAP: float = 500_000_000
    PAPER_MAX_MCAP_RANK: int = 1500  # skip trending coins below rank 1500 (illiquid)
    # Hard cap on concurrent open positions. Prevents restart-bursts and
    # survives env changes to PAPER_MAX_EXPOSURE_USD / PAPER_TRADE_AMOUNT_USD.
    PAPER_MAX_OPEN_TRADES: int = 10
    # Cooldown after service start: refuse to open new paper trades during
    # this window so a restart doesn't replay every currently-qualifying
    # candidate as a fresh signal. A live trader doesn't bulk-enter on reboot.
    PAPER_STARTUP_WARMUP_SECONDS: int = 180
    # Trailing stop (legacy — still used for pre-BL-061 rows; BL-061 ladder
    # uses PAPER_LADDER_TRAIL_PCT on the runner slice).
    PAPER_TRAILING_ENABLED: bool = True
    PAPER_TRAILING_ACTIVATION_PCT: float = 10.0
    PAPER_TRAILING_DRAWDOWN_PCT: float = 10.0
    PAPER_TRAILING_FLOOR_PCT: float = 3.0
    # Late-pump rejection for trade_gainers: skip candidates whose 24h change
    # already exceeds this threshold (they're near exhaustion).
    PAPER_GAINERS_MAX_24H_PCT: float = 50.0
    # BL-061 ladder: replaces flat TP/SL for post-cutover rows.
    PAPER_LADDER_LEG_1_PCT: float = 25.0
    PAPER_LADDER_LEG_1_QTY_FRAC: float = 0.30
    PAPER_LADDER_LEG_2_PCT: float = 50.0
    PAPER_LADDER_LEG_2_QTY_FRAC: float = 0.30
    PAPER_LADDER_TRAIL_PCT: float = 12.0
    PAPER_LADDER_FLOOR_ARM_ON_LEG_1: bool = True
    # BL-062 signal-stacking: require >=N scoring signals for first_signal admission
    FIRST_SIGNAL_MIN_SIGNAL_COUNT: int = 2
    # BL-062 peak-fade early-kill: sustained-fade exit between trail and expiry
    PEAK_FADE_ENABLED: bool = True
    PEAK_FADE_MIN_PEAK_PCT: float = 10.0
    PEAK_FADE_RETRACE_RATIO: float = 0.7
    TRADING_DIGEST_HOUR_UTC: int = 0  # midnight digest
    TRADING_EVAL_INTERVAL: int = 1800  # 30 min eval cycle

    # -------- Live Trading (BL-055, spec 2026-04-22) --------
    # Default LIVE_MODE=paper leaves the paper path untouched. See spec §4.
    LIVE_MODE: Literal["paper", "shadow", "live"] = "paper"

    # Sizing (CSV map overrides default per-signal; spec §4 M1)
    LIVE_TRADE_AMOUNT_USD: Decimal = Decimal("100")
    LIVE_SIGNAL_SIZES: str = ""  # e.g. "first_signal=50,gainers_early=75"

    # Exit rules (None = inherit PAPER_* via LiveConfig resolver)
    LIVE_TP_PCT: Decimal | None = None
    LIVE_SL_PCT: Decimal | None = None
    LIVE_MAX_DURATION_HOURS: int | None = None

    # Execution quality
    LIVE_SLIPPAGE_BPS_CAP: int = 50
    LIVE_DEPTH_HEALTH_MULTIPLIER: Decimal = Decimal("3")
    LIVE_VENUE_PREFERENCE: str = "binance"  # CSV in v2; v1 is Binance-only

    # Risk gates
    LIVE_DAILY_LOSS_CAP_USD: Decimal = Decimal("50")
    LIVE_MAX_EXPOSURE_USD: Decimal = Decimal("500")
    LIVE_MAX_OPEN_POSITIONS: int = 5

    # Signal allowlist — CSV, lowercased, trimmed; empty = no signals eligible
    LIVE_SIGNAL_ALLOWLIST: str = ""

    # Credentials (live mode only; never in .env.example — see spec §4.4)
    BINANCE_API_KEY: SecretStr | None = None
    BINANCE_API_SECRET: SecretStr | None = None

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

    # -------- Perp WebSocket Anomaly Detector (BL-054) --------
    # Research-only, default-off. PERP_ENABLED gates data collection;
    # PERP_SCORING_ENABLED gates scorer signal separately. Flipping
    # PERP_SCORING_ENABLED alone does NOT affect scoring -- the scorer
    # also requires SCORER_MAX_RAW >= _PERP_ENABLED_MAX_RAW (runtime guard
    # in scorer.py), which after the BL-054 recalibration PR ships as 208
    # (equal to _PERP_ENABLED_MAX_RAW=208, so the guard opens by default).
    # Full design in
    # docs/superpowers/specs/2026-04-20-bl054-perp-ws-anomaly-detector-design.md.
    PERP_ENABLED: bool = False
    PERP_SCORING_ENABLED: bool = False
    PERP_BINANCE_ENABLED: bool = True
    PERP_BYBIT_ENABLED: bool = True
    PERP_BINANCE_WS_URL: str = "wss://fstream.binance.com/stream"
    PERP_SYMBOLS: list[str] = []
    PERP_FUNDING_FLIP_MIN_PCT: float = 0.05
    PERP_OI_SPIKE_RATIO: float = 3.0
    PERP_BASELINE_ALPHA: float = 0.1
    PERP_BASELINE_MIN_SAMPLES: int = 30
    PERP_BASELINE_MAX_KEYS: int = 1000
    PERP_BASELINE_IDLE_EVICT_SEC: int = 3600
    PERP_ANOMALY_LOOKBACK_MIN: int = 15
    PERP_ANOMALY_DEDUP_MIN: int = 5
    PERP_ANOMALY_RETENTION_DAYS: int = 7
    PERP_MAX_CONSECUTIVE_RESTARTS: int = 5
    PERP_CIRCUIT_BREAK_SEC: int = 3600
    PERP_WS_PING_INTERVAL_SEC: int = 20
    PERP_WS_RECONNECT_MAX_SEC: int = 60
    PERP_QUEUE_MAXSIZE: int = 2048
    PERP_DB_FLUSH_INTERVAL_SEC: float = 2.0
    PERP_DB_FLUSH_MAX_ROWS: int = 100

    # -------- BL-055 computed fields (spec §4.1) --------
    @computed_field
    @property
    def live_signal_allowlist_set(self) -> frozenset[str]:
        """Parse LIVE_SIGNAL_ALLOWLIST CSV into a lowercased, trimmed frozenset."""
        if not self.LIVE_SIGNAL_ALLOWLIST:
            return frozenset()
        return frozenset(
            s.strip().lower()
            for s in self.LIVE_SIGNAL_ALLOWLIST.split(",")
            if s.strip()
        )

    @computed_field
    @property
    def live_signal_sizes_map(self) -> dict[str, Decimal]:
        """Parse LIVE_SIGNAL_SIZES CSV of name=amount pairs.

        Raises ValueError on any malformed entry (missing '=' or empty key/value).
        """
        if not self.LIVE_SIGNAL_SIZES:
            return {}
        out: dict[str, Decimal] = {}
        for pair in self.LIVE_SIGNAL_SIZES.split(","):
            pair = pair.strip()
            if not pair:
                continue
            k, sep, v = pair.partition("=")
            k = k.strip().lower()
            if not sep or not k or not v.strip():
                raise ValueError(
                    f"LIVE_SIGNAL_SIZES malformed entry: {pair!r}"
                )
            out[k] = Decimal(v.strip())
        return out

    @field_validator(
        "PAPER_TRAILING_ACTIVATION_PCT",
        "PAPER_TRAILING_DRAWDOWN_PCT",
        "PAPER_TRAILING_FLOOR_PCT",
    )
    @classmethod
    def _validate_paper_trailing_pct(cls, v: float) -> float:
        if v < 0 or v > 100:
            raise ValueError(
                "PAPER_TRAILING_* percent knobs must be in [0, 100]; "
                f"drawdown > 100 yields a negative stop price. got={v}"
            )
        return v

    @field_validator("PAPER_GAINERS_MAX_24H_PCT")
    @classmethod
    def _validate_gainers_max_24h(cls, v: float) -> float:
        if v < 0:
            raise ValueError("PAPER_GAINERS_MAX_24H_PCT must be >= 0 (0 disables)")
        return v

    @field_validator("PAPER_SL_PCT")
    @classmethod
    def _validate_paper_sl_pct(cls, v: float) -> float:
        if v < 0:
            raise ValueError("sl_pct must be positive, e.g. 10.0 for 10% stop loss")
        return v

    @field_validator("PAPER_TP_PCT")
    @classmethod
    def _validate_paper_tp_pct(cls, v: float) -> float:
        if v < 0:
            raise ValueError("tp_pct must be positive, e.g. 20.0 for 20% take profit")
        return v

    @field_validator("PAPER_LADDER_LEG_1_QTY_FRAC", "PAPER_LADDER_LEG_2_QTY_FRAC")
    @classmethod
    def _validate_ladder_qty_frac(cls, v: float) -> float:
        if not (0.0 < v <= 1.0):
            raise ValueError(
                "PAPER_LADDER_*_QTY_FRAC must be in (0, 1]; "
                f"got={v} — fractions > 1 would oversell the position"
            )
        return v

    @field_validator("FIRST_SIGNAL_MIN_SIGNAL_COUNT")
    @classmethod
    def _validate_first_signal_min_count(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                f"FIRST_SIGNAL_MIN_SIGNAL_COUNT must be >= 1; got={v}"
            )
        return v

    @field_validator("PEAK_FADE_MIN_PEAK_PCT")
    @classmethod
    def _validate_peak_fade_min_peak_pct(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(
                f"PEAK_FADE_MIN_PEAK_PCT must be > 0; got={v}"
            )
        return v

    @field_validator("PEAK_FADE_RETRACE_RATIO")
    @classmethod
    def _validate_peak_fade_retrace_ratio(cls, v: float) -> float:
        if not (0.0 < v < 1.0):
            raise ValueError(
                f"PEAK_FADE_RETRACE_RATIO must be in (0, 1); got={v}"
            )
        return v

    @field_validator("PAPER_MAX_MCAP")
    @classmethod
    def _validate_paper_max_mcap(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(
                "PAPER_MAX_MCAP must be > 0 (paper-trade large-cap filter)"
            )
        return v

    @field_validator("CHAIN_PROMOTION_THRESHOLD", "CHAIN_GRADUATION_HIT_RATE")
    @classmethod
    def _validate_hit_rate_thresholds(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @field_validator("CHAIN_MIN_TRIGGERS_FOR_STATS", "CHAIN_GRADUATION_MIN_TRIGGERS")
    @classmethod
    def _validate_min_triggers(cls, v: int) -> int:
        return max(1, v)

    @field_validator("CHAINS", mode="before")
    @classmethod
    def parse_chains(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [c.strip() for c in v.split(",") if c.strip()]
        return v

    @field_validator("PERP_SYMBOLS", mode="before")
    @classmethod
    def parse_perp_symbols(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            v = [s.strip().upper() for s in v.split(",") if s.strip()]
        elif isinstance(v, list):
            v = [str(s).strip().upper() for s in v if str(s).strip()]
        if len(v) > 200:
            # Binance URL-length + subscription-rate safety (design spec §3.4).
            raise ValueError("PERP_SYMBOLS exceeds max length 200")
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

    @model_validator(mode="after")
    def validate_ladder_qty_fracs_leave_runner(self) -> "Settings":
        """leg_1 + leg_2 must be < 1.0 so a runner slice remains for trail/floor.

        Runner is implicit: 1.0 - leg_1_frac - leg_2_frac. If it's 0, the ladder
        degenerates (trail/floor have no qty to close) — reject before runtime.
        """
        total = self.PAPER_LADDER_LEG_1_QTY_FRAC + self.PAPER_LADDER_LEG_2_QTY_FRAC
        if total >= 1.0:
            raise ValueError(
                f"PAPER_LADDER_LEG_1_QTY_FRAC ({self.PAPER_LADDER_LEG_1_QTY_FRAC}) + "
                f"PAPER_LADDER_LEG_2_QTY_FRAC ({self.PAPER_LADDER_LEG_2_QTY_FRAC}) "
                f"= {total}, must be < 1.0 to leave a runner slice"
            )
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
