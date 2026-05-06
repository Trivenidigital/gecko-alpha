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
    # BL-071a' v3 (2026-05-04): outcome resolution + health-monitoring tunables
    CHAIN_OUTCOME_HIT_THRESHOLD_PCT: float = (
        50.0  # memecoin chain hit if (current_fdv/completion_fdv - 1)*100 >= this
    )
    CHAIN_OUTCOME_MIN_MCAP_USD: float = (
        1000.0  # writer skips dust mcap that would produce fake hits at hydrate
    )
    CHAIN_OUTCOME_PERSISTENT_FAILURE_HOURS: float = (
        1.0  # ERROR threshold for stuck-row aging
    )
    CHAIN_TRACKER_UNHEALTHY_FAILURE_RATE: float = (
        0.5  # 50% of attempts → session-unhealthy ERROR
    )
    CHAIN_TRACKER_UNHEALTHY_MIN_ATTEMPTS: int = 3  # floor — don't ERROR on 1-row cycles

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
    # Adaptive trail (2026-04-28): when a trade's peak_pct is below the
    # low-peak threshold, use a tighter trail to harvest profit on modest
    # peakers before they fade. When peak ≥ threshold, the full
    # PAPER_LADDER_TRAIL_PCT applies. Post-moonshot, the moonshot trail
    # always wins. Must be < PAPER_LADDER_TRAIL_PCT.
    PAPER_LADDER_TRAIL_PCT_LOW_PEAK: float = 8.0
    PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT: float = 20.0
    PAPER_LADDER_FLOOR_ARM_ON_LEG_1: bool = True
    # Per-signal-type kill switches (2026-04-28 strategy review). Net-loser
    # signals can be disabled at their call sites without removing source
    # code — flip via .env when the underlying market behavior changes.
    PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED: bool = True
    PAPER_SIGNAL_TRENDING_CATCH_ENABLED: bool = True
    # BL-063 moonshot exit upgrade: when peak_pct crosses MOONSHOT_THRESHOLD_PCT,
    # widen the BL-061 ladder trail from PAPER_LADDER_TRAIL_PCT to
    # PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT so big runners aren't clipped early.
    PAPER_MOONSHOT_ENABLED: bool = False
    PAPER_MOONSHOT_THRESHOLD_PCT: float = 40.0
    PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT: float = 30.0

    # BL-067 conviction-lock: when N>= PAPER_CONVICTION_LOCK_THRESHOLD
    # distinct signals fire on the same token within a 504h window, widen
    # the trade's trail_pct / sl_pct / max_duration_hours per the spec
    # table at backlog.md:374-380. Master kill-switch defaults False; per-
    # signal opt-in via signal_params.conviction_lock_enabled (default 0).
    # Validated by tasks/findings_bl067_backtest_conviction_lock.md
    # (lift +114% at N=3 threshold, both compound gates PASS). Default
    # fail-closed.
    PAPER_CONVICTION_LOCK_ENABLED: bool = False
    PAPER_CONVICTION_LOCK_THRESHOLD: int = 3

    # BL-064 TG Social Signals — Telethon user-session listener for curated TG
    # channels. Default OFF. Auto-read 3-4 watched channels, parse cashtags +
    # contract addresses, alert always, paper-trade via TradingEngine when
    # CA-resolved + admission gates pass. Spec: 2026-04-27-bl064-...md.
    TG_SOCIAL_ENABLED: bool = False
    TG_SOCIAL_API_ID: int = 0
    TG_SOCIAL_API_HASH: SecretStr | None = None
    TG_SOCIAL_PHONE_NUMBER: str = ""
    TG_SOCIAL_SESSION_PATH: Path = Path("./tg_social.session")
    TG_SOCIAL_CHANNELS_FILE: Path = Path("./channels.yml")
    TG_SOCIAL_MAX_OPEN_TRADES: int = 5
    PAPER_TG_SOCIAL_TRADE_AMOUNT_USD: float = 300.0
    # BL-065 v3 (Bundle B 2026-05-04): cashtag-only dispatch tunables.
    # R2#7 v3: Field validators in a separate @field_validator below so
    # invalid .env values fail at startup, not at first dispatch.
    PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD: float = 300.0
    PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD: float = 100_000.0
    PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO: float = 2.0
    PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY: int = 5
    TG_SOCIAL_CATCHUP_LIMIT: int = 200
    TG_SOCIAL_FLOOD_WAIT_MAX_SEC: int = 600
    TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC: int = 300
    TG_SOCIAL_RESOLUTION_RETRY_DELAY_SEC: int = 60
    TG_SOCIAL_CHANNEL_SILENCE_ALERT_HOURS: int = 72
    TG_SOCIAL_CHANNEL_SILENCE_CHECK_INTERVAL_SEC: int = 3600
    # BL-062 signal-stacking: require >=N scoring signals for first_signal admission
    FIRST_SIGNAL_MIN_SIGNAL_COUNT: int = 2
    # BL-062 peak-fade early-kill: sustained-fade exit between trail and expiry
    PEAK_FADE_ENABLED: bool = True
    PEAK_FADE_MIN_PEAK_PCT: float = 10.0
    PEAK_FADE_RETRACE_RATIO: float = 0.7
    # BL-NEW-HPF high-peak fade — single-pass tighter exit on confirmed runners.
    # Fires when peak_pct >= MIN_PEAK_PCT AND current price has retraced
    # >= RETRACE_PCT from peak. Tighter than moonshot trail (30%) because
    # the cohort can afford it: capture > give-back at this peak.
    # See tasks/findings_high_peak_giveback.md §14 for backtest evidence
    # (n=15 cohort at 60%, +$650 lift, bootstrap p5=$23, slippage-robust to 500bps).
    PAPER_HIGH_PEAK_FADE_ENABLED: bool = False  # master kill, default off
    PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT: float = 60.0  # §14 sweet spot — n=15, p5=$23
    PAPER_HIGH_PEAK_FADE_RETRACE_PCT: float = 15.0  # tighter than moonshot 30%
    PAPER_HIGH_PEAK_FADE_DRY_RUN: bool = True  # log-only initially
    PAPER_HIGH_PEAK_FADE_PER_SIGNAL_OPT_IN: bool = (
        True  # require signal_params.high_peak_fade_enabled=1
    )
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

    # Tier 1a + 1b (signal-params self-tuning). Default OFF — first deploy
    # is a no-op: the migration seeds the table from current Settings, but
    # the evaluator/engine keep reading from Settings until the flag flips.
    SIGNAL_PARAMS_ENABLED: bool = False
    # Auto-suspension thresholds. PNL_THRESHOLD requires at least MIN_TRADES;
    # HARD_LOSS bypasses the trade floor for catastrophic bleed.
    SIGNAL_SUSPEND_PNL_THRESHOLD_USD: float = -200.0
    SIGNAL_SUSPEND_HARD_LOSS_USD: float = -500.0
    SIGNAL_SUSPEND_MIN_TRADES: int = 50
    SUSPENSION_CHECK_HOUR: int = 1  # local hour, in-loop scheduler
    # Calibration — dry-run by default; --apply gated on Telegram health
    # unless --force-no-alert. Trade-count floor mirrors suspension floor
    # so we don't tune on noise.
    CALIBRATION_MIN_TRADES: int = 50
    CALIBRATION_WINDOW_DAYS: int = 30
    CALIBRATION_STEP_SIZE_PCT: float = 2.0
    # Weekly scheduled --dry-run + Telegram alert (no auto-apply).
    # Operator reviews diff in chat, manually re-runs --apply if approved.
    CALIBRATION_DRY_RUN_ENABLED: bool = True
    CALIBRATION_DRY_RUN_WEEKDAY: int = 0  # 0=Mon (matches WEEKLY_DIGEST_WEEKDAY)
    CALIBRATION_DRY_RUN_HOUR: int = 2  # local hour
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
                raise ValueError(f"LIVE_SIGNAL_SIZES malformed entry: {pair!r}")
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

    @field_validator("PAPER_LADDER_TRAIL_PCT")
    @classmethod
    def _validate_paper_ladder_trail_pct(cls, v: float) -> float:
        # Must be strictly positive: a 0 override would make trail_threshold
        # equal to peak_price, firing on every tick after peak. The cross-field
        # moonshot validator below assumes this is also a meaningful baseline.
        if not (0 < v < 100):
            raise ValueError(
                "PAPER_LADDER_TRAIL_PCT must be in (0, 100); "
                f"got={v} (0 fires on every tick after peak; >=100 yields negative trail price)"
            )
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
            raise ValueError(f"FIRST_SIGNAL_MIN_SIGNAL_COUNT must be >= 1; got={v}")
        return v

    @field_validator("PAPER_CONVICTION_LOCK_THRESHOLD")
    @classmethod
    def _validate_conviction_lock_threshold(cls, v: int) -> int:
        # Lower bound 2: stack=1 means no independent signals fired;
        # nothing to "lock" against.
        # Upper bound 50 (PR-review M2 relaxation): operator escape hatch
        # — previously hard-capped at 11 (highest observed stack 30d),
        # but operators may want to effectively-disable lock for one
        # signal_type via threshold > observed-max without flipping the
        # per-signal flag. Above 11 is unusual; an explicit log noise
        # would be ideal but field validators can't log cleanly. Above
        # 50 is almost certainly a typo.
        if v < 2:
            raise ValueError(
                "PAPER_CONVICTION_LOCK_THRESHOLD must be >= 2 "
                f"(stack=1 means no independent signals fired); got={v}"
            )
        if v > 50:
            raise ValueError(
                "PAPER_CONVICTION_LOCK_THRESHOLD must be <= 50 "
                f"(observed max=11 over 30d; values > 50 likely a typo); "
                f"got={v}"
            )
        return v

    @field_validator("PEAK_FADE_MIN_PEAK_PCT")
    @classmethod
    def _validate_peak_fade_min_peak_pct(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"PEAK_FADE_MIN_PEAK_PCT must be > 0; got={v}")
        return v

    @field_validator("PEAK_FADE_RETRACE_RATIO")
    @classmethod
    def _validate_peak_fade_retrace_ratio(cls, v: float) -> float:
        if not (0.0 < v < 1.0):
            raise ValueError(f"PEAK_FADE_RETRACE_RATIO must be in (0, 1); got={v}")
        return v

    @field_validator("PAPER_HIGH_PEAK_FADE_RETRACE_PCT")
    @classmethod
    def _validate_high_peak_fade_retrace_pct(cls, v: float) -> float:
        if not (0 < v < 100):
            raise ValueError(
                f"PAPER_HIGH_PEAK_FADE_RETRACE_PCT must be in (0, 100); got={v}"
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

    @field_validator("TG_SOCIAL_MAX_OPEN_TRADES")
    @classmethod
    def _validate_tg_social_max_open_trades(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"TG_SOCIAL_MAX_OPEN_TRADES must be >= 1; got={v}")
        return v

    @field_validator("PAPER_TG_SOCIAL_TRADE_AMOUNT_USD")
    @classmethod
    def _validate_paper_tg_social_trade_amount(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"PAPER_TG_SOCIAL_TRADE_AMOUNT_USD must be > 0; got={v}")
        return v

    # BL-065 v3 (R2#7): cashtag-dispatch field validators
    @field_validator("PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD")
    @classmethod
    def _validate_cashtag_trade_amount(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(
                f"PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD must be > 0; got={v}"
            )
        return v

    @field_validator("PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD")
    @classmethod
    def _validate_cashtag_min_mcap(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(
                f"PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD must be > 0; got={v}"
            )
        return v

    @field_validator("PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO")
    @classmethod
    def _validate_cashtag_disambiguity_ratio(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError(
                f"PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO must be >= 1.0; got={v}"
            )
        return v

    @field_validator("PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY")
    @classmethod
    def _validate_cashtag_max_per_channel_per_day(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                f"PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY must be >= 1; got={v}"
            )
        return v

    @field_validator("TG_SOCIAL_CATCHUP_LIMIT")
    @classmethod
    def _validate_tg_social_catchup_limit(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"TG_SOCIAL_CATCHUP_LIMIT must be >= 0; got={v}")
        return v

    @field_validator("TG_SOCIAL_FLOOD_WAIT_MAX_SEC")
    @classmethod
    def _validate_tg_social_flood_wait_max(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"TG_SOCIAL_FLOOD_WAIT_MAX_SEC must be > 0; got={v}")
        return v

    @field_validator("TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC")
    @classmethod
    def _validate_tg_social_channel_reload(cls, v: int) -> int:
        # BL-064 channel-reload PR — operator escape-hatch: 0 disables
        # the reload heartbeat entirely (returns immediately + emits
        # `tg_social_channel_reload_disabled` log). All other values
        # must be >= 60 to prevent DB hot-loops (anti-thrash).
        if v != 0 and v < 60:
            raise ValueError(
                "TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC must be >= 60 "
                f"(anti-thrash) or exactly 0 (disable); got={v}"
            )
        return v

    @field_validator("TG_SOCIAL_RESOLUTION_RETRY_DELAY_SEC")
    @classmethod
    def _validate_tg_social_resolution_retry(cls, v: int) -> int:
        if v < 0:
            raise ValueError(
                f"TG_SOCIAL_RESOLUTION_RETRY_DELAY_SEC must be >= 0; got={v}"
            )
        return v

    @field_validator("TG_SOCIAL_CHANNEL_SILENCE_ALERT_HOURS")
    @classmethod
    def _validate_tg_social_silence_hours(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                f"TG_SOCIAL_CHANNEL_SILENCE_ALERT_HOURS must be >= 1; got={v}"
            )
        return v

    @field_validator("CALIBRATION_DRY_RUN_WEEKDAY")
    @classmethod
    def _validate_calibration_dry_run_weekday(cls, v: int) -> int:
        if not 0 <= v <= 6:
            raise ValueError(
                f"CALIBRATION_DRY_RUN_WEEKDAY must be 0-6 (Mon-Sun); got={v}"
            )
        return v

    @field_validator("CALIBRATION_DRY_RUN_HOUR")
    @classmethod
    def _validate_calibration_dry_run_hour(cls, v: int) -> int:
        if not 0 <= v <= 23:
            raise ValueError(f"CALIBRATION_DRY_RUN_HOUR must be 0-23; got={v}")
        return v

    @model_validator(mode="after")
    def _validate_tg_social_creds(self) -> "Settings":
        # Value-only check — filesystem (.session existence) is checked at
        # listener startup in scout/social/telegram/client.py with an
        # actionable error message including the bootstrap command.
        if self.TG_SOCIAL_ENABLED:
            if self.TG_SOCIAL_API_ID <= 0:
                raise ValueError(
                    "TG_SOCIAL_ENABLED=True requires TG_SOCIAL_API_ID > 0; "
                    "get one from https://my.telegram.org -> API Development tools"
                )
            if self.TG_SOCIAL_API_HASH is None:
                raise ValueError(
                    "TG_SOCIAL_ENABLED=True requires TG_SOCIAL_API_HASH; "
                    "get one from https://my.telegram.org -> API Development tools"
                )
        return self

    @model_validator(mode="after")
    def _validate_moonshot(self) -> "Settings":
        # Threshold must be positive — a non-positive threshold would arm every
        # trade at open, defeating the purpose.
        if self.PAPER_MOONSHOT_THRESHOLD_PCT <= 0:
            raise ValueError(
                "PAPER_MOONSHOT_THRESHOLD_PCT must be > 0; "
                f"got={self.PAPER_MOONSHOT_THRESHOLD_PCT}"
            )
        # Drawdown in (0, 100). >= 100 would silently disable trailing
        # entirely (trail price <= 0 never triggers); <= 0 would fire on any
        # tick.
        if not (0 < self.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT < 100):
            raise ValueError(
                "PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT must be in (0, 100); "
                f"got={self.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT}"
            )
        # Cross-field guard: moonshot must WIDEN the ladder trail, never
        # tighten it. A misconfig that tightens at the threshold would clip
        # runners harder than baseline — silent regression.
        if self.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT <= self.PAPER_LADDER_TRAIL_PCT:
            raise ValueError(
                "PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT must be > "
                "PAPER_LADDER_TRAIL_PCT (moonshot widens the trail); "
                f"got moonshot={self.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT}, "
                f"ladder={self.PAPER_LADDER_TRAIL_PCT}"
            )
        return self

    @model_validator(mode="after")
    def _validate_low_peak_trail(self) -> "Settings":
        """Adaptive trail invariants:
          - low_peak trail must be in (0, 100)
          - low_peak threshold must be in (0, moonshot_threshold)
          - low_peak trail must be < full trail (tighter on modest peakers)

        A misconfigured low_peak ≥ full would silently INVERT the strategy —
        modest peakers would have looser trail than runners.
        """
        if not (0 < self.PAPER_LADDER_TRAIL_PCT_LOW_PEAK < 100):
            raise ValueError(
                "PAPER_LADDER_TRAIL_PCT_LOW_PEAK must be in (0, 100); "
                f"got={self.PAPER_LADDER_TRAIL_PCT_LOW_PEAK}"
            )
        if self.PAPER_LADDER_TRAIL_PCT_LOW_PEAK >= self.PAPER_LADDER_TRAIL_PCT:
            raise ValueError(
                "PAPER_LADDER_TRAIL_PCT_LOW_PEAK must be < PAPER_LADDER_TRAIL_PCT "
                "(tighter trail on modest peakers); "
                f"got low_peak={self.PAPER_LADDER_TRAIL_PCT_LOW_PEAK}, "
                f"full={self.PAPER_LADDER_TRAIL_PCT}"
            )
        if self.PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT <= 0:
            raise ValueError(
                "PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT must be > 0; "
                f"got={self.PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT}"
            )
        # If moonshot is enabled, low-peak threshold must be below the moonshot
        # threshold. Otherwise a peak in [moonshot, low_peak] is ambiguous —
        # moonshot logic catches it via moonshot_armed_at, but the read-order
        # in the evaluator picks low_peak vs full BEFORE checking moonshot_armed,
        # so an inverted relationship would mean a peak ≥ moonshot uses the
        # tighter trail until the next eval pass arms moonshot. The ordering
        # invariant is `low_peak_threshold < moonshot_threshold`.
        if (
            self.PAPER_MOONSHOT_ENABLED
            and self.PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT
            >= self.PAPER_MOONSHOT_THRESHOLD_PCT
        ):
            raise ValueError(
                "PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT must be < "
                "PAPER_MOONSHOT_THRESHOLD_PCT when moonshot is enabled; "
                f"got low_peak={self.PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT}, "
                f"moonshot={self.PAPER_MOONSHOT_THRESHOLD_PCT}"
            )
        return self

    @model_validator(mode="after")
    def _validate_high_peak_fade_cross_fields(self) -> "Settings":
        # MIN_PEAK_PCT must be > moonshot threshold so the gate only fires
        # in the moonshot regime (peak >= 40%). Below that, the regular
        # adaptive trail (sp.trail_pct_low_peak / sp.trail_pct) handles it.
        if self.PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT <= self.PAPER_MOONSHOT_THRESHOLD_PCT:
            raise ValueError(
                "PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT must be > "
                "PAPER_MOONSHOT_THRESHOLD_PCT (gate targets moonshot regime); "
                f"got high_peak={self.PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT}, "
                f"moonshot={self.PAPER_MOONSHOT_THRESHOLD_PCT}"
            )
        # RETRACE_PCT must be tighter than the moonshot trail, otherwise
        # the gate is a no-op (moonshot trail fires first).
        if (
            self.PAPER_HIGH_PEAK_FADE_RETRACE_PCT
            >= self.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT
        ):
            raise ValueError(
                "PAPER_HIGH_PEAK_FADE_RETRACE_PCT must be < "
                "PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT (must be tighter than "
                "moonshot trail); "
                f"got retrace={self.PAPER_HIGH_PEAK_FADE_RETRACE_PCT}, "
                f"moonshot_trail={self.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT}"
            )
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
