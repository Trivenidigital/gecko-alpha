"""Application configuration via Pydantic BaseSettings."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root, derived once at import time. Anchors TG_SOCIAL_SESSION_PATH /
# CHANNELS_FILE defaults so they don't depend on CWD ("./tg_social.session"
# resolves differently for systemd starts vs ad-hoc CLI invocations). The
# environment-variable overrides for these fields still take precedence —
# this only fixes the DEFAULT.
_REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # Scanner
    # next-audit-trigger: 2026-11-13 OR SCAN_INTERVAL value change OR new external API OR
    # new *_CYCLES setting OR score_history/volume_snapshots write-rate +/- 2x.
    # See tasks/findings_cycle_change_audit_2026_05_13.md sec 5.
    # Bounds rationale: ge=1 (0 would be a tight-loop CPU burn); le=3600
    # (1h ceiling — operator intent of slower cadence is fine, but >1h
    # likely a misconfig that should fail-fast at startup).
    SCAN_INTERVAL_SECONDS: int = Field(default=60, ge=1, le=3600)
    HEARTBEAT_INTERVAL_SECONDS: int = Field(
        default=300, ge=1, le=3600
    )  # BL-033: periodic heartbeat summary
    INGEST_WATCHDOG_ENABLED: bool = True
    INGEST_STARVATION_THRESHOLD_CYCLES: int = Field(default=5, ge=1, le=100)

    # --- Signal outcome ledger (P0, edge-audit 2026-07-02) -----------------
    # Observe-only writer + in-DB labeler: every emission (candidate alert,
    # paper-trade dispatch, sampled gate-block) self-labels with forward
    # returns resolved from volume_history_cg + price_cache ONLY — no sends,
    # no external API calls, zero rate-limit budget. Default-on approved by
    # operator ("start now"); LEDGER_ENABLED=False is the kill switch,
    # respected at all three write sites and the hourly labeler.
    LEDGER_ENABLED: bool = True
    # 1-in-N sampling of blocked trade_decision emissions (0 = off). le=1e5
    # admits "effectively off" without the 0 sentinel.
    LEDGER_GATED_OUT_SAMPLE_RATE: int = Field(default=25, ge=0, le=100_000)
    # Dispatcher-layer suppressed-block recording (edge audit Phase 3). The
    # engine's GatedOutSampler covers only engine-level blocks; dispatcher
    # suppression (scout/trading/signals.py should_open -> reason='suppressed')
    # is a DIFFERENT path that never reaches that sampler, yet it is the
    # dominant winner-killer (12 of 24 >=5x winners). Recorded AT EMISSION,
    # NOT 1-in-N sampled — every suppressed block is the priority cohort, so
    # the gate-counterfactual / recall lane sees the exact block class that
    # killed those winners before the reopening experiment measures recall.
    # Gated by this flag AND LEDGER_ENABLED (global kill switch); flag lets
    # the lane be disabled without touching the rest of the ledger.
    LEDGER_SAMPLE_SUPPRESSED: bool = True
    # Max pending/partial rows examined per hourly labeling pass — bounds the
    # per-pass read/write load on the hot-loop DB connection.
    LEDGER_LABEL_BATCH_MAX: int = Field(default=500, ge=1, le=100_000)
    # price_cache fallback freshness: a cache row may stand in for a horizon
    # price only if observed within this many minutes AFTER the horizon
    # deadline (volume_history_cg rows are preferred and un-bounded — they are
    # true historical observations). 120 min covers the hourly pass + margin;
    # le = 7 days (the longest horizon).
    LEDGER_PRICE_CACHE_MAX_LATENESS_MINUTES: int = Field(default=120, ge=1, le=10_080)
    # Enrollment-at-emission: emissions whose token has no LIVE in-DB price
    # coverage (all gated_out_samples + priceless alerts) enroll the token
    # into a forward-polling set so the labeler can price it. TTL matches
    # the 7d labeling window; cap bounds the per-cycle polling cost
    # (oldest-expire-first eviction).
    LEDGER_ENROLLMENT_TTL_DAYS: int = Field(default=7, ge=1, le=90)
    LEDGER_ENROLLMENT_MAX_ACTIVE: int = Field(default=200, ge=1, le=10_000)
    # Coverage = LIVENESS, not shape (2026-07-03 operator condition (b) on the
    # #423->#421 pair). A gated_out / priceless emission is treated as
    # already-covered (no enrollment) ONLY when a FRESH price observation
    # exists within this many minutes of now — in price_cache.updated_at OR the
    # latest volume_history_cg.recorded_at. Rationale: this window ties to the
    # pipeline / poller cadence — a token with a price observation inside the
    # last hour is being actively served by the existing lanes and will be
    # labelable from in-DB data WITHOUT enrollment; anything older is treated
    # as feed-dead and enrolled so the poller keeps a live price. A DEAD-but-
    # valid CG slug or a STALE price_cache row therefore no longer reads as
    # "covered" (the shape/existence heuristic did, undercounting dead
    # suppressed tokens and biasing the suppressed cohort's returns upward).
    # le = 7 days (the longest labeling horizon).
    LEDGER_COVERAGE_FRESHNESS_MIN: int = Field(default=60, ge=1, le=10_080)
    # MIN_SCORE / CONVICTION_THRESHOLD are 0..100 scores in normal use,
    # but tests + operator circuit-breakers use sentinel values like
    # 999 to mean "disable this gate entirely". ge=0 catches sign typos;
    # le=10_000 catches accidental "MIN_SCORE=9999999" while still
    # admitting the deliberate-disable pattern.
    MIN_SCORE: int = Field(default=65, ge=0, le=10_000)
    CONVICTION_THRESHOLD: int = Field(default=75, ge=0, le=10_000)
    QUANT_WEIGHT: float = 0.6
    NARRATIVE_WEIGHT: float = 0.4

    # Token filters
    MIN_MARKET_CAP: float = 10_000
    MAX_MARKET_CAP: float = 500_000
    MAX_TOKEN_AGE_DAYS: int = 7
    MIN_LIQUIDITY_USD: float = Field(default=15_000, ge=0)
    MIN_VOL_LIQ_RATIO: float = Field(default=5.0, ge=0)
    # Fraction-domain threshold (0..1). Values outside that range either
    # invert detection (negative) or never fire (>1). Fail-fast at startup.
    BUY_PRESSURE_THRESHOLD: float = Field(default=0.65, ge=0.0, le=1.0)
    CO_OCCURRENCE_MIN_SIGNALS: int = Field(default=3, ge=1, le=20)
    CO_OCCURRENCE_MULTIPLIER: float = Field(default=1.15, ge=1.0, le=5.0)

    # BL-NEW-QUOTE-PAIR: stable-pair liquidity-quality signal.
    # Tokens whose DexScreener quoteToken.symbol is in STABLE_QUOTE_SYMBOLS AND
    # whose liquidity_usd >= STABLE_PAIRED_LIQ_THRESHOLD_USD get +5 raw / +2
    # normalized points. Counts toward co-occurrence multiplier (intended).
    # 2026-05-09 stable list: PYUSD/RLUSD/sUSDe added; BUSD/TUSD intentionally
    # excluded (BUSD redemption-only since 2024-02; TUSD repeat depegs).
    STABLE_QUOTE_SYMBOLS: tuple[str, ...] = (
        "USDC",
        "USDT",
        "DAI",
        "FDUSD",
        "USDe",
        "PYUSD",
        "RLUSD",
        "sUSDe",
    )
    STABLE_PAIRED_LIQ_THRESHOLD_USD: float = 50_000.0
    STABLE_PAIRED_BONUS: int = 5
    CHAINS: list[str] = ["solana", "base", "ethereum"]

    # CoinGecko
    # Fraction-domain (0..1). 0 fires for any positive 1h move; >1 never fires.
    MOMENTUM_RATIO_THRESHOLD: float = Field(default=0.6, ge=0.0, le=1.0)
    # Minimum absolute 24h price change (%) required for momentum_ratio to fire.
    # Prevents stablecoin peg-wobble (0.05%/0.08% -> ratio 0.625 > 0.6) from triggering.
    MOMENTUM_MIN_24H_CHANGE_PCT: float = 3.0
    MIN_VOL_ACCEL_RATIO: float = 5.0
    COINGECKO_API_KEY: str = ""
    COINGECKO_RATE_LIMIT_PER_MIN: int = 25  # buffer under 30/min free tier
    # Smooth concurrent CoinGecko lanes so the scanner stays under provider
    # burst/concurrency throttles, not only the rolling minute cap.
    COINGECKO_MIN_REQUEST_INTERVAL_SEC: float = 0.75
    COINGECKO_REQUEST_JITTER_SEC: float = 0.25
    # Provider-side 429 means the shared IP/key budget is exhausted. Do not
    # retry immediately inside the same cycle; pause the whole CG lane instead.
    COINGECKO_429_COOLDOWN_SEC: float = 120.0
    # Default keeps the main-cycle scheduled CoinGecko calls at about 8/min:
    # top_movers uses 2, trending hydration uses 2, volume scan uses 3,
    # held-position refresh can add 1 when enabled, and midcap scan averages
    # +1/min under its default 3-cycle cadence. Raise only with rate-budget
    # review against the 25/min limiter.
    COINGECKO_VOLUME_SCAN_PAGES: int = 3
    # BL-NEW-COINGECKO-MIDCAP-GAINER-SCAN: rank-band scan for CoinGecko
    # gainers that are not top-volume and not trending. Cadence and output cap
    # keep this quality-first under the free-tier limiter.
    # Reallocated 2026-06-02 (Increment 2): disabled in favor of the proactive
    # rotating deep-volume page below. Budget-NEUTRAL (deep page +1/cycle; midcap
    # was 3 pages / 3 cycles = -1/cycle avg) and smoother. Midcap is reactive
    # (24h>=25%) + starts at $10M, so it cannot cover the $500K-$10M residual gap.
    # Revert: re-enable this + set COINGECKO_DEEP_VOLUME_ENABLED=False.
    COINGECKO_MIDCAP_SCAN_ENABLED: bool = False
    COINGECKO_MIDCAP_SCAN_INTERVAL_CYCLES: int = 3
    COINGECKO_MIDCAP_SCAN_START_PAGE: int = 2
    COINGECKO_MIDCAP_SCAN_PAGES: int = 3
    COINGECKO_MIDCAP_SCAN_MIN_RANK: int = 251
    COINGECKO_MIDCAP_SCAN_MAX_RANK: int = 1000
    COINGECKO_MIDCAP_SCAN_MIN_24H_CHANGE: float = 25.0
    COINGECKO_MIDCAP_SCAN_MIN_VOLUME: float = 250_000.0
    COINGECKO_MIDCAP_SCAN_MIN_MCAP: float = 10_000_000.0
    COINGECKO_MIDCAP_SCAN_MAX_MCAP: float = 200_000_000.0  # $200M ceiling
    COINGECKO_MIDCAP_SCAN_MAX_TOKENS_PER_CYCLE: int = 20

    # -------- Deep-volume rotating page (gap-fill Increment 2, 2026-06-02) --------
    # ONE extra volume_desc page per cycle, rotating START..END (4->5->6), funded
    # by disabling the midcap lane above (page-neutral, smoother than a 3-page
    # burst). Targets the $500K-$10M coverage hole: tokens about to pump show
    # rising VOLUME first, so they climb into volume ranks ~750-1500 BEFORE the
    # +20%/24h move -> gives the gainer_acceleration detector + the gainers tracker
    # pre-pump volume_history_cg. Tight filters bound blast radius (every accepted
    # token also reaches scoring/candidates; CG-listed micro-caps score ~0).
    # Thresholds per the Codex xhigh review 2026-06-02.
    COINGECKO_DEEP_VOLUME_ENABLED: bool = True
    COINGECKO_DEEP_VOLUME_START_PAGE: int = 4
    COINGECKO_DEEP_VOLUME_END_PAGE: int = 6
    COINGECKO_DEEP_VOLUME_MIN_MCAP: float = 500_000.0
    # gap-fill target ceiling $10M; configurable up to the $200M hard universe cap.
    COINGECKO_DEEP_VOLUME_MAX_MCAP: float = 10_000_000.0
    COINGECKO_DEEP_VOLUME_MIN_VOLUME: float = 100_000.0
    COINGECKO_DEEP_VOLUME_MIN_VOL_MCAP_RATIO: float = 0.03
    COINGECKO_DEEP_VOLUME_MIN_24H_CHANGE: float = 3.0
    COINGECKO_DEEP_VOLUME_MAX_TOKENS_PER_CYCLE: int = 75

    # -------- Source-call price snapshots (X perf accrual C2, #392) --------
    # Forward-only GeckoTerminal-by-CA snapshot writer
    # (scripts/source_call_price_snapshots_writer.py, cron <=15 min). DEFAULT
    # OFF (deploy-without-activate): the merged writer is inert until the
    # operator sets SOURCE_CALL_SNAPSHOT_WRITER_ENABLED=true in .env — no
    # deploy/activation during the DEX soak without separate approval. These
    # knobs are consumed by the .sh wrapper (via .env); declared here so .env
    # stays valid under extra="forbid".
    # HORIZON = widest forward-window end (the 24h window closes at call+28h);
    # a call older than this can gain no new in-window snapshot.
    SOURCE_CALL_SNAPSHOT_WRITER_ENABLED: bool = False
    SOURCE_CALL_SNAPSHOT_HORIZON_HOURS: int = Field(default=28, ge=1, le=168)

    # Held-position price-refresh lane (§12c-narrow remediation).
    # See tasks/plan_held_position_price_freshness.md and
    # tasks/findings_open_position_price_freshness_2026_05_12.md.
    # When enabled, every Nth pipeline cycle queries open paper_trades and
    # forces a price_cache refresh for held tokens regardless of whether they
    # appear in any other ingestion lane.
    #
    # DEFAULT IS FALSE — deploy-safe-by-default. Operator explicitly sets
    # HELD_POSITION_PRICE_REFRESH_ENABLED=True on the VPS .env on 2026-05-14
    # (the planned activation date — clean cohort boundary post BL-NEW-
    # AUTOSUSPEND-FIX soak close). Revert via _ENABLED=False.
    HELD_POSITION_PRICE_REFRESH_ENABLED: bool = False
    HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES: int = 1
    # BL-NEW-HELD-POSITION-REFRESH-RATE-GAP (cycle 13): per-token persistent-
    # stale WARN threshold. ≥ this many hours of cache staleness on an open
    # paper_trade emits one WARN/24h to journalctl (in-memory dedup; resets on
    # pipeline restart). Default 24 aligns with the stale_open_count gauge
    # threshold (single semantic across both surfaces).
    HELD_POSITION_STALE_WARN_HOURS: int = 24

    # BL-NEW-TODAYS-FOCUS-LIQUIDITY-VENUE-FACTS Phase 1a-i (2026-05-29):
    # liquidity enrichment cron + watchdog. Writer ships in Phase 1a-ii.
    # See tasks/design_liquidity_enrichment_b2_2026_05_29.md.
    #
    # DEFAULT IS FALSE — deploy-safe-by-default. Operator explicitly sets
    # LIQUIDITY_ENRICHMENT_ENABLED=True on the VPS .env when ready to
    # activate the cron after Phase 1a-ii lands. Watchdog respects the
    # flag and suppresses staleness alerts when False (prevents pager
    # fatigue during planned downtime per design's failure-mode table).
    LIQUIDITY_ENRICHMENT_ENABLED: bool = False
    # Per-row TTL: cron skips rows enriched within this window so a
    # backlog drain doesn't re-hit healthy rows. 1800s (30 min) keeps
    # data fresh enough for a 15-min cron cadence without thrashing.
    LIQUIDITY_ENRICHMENT_TTL_SEC: int = Field(default=1800, ge=60, le=86400)
    # Dashboard staleness gate: max(liquidity_enriched_at) older than
    # this renders confidence='stale' regardless of stored value. 3600s
    # (1h) is 4x the cron's per-row TTL — generous slack so transient
    # cron-tick miss doesn't flap the UI.
    LIQUIDITY_ENRICHMENT_STALE_SEC: int = Field(default=3600, ge=60, le=86400)
    # Per-tick row cap. 50 rows × 4 ticks/hour = 200 rows/hour drain rate
    # under the shared 25 req/min CG budget (each row = 1 CG call +
    # 1-3 DexScreener calls). Initial 995-row backlog drains in ~5h.
    LIQUIDITY_BACKFILL_BATCH_MAX: int = Field(default=50, ge=1, le=1000)

    # MiroFish
    MIROFISH_URL: str = "http://localhost:5001"
    # ge=1 — zero would trigger instant timeout on every call; le=600
    # — MiroFish jobs taking >10min are a separate problem class.
    MIROFISH_TIMEOUT_SEC: int = Field(default=180, ge=1, le=600)
    MAX_MIROFISH_JOBS_PER_DAY: int = Field(default=50, ge=0, le=10_000)

    # Alerts
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_CHAT_ID: str
    DISCORD_WEBHOOK_URL: str = ""

    # Holder enrichment (optional)
    HELIUS_API_KEY: str = ""
    MORALIS_API_KEY: str = ""

    # DEX-outcome instrumentation (observe-only; I1/I2/I3). ALL capture is gated
    # by DEX_INSTRUMENTATION_ENABLED — when False the pipeline is byte-identical
    # (no scorer/gate/threshold/alert change). Captured-not-scored. See
    # tasks/spec_dex_outcome_instrumentation_i1_i2_i3_2026_06_28.md.
    DEX_INSTRUMENTATION_ENABLED: bool = False
    # I1 resolver: max /coins/{id} calls per cycle. 5/cycle at 60 cyc/hr =
    # <=5/min, leaving >=25/min of the shared 30 req/min budget for ingestion.
    DEX_RESOLVER_BUDGET_PER_CYCLE: int = Field(default=5, ge=0, le=1000)
    # Negative-result TTL: skip a coin_id whose resolution failed within this
    # window (avoids re-spending budget on persistent 404s; still retries after).
    DEX_RESOLVER_NEGATIVE_TTL_SEC: int = Field(default=3600, ge=0, le=86_400)
    # Raw proxy snapshot retention (txns_h1_buys_snapshots).
    DEX_TXNS_RETENTION_DAYS: int = Field(default=30, ge=1, le=365)
    # Tier-2 data-quality watchdog floors (fractions); alarm when measured below.
    DEX_RESOLUTION_HEALTH_FLOOR: float = Field(default=0.05, ge=0.0, le=1.0)
    DEX_NONZERO_MCAP_FLOOR: float = Field(default=0.90, ge=0.0, le=1.0)
    DEX_NONNULL_TXNS_FLOOR: float = Field(default=0.50, ge=0.0, le=1.0)
    # Health/watchdog alert routing (C3): empty -> falls back to TELEGRAM_CHAT_ID.
    # System-health alerts only, NEVER trading/signal alerts.
    TELEGRAM_HEALTH_CHAT_ID: str = ""

    # Database
    DB_PATH: Path = Path("scout.db")
    # GA-22: connection-level PRAGMA busy_timeout applied at
    # Database.initialize(). ge=0 (0 = fail immediately on lock, valid for
    # tests); le=600_000 (10 min ceiling — anything larger is almost
    # certainly a misconfig that should fail-fast at startup).
    SQLITE_BUSY_TIMEOUT_MS: int = Field(default=90_000, ge=0, le=600_000)

    # Anthropic fallback
    ANTHROPIC_API_KEY: str

    # Narrative Rotation Agent
    # ge=60 — sub-minute polling is hostile to upstream APIs; le=86400 —
    # cadences >24h are likely a misconfig (typo: 21600 vs 216000).
    NARRATIVE_POLL_INTERVAL: int = Field(default=1800, ge=60, le=86_400)
    NARRATIVE_EVAL_INTERVAL: int = Field(default=21_600, ge=60, le=604_800)
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
    CHAIN_CHECK_INTERVAL_SEC: int = Field(default=300, ge=1, le=3600)  # 5 minutes
    CHAIN_MAX_WINDOW_HOURS: float = Field(default=24.0, gt=0, le=720.0)
    CHAIN_COOLDOWN_HOURS: float = Field(default=12.0, ge=0, le=720.0)
    CHAIN_EVENT_RETENTION_DAYS: int = Field(default=14, ge=1, le=365)
    CHAIN_ACTIVE_RETENTION_DAYS: int = Field(default=7, ge=1, le=365)
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
    CHAIN_OUTCOME_PERSISTENT_FAILURE_HOURS: float = Field(
        default=1.0,
        gt=0,
        le=168.0,  # 1 week ceiling — beyond is "stuck rows expected"
        description="ERROR threshold for stuck-row aging",
    )
    CHAIN_TRACKER_UNHEALTHY_FAILURE_RATE: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Failure-rate fraction (0..1); >=1 disables circuit-break",
    )
    CHAIN_TRACKER_UNHEALTHY_MIN_ATTEMPTS: int = 3  # floor — don't ERROR on 1-row cycles

    # -------- Market Briefing Agent --------
    BRIEFING_ENABLED: bool = False  # opt-in, not default-on
    BRIEFING_HOURS_UTC: str = "6,18"  # comma-separated hours (6am + 6pm)
    BRIEFING_MODEL: str = "claude-sonnet-4-6"
    BRIEFING_TELEGRAM_ENABLED: bool = True
    # Cadence at which briefing_loop polls "is now in BRIEFING_HOURS_UTC?".
    # ge=10 — sub-10s polls thrash on a sleep-heavy loop; le=3600 — beyond
    # 1h the trigger window may close before the next poll fires. Default
    # 60s matches the prior hardcoded value at scout/main.py:561.
    BRIEFING_LOOP_POLL_INTERVAL_SEC: int = Field(default=60, ge=10, le=3600)
    COINGLASS_API_KEY: str = ""  # free tier, register at coinglass.com

    # -------- 7-Day Momentum Scanner --------
    MOMENTUM_7D_ENABLED: bool = True
    MOMENTUM_7D_MIN_CHANGE: float = 100.0  # min 7d change % to flag (100% = doubled)
    MOMENTUM_7D_MAX_MCAP: float = 200_000_000  # $200M ceiling (operator trades <=$200M)
    MOMENTUM_7D_MIN_VOLUME: float = (
        100_000  # min $100K 24h volume — weeds out illiquid junk
    )

    # -------- Slow-Burn Watcher (BL-075 Phase B) --------
    SLOW_BURN_ENABLED: bool = True
    SLOW_BURN_MIN_7D_CHANGE: float = 50.0
    SLOW_BURN_MAX_1H_CHANGE: float = 5.0
    SLOW_BURN_MAX_MCAP: float = 200_000_000  # $200M ceiling
    SLOW_BURN_MIN_VOLUME: float = 100_000
    # calibration era: undocumented -- see BL-NEW-CALIBRATION-ERA-DOC
    SLOW_BURN_DEDUP_DAYS: int = 7
    # BL-NEW-SLOW-BURN-DISPATCH-PROMOTION (2026-06-12): promote slow-burn from
    # shadow-only to paper dispatch. Default OFF — flip True to start the
    # forward paper-PnL soak. Promotion gate PASSED (BL-075): slow_burn's
    # 5x-runner rate matched velocity_alerter and uniquely caught VELVET 12.9x
    # + BEAT 11.8x. Revert: SLOW_BURN_DISPATCH_ENABLED=False (no DB cleanup).
    SLOW_BURN_DISPATCH_ENABLED: bool = False
    SLOW_BURN_DISPATCH_MIN_MCAP: float = 500_000.0

    # -------- Volume Spike Detector --------
    VOLUME_SPIKE_ENABLED: bool = True
    VOLUME_SPIKE_RATIO: float = 5.0
    VOLUME_SPIKE_MAX_MCAP: float = 200_000_000  # $200M ceiling

    # -------- Velocity Alerter (CoinGecko 1h early-pump detection) --------
    # Research-only alerts for tokens pumping hard in the last hour.
    # No paper trade dispatch -- Telegram plain-text only.
    VELOCITY_ALERTS_ENABLED: bool = False
    VELOCITY_MIN_1H_PCT: float = 30.0  # minimum 1h % change to flag
    VELOCITY_MIN_MCAP: float = 500_000  # skip dust
    VELOCITY_MAX_MCAP: float = 50_000_000  # skip mega-caps
    VELOCITY_MIN_VOL_MCAP_RATIO: float = 0.2  # vol_24h / mcap -- liquidity sanity
    # calibration era: undocumented -- see BL-NEW-CALIBRATION-ERA-DOC
    VELOCITY_DEDUP_HOURS: int = 4  # re-alert cooldown per coin
    VELOCITY_TOP_N: int = 10  # max alerts per cycle

    # -------- Gainer Acceleration Detector (gap-fill 2026-06-02) --------
    # Catches $500K-$200M tokens accelerating (1h/4h price + volume) over our
    # stored volume_history_cg BEFORE the 24h +20% gainer move completes. Zero
    # extra CG calls (reads existing history). Research-only (writes the
    # gainer_acceleration table + Top-Gainers-Tracker surface; NO alert/paper)
    # until precision is measured -- vol_expansion is noisy because
    # volume_history_cg.volume_24h is a CG 24h snapshot, not interval volume, so
    # price acceleration is the strong leg and volume is a soft filter.
    ACCELERATION_ENABLED: bool = True
    ACCELERATION_MIN_1H_PCT: float = 8.0
    ACCELERATION_MIN_4H_PCT: float = 12.0
    ACCELERATION_MIN_VOL_EXPANSION: float = 2.0
    ACCELERATION_MIN_SAMPLES: int = 3
    ACCELERATION_MIN_MCAP: float = 500_000
    ACCELERATION_MAX_MCAP: float = 200_000_000
    ACCELERATION_DEDUP_HOURS: int = 4
    # >= the 4h reference window's upper bound (5.5h) + slack for a slightly
    # aged latest sample, so the SQL recency slice doesn't clip the window.
    ACCELERATION_LOOKBACK_HOURS: float = 6.0
    ACCELERATION_TOP_N: int = 20

    # -------- Top Gainers Tracker --------
    GAINERS_TRACKER_ENABLED: bool = True
    GAINERS_MIN_CHANGE: float = 20.0
    GAINERS_MAX_MCAP: float = 200_000_000  # $200M ceiling

    # -------- Cross-Surface Conviction Score (BL-NEW-CROSS-SURFACE-CONVICTION-SCORE) --------
    # Read-only ranking over gainers_comparisons: counts independent detectors
    # that confirmed a coin >= EARLY_LEAD_MINUTES before the +20%/24h move.
    # Validated discriminator (≥4 early surfaces → ~21% 3x-rate vs ~1% for ≤1).
    # Observe-first: powers /api/conviction/shortlist only — no alert/paper-trade.
    CONVICTION_SCORE_ENABLED: bool = True
    CONVICTION_EARLY_LEAD_MINUTES: int = 1440  # 24h — the validated "early" window
    CONVICTION_HIGH_TIER_MIN_SURFACES: int = 4  # ~21% 3x precision at this gate
    CONVICTION_WATCH_TIER_MIN_SURFACES: int = 2

    # BL-NEW-CONVICTION-PROSPECTIVE-SCORE (V1, observe-only): forward watchlist of
    # not-yet-pumped sub-$30M coins with sustained (>=24h) cross-surface early
    # confirmation. Snapshots = the prospective-precision event stream. No alerts/
    # trades. See tasks/design_prospective_conviction_watchlist_2026_06_19.md.
    CONVICTION_PROSPECTIVE_ENABLED: bool = True
    CONVICTION_WATCHLIST_MAX_MCAP: float = Field(default=30_000_000, ge=0)
    CONVICTION_WATCHLIST_MCAP_MAX_AGE_MINUTES: int = Field(default=1440, ge=0)
    CONVICTION_PROSPECTIVE_LOOKBACK_DAYS: int = Field(default=14, ge=1, le=120)
    CONVICTION_WATCHLIST_SNAPSHOT_RETENTION_DAYS: int = Field(default=90, ge=1)
    CONVICTION_WATCHLIST_SNAPSHOT_SLO_MINUTES: int = Field(default=180, ge=1)

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
    # calibration era: undocumented -- see BL-NEW-CALIBRATION-ERA-DOC
    SECONDWAVE_DEDUP_DAYS: int = 7
    SECONDWAVE_MIN_VOLUME_POINTS: int = 2

    # -------- Score / Volume history retention (BL-NEW-SCORE-HISTORY-PRUNING
    # + BL-NEW-VOLUME-SNAPSHOTS-PRUNING) --------
    # Hourly prune cutoff applied by main._run_hourly_maintenance. Must be
    # >= SECONDWAVE_COOLDOWN_MAX_DAYS to avoid truncating secondwave's
    # evidence window. Validator below enforces.
    SCORE_HISTORY_RETENTION_DAYS: int = 21
    VOLUME_SNAPSHOTS_RETENTION_DAYS: int = 21

    # -------- Narrative-owned table retention (BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION) --------
    # Hourly prune via main._run_hourly_maintenance. V8 plan-review fold:
    # volume_spikes / trending_snapshots / chain_matches defaults set to >= 30
    # to cover backtest CLI default --days=30 + 15d headroom (where applicable);
    # validator enforces 30d floor below. Per-table reader-window analysis
    # in tasks/plan_narrative_prune_scope_expansion.md.
    VOLUME_SPIKES_RETENTION_DAYS: int = 45
    MOMENTUM_7D_RETENTION_DAYS: int = 30
    TRENDING_SNAPSHOTS_RETENTION_DAYS: int = 30
    LEARN_LOGS_RETENTION_DAYS: int = 90
    CHAIN_MATCHES_RETENTION_DAYS: int = 45
    HOLDER_SNAPSHOTS_RETENTION_DAYS: int = 14

    # BL-NEW-TG-BURST-PROFILE: per-call instrumentation for TG dispatch
    # frequency. Default True for the 4-week measurement window; toggle
    # False via .env to disable if instrumentation overhead surfaces.
    # Counter is in-memory deque (collections.deque + threading.Lock);
    # logs at debug (per-call) + warning (burst/429). See
    # tasks/plan_tg_burst_profile.md for pre-registered decision criteria.
    TG_BURST_PROFILE_ENABLED: bool = True

    # P1 #2 TG pacing: honor Telegram retry_after. Pre-send gate waits if the
    # chat is currently paced; on a 429 within budget we pace + retry once.
    # Every wait/retry sleep is capped at TG_PACING_MAX_WAIT_SECONDS so a large
    # retry_after can't stall the pipeline (over-budget asks fall through, paced).
    TG_PACING_ENABLED: bool = True
    TG_PACING_MAX_WAIT_SECONDS: float = Field(default=10.0, gt=0)

    # BL-NEW-SQLITE-WAL-PROFILE cycle 4: hourly WAL state probe.
    # Default True for 4-week measurement; threshold default 50MB is a
    # starting point — operator runs scripts/wal_summary.sh 168 after
    # Week 1 and sets SQLITE_WAL_BLOAT_BYTES to ~1.5x observed p95 in
    # .env. See tasks/plan_sqlite_wal_profile.md § Week-1 baseline.
    SQLITE_WAL_PROFILE_ENABLED: bool = True
    SQLITE_WAL_BLOAT_BYTES: int = 50_000_000

    # BL-NEW-SQLITE-DURABLE-MAINTENANCE (P0 Part B): active WAL/freelist
    # remediation + stale-reader watchdog in _run_hourly_maintenance.
    # Incident 2026-06-18: auto_vacuum=NONE (freelist 54.7%) + 2 orphaned
    # 65-day reader processes pinning the WAL. auto_vacuum was flipped to
    # INCREMENTAL during the one-time VACUUM, so incremental_vacuum reclaims
    # freelist online. See tasks/plan_sqlite_durable_maintenance_2026_06_18.md.
    SQLITE_WAL_CHECKPOINT_ENABLED: bool = True
    SQLITE_WAL_CHECKPOINT_THRESHOLD_BYTES: int = Field(default=100_000_000, ge=0)
    # Alert the operator after N CONSECUTIVE busy checkpoints — covers the
    # WAL-pin case where the holder is younger than the stale-reader age gate
    # OR is an expected service (e.g. a long dashboard read), which the
    # stale-reader watchdog alone would not surface (gate-3 failure-mode review).
    SQLITE_WAL_CHECKPOINT_BUSY_ALERT_THRESHOLD: int = Field(default=3, ge=1)
    SQLITE_INCREMENTAL_VACUUM_ENABLED: bool = True
    SQLITE_INCREMENTAL_VACUUM_FREELIST_THRESHOLD: int = Field(default=50_000, ge=0)
    SQLITE_INCREMENTAL_VACUUM_MAX_PAGES: int = Field(default=200_000, ge=0)
    SQLITE_STALE_READER_WATCHDOG_ENABLED: bool = True
    SQLITE_STALE_READER_MAX_AGE_HOURS: float = Field(default=6.0, gt=0)
    SQLITE_STALE_READER_ALERT_ENABLED: bool = True
    SQLITE_EXPECTED_SERVICE_UNITS: list[str] = Field(
        default_factory=lambda: [
            "gecko-pipeline.service",
            "gecko-dashboard.service",
        ]
    )

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
    LUNARCRUSH_CREDIT_SOFT_PCT: float = Field(
        default=0.80, ge=0.0, le=1.0, description="downshift threshold (0..1)"
    )
    LUNARCRUSH_CREDIT_HARD_PCT: float = Field(
        default=0.95, ge=0.0, le=1.0, description="stop threshold (0..1)"
    )
    LUNARCRUSH_SOCIAL_SPIKE_RATIO: float = 2.0
    LUNARCRUSH_GALAXY_JUMP: float = 10.0
    LUNARCRUSH_INTERACTIONS_ACCEL: float = 3.0
    # calibration era: undocumented -- see BL-NEW-CALIBRATION-ERA-DOC
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
    # A future CryptoPanic scoring PR must add its weight to SCORER_MAX_RAW and
    # recalibrate tests before enabling this signal.
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

    # BL-NEW-LIVE-ELIGIBLE: writes would_be_live=1 on paper trades that
    # match the tier rules from tasks/findings_live_eligibility_*.md AND
    # fit under the live-eligible concurrent-slot cap. Pure observability;
    # paper trade behavior is unchanged.
    # Tier 1 (mandatory): signal_type='chain_completed' OR conviction stack≥3.
    # Tier 2 (high-quality): signal_type='volume_spike' OR (signal_type=
    #   'gainers_early' AND mcap≥PAPER_TIER2_GAINERS_MIN_MCAP_USD AND
    #   24h≥PAPER_TIER2_GAINERS_MIN_24H_PCT).
    PAPER_LIVE_ELIGIBLE_SLOTS: int = 20
    PAPER_TIER2_GAINERS_MIN_MCAP_USD: float = 10_000_000.0
    PAPER_TIER2_GAINERS_MIN_24H_PCT: float = 25.0

    # BL-NEW-TG-ALERT-ALLOWLIST: per-signal Telegram alert dispatch on
    # paper-trade open. Eligibility tracked per-signal in
    # signal_params.tg_alert_eligible (default 0). Cooldown is per-token
    # ACROSS signal types (R2-I1 design fold) — a single token firing two
    # different signals within the window only alerts once.
    TG_ALERT_PER_TOKEN_COOLDOWN_HOURS: int = 6

    # BL-NEW-TG-ALERT-NOISE-DEDUP: strict 24h per-token dedup window for
    # paper-trade-open TG alerts. Once a token's alert is SENT, further
    # alerts for the same token_id are suppressed for this many hours. This
    # SUPERSEDES TG_ALERT_PER_TOKEN_COOLDOWN_HOURS as the single live
    # dispatch gate (see tasks/design_tg_alert_24h_dedup_2026_05_30.md);
    # the legacy field is kept for back-compat but no longer drives the
    # decision. 0 disables dedup entirely (clean revert), with no
    # off-by-one. See global CLAUDE.md §12b for the co-shipped audit logs.
    TG_ALERT_DEDUP_WINDOW_HOURS: int = Field(default=24, ge=0)

    # BL-NEW-TRADE-SURFACE-TG-ALERTS: optional scarce Telegram alert lane
    # sourced from the Today Focus and Now Tradable dashboard surfaces. Kept
    # behind an env flag because it creates new operator-facing messages; the
    # selector itself is capped + per-token deduped when enabled.
    TRADE_SURFACE_TG_ALERTS_ENABLED: bool = False
    TRADE_SURFACE_TG_ALERTS_INTERVAL_SECONDS: int = Field(default=900, ge=60, le=86_400)
    TRADE_SURFACE_TG_ALERTS_WINDOW_HOURS: int = Field(default=36, ge=6, le=72)
    TRADE_SURFACE_TG_ALERTS_MAX_PER_RUN: int = Field(default=5, ge=1, le=5)
    TRADE_SURFACE_TG_ALERTS_MAX_PER_DAY: int = Field(default=5, ge=1, le=5)
    TRADE_SURFACE_TG_ALERTS_DEDUP_HOURS: int = Field(default=24, ge=0, le=720)
    TRADE_SURFACE_TG_ALERTS_SEND_SPACING_SECONDS: float = Field(
        default=1.25, ge=0.0, le=10.0
    )

    # BL-NEW-NARRATIVE-SCANNER: Hermes-driven narrative pump scanner (V1).
    # Hermes (main-vps) emits structured events to gecko-alpha via HMAC-authed
    # HTTPS. Feature gated off when secret is empty (endpoints respond 503).
    # See tasks/design_crypto_narrative_scanner.md for full design.
    #
    # PR #110 V2 reviewer S1 fold: secret must be empty (gated-off sentinel)
    # OR >= 32 chars (32-byte hex from `secrets.token_hex(32)` is 64 chars).
    # The field_validator below enforces this at Settings() construction.
    NARRATIVE_SCANNER_HMAC_SECRET: str = ""
    # Replay-protection window: reject requests where |now - timestamp| exceeds this.
    NARRATIVE_SCANNER_REPLAY_WINDOW_SEC: int = 300
    # Max body bytes for POST /api/narrative-alert. Cap BEFORE HMAC compute
    # so attackers without secret can't flood with multi-MB bodies.
    # (Vector B D5 fold.) 16KB comfortably fits a max-length tweet + metadata.
    NARRATIVE_SCANNER_MAX_BODY_BYTES: int = 16 * 1024
    # BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE Reviewer 1 P1 fold: separate secret
    # for the internal operator-alert endpoint so the dispatcher can still
    # raise a Telegram alert when NARRATIVE_SCANNER_HMAC_SECRET is missing
    # (the exact failure mode the operator-alert endpoint exists to surface).
    # Same shape rules as NARRATIVE_SCANNER_HMAC_SECRET — empty (feature off)
    # or >= 32 chars. The internal-alert endpoint gates 503 when this is
    # empty. Body-size cap + replay-window settings are shared with the
    # narrative endpoint (they're generic HMAC mechanics, not narrative-
    # specific).
    OPERATOR_ALERT_HMAC_SECRET: str = ""

    # BL-NEW-SOURCE-CALL-CRON-TICK-WATCHDOG: writer heartbeat file path.
    # When set, the source-calls writer cron touches this file on every
    # successful run; the lag watchdog reads its mtime to detect cron
    # outages independently of upstream traffic. Read directly by the
    # bash wrappers (scripts/source-calls-live-writer.sh + lag-watchdog.sh)
    # via shell env — declared here so Pydantic's `extra="forbid"`
    # doesn't reject the .env line. Empty default keeps the feature off
    # for back-compat; activation = operator sets in .env.
    WRITER_HEARTBEAT_FILE: str = ""
    # Optional override for the lag-watchdog's writer-staleness threshold
    # (minutes). Default 20min = 4× the 5min writer cron cadence. Same
    # bash-env-only consumption pattern as WRITER_HEARTBEAT_FILE.
    WRITER_THRESHOLD_MINUTES: int = 20

    # NOTE: rate-limit middleware (slowapi) deferred to Day 2 — see
    # tasks/design_crypto_narrative_scanner.md §8. PR #110 V1-I1 fold:
    # the unused NARRATIVE_SCANNER_RATE_LIMIT_PER_MIN setting was removed
    # to avoid the footgun of operators assuming protection exists.

    # BL-NEW-M1.5C: Minara DEX-eligibility alert extension (Phase 0 Option A).
    # When a TG paper-trade-open alert is about to fire for a Solana-listed
    # token, append a `minara swap` shell command to the alert body for
    # operator copy-paste. gecko-alpha does NOT execute — pure decision-
    # support. Solana-only in M1.5c; EVM chains are M1.5d/M2.
    MINARA_ALERT_ENABLED: bool = True
    MINARA_ALERT_FROM_TOKEN: str = "USDC"
    # Default trade-size suggestion in the Run: command. R2-C1 fold:
    # default $10 mirrors M1.5a V3-M3 first-24h discipline. Paper-trade
    # caller size is $300 prod / $1000 default — too large for memecoin
    # slippage. Operator overrides via .env if they want larger sizes.
    MINARA_ALERT_AMOUNT_USD: float = 10.0

    PAPER_MIN_MCAP: float = 5_000_000  # min $5M mcap to paper trade (filters junk)
    # Upper mcap cap for paper trades. Large caps (BTC, ETH, SOL, AAVE...) rarely
    # pump fast enough to hit PAPER_TP_PCT within PAPER_MAX_DURATION_HOURS, so
    # they consume slots without producing wins. Signals/alerts still fire —
    # this knob only gates the paper-trade entry path.
    PAPER_MAX_MCAP: float = 200_000_000  # $200M ceiling (operator trades <=$200M)
    PAPER_MAX_MCAP_RANK: int = 1500  # skip trending coins below rank 1500 (illiquid)
    # Hard cap on concurrent open positions. Prevents restart-bursts and
    # survives env changes to PAPER_MAX_EXPOSURE_USD / PAPER_TRADE_AMOUNT_USD.
    PAPER_MAX_OPEN_TRADES: int = 10
    # Per-signal-type caps: prevent any one signal type from dominating slot allocation.
    # If set to 0, the per-signal caps are disabled and only PAPER_MAX_OPEN_TRADES applies.
    PAPER_MAX_OPEN_TRENDING_CATCH: int = 20
    PAPER_MAX_OPEN_FIRST_SIGNAL: int = 20
    PAPER_MAX_OPEN_GAINERS_EARLY: int = 12
    PAPER_MAX_OPEN_NARRATIVE_PREDICTION: int = 7
    PAPER_MAX_OPEN_LOSERS_CONTRARIAN: int = 3
    # Cooldown after service start: refuse to open new paper trades during
    # this window so a restart doesn't replay every currently-qualifying
    # candidate as a fresh signal. A live trader doesn't bulk-enter on reboot.
    # calibration era: undocumented -- see BL-NEW-CALIBRATION-ERA-DOC
    PAPER_STARTUP_WARMUP_SECONDS: int = 180
    # GA-01 fail-closed dispatch gate: refuse to open a paper trade whose
    # token_id has no refreshable price source — i.e., NOT CG-id-shaped
    # (scout.token_ids.is_cg_coin_id) AND no price_cache row. Without this,
    # DexScreener-fallback ids (`dex:{chain}:{address}` from the TG-social
    # resolver) open at a caller-supplied entry_price, are never re-priced
    # by ANY price_cache writer, and can only exit via expiry at
    # entry_price → fabricated $0-PnL rows that dilute auto-suspend /
    # calibration / combo stats. Prod evidence: 12/12 historical `dex:`
    # closes were $0 at exactly max_duration (2026-07). Flip to False only
    # if a dex:-namespace price writer ships.
    PAPER_REQUIRE_PRICEABLE_TOKEN_ID: bool = True
    # Phase 6 slice 3 (operator-approved policy A): stale-onset exit. When an
    # open trade's price_cache row goes stale for more than this many hours
    # (and the trade has NOT reached max_duration), the evaluator exits NOW at
    # the last-good cached price (provenance 'stale_snapshot', status
    # 'closed_stale_onset') instead of holding a position it can no longer
    # mark. Rationale: a token leaving the tracked universe usually means
    # liquidity death — waiting for expiry just fabricates a later close at
    # the same stale mark. ge=1: the evaluator's own freshness window is 1h;
    # a sub-hour onset threshold would fight it.
    STALE_ONSET_EXIT_HOURS: float = Field(default=6.0, ge=1)
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
    TG_SOCIAL_SESSION_PATH: Path = _REPO_ROOT / "tg_social.session"
    TG_SOCIAL_CHANNELS_FILE: Path = _REPO_ROOT / "channels.yml"
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

    # -------- BL-NEW-LIVE-HYBRID M1 (design v2.1, 2026-05-08) --------
    # Layer 1 of 4-layer kill stack. Master kill — when False, all live
    # execution short-circuits at engine entry regardless of LIVE_MODE /
    # per-signal opt-in / kill_switch state. Operator via .env edit + restart.
    # Distinct from LIVE_MODE (paper/shadow/live tri-state, Layer 2).
    LIVE_TRADING_ENABLED: bool = False

    # Per-token concurrency cap. Routing layer's live-position-aggregator
    # guard rejects intents when live_trades.count(canonical_symbol, status='open')
    # >= this value. Default 1 covers BILL dual-signal pattern.
    # Distinct from existing LIVE_MAX_OPEN_POSITIONS (total-across-venues cap, default 5).
    LIVE_MAX_OPEN_POSITIONS_PER_TOKEN: int = 1

    # OverrideStore semantics: False = PREPEND chain's venues to candidate list
    # (graceful fallback if override chain has no healthy venue); True = REPLACE
    # (only override chain's venues; abort if none healthy). Default False.
    LIVE_OVERRIDE_REPLACE_ONLY: bool = False

    # -------- BL-NEW-LIVE-HYBRID M1.5a (2026-05-09) --------
    # Gates the signed-endpoint runtime codepath for the 3 ABC methods on
    # BinanceSpotAdapter (place_order_request, await_fill_confirmation,
    # fetch_account_balance). When False (default), runtime bodies fall back
    # to NotImplementedError — emergency-revert posture without git revert.
    # Operator flips True after balance smoke check passes on testnet.
    LIVE_USE_REAL_SIGNED_REQUESTS: bool = False

    # M1.5b — gates the multi-venue routing layer dispatch in LiveEngine.
    # When False (default), engine falls back to M1.5a's single-venue path
    # and _dispatch_live is not invoked. Operator opts in by flipping True
    # after observing 1-3 successful place_order_request + await_fill cycles
    # in live mode. Engine __init__ raises RuntimeError if this is True
    # AND LIVE_USE_REAL_SIGNED_REQUESTS=False (silent no-op misconfig CRASH
    # per design §2.2).
    LIVE_USE_ROUTING_LAYER: bool = False

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
    # calibration era: undocumented -- see BL-NEW-CALIBRATION-ERA-DOC
    FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN: int = 60
    FEEDBACK_WEEKLY_DIGEST_WEEKDAY: int = 6
    FEEDBACK_WEEKLY_DIGEST_HOUR: int = 9
    FEEDBACK_COMBO_REFRESH_HOUR: int = 3

    # BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST (cycle 5). Weekly cohort-comparison
    # digest paralleling weekly_digest.py — would_be_live=1 cohort vs full
    # cohort per signal_type, with sign-flip detection + final-window
    # decision-recommendation block. Decision criteria pre-registered in
    # tasks/plan_live_eligible_weekly_digest.md. Verdict thresholds mirror
    # dashboard/frontend/components/TradingTab.jsx — operator retunes both
    # surfaces in lockstep via .env override + restart.
    COHORT_DIGEST_ENABLED: bool = True
    COHORT_DIGEST_N_GATE: int = 10
    COHORT_DIGEST_DAY_OF_WEEK: int = 0  # Monday
    COHORT_DIGEST_HOUR: int = 9
    COHORT_DIGEST_FINAL_DATE: date = date(2026, 6, 8)
    COHORT_DIGEST_STRONG_WR_GAP_PP: float = 15.0  # mirrors dashboard
    COHORT_DIGEST_STRONG_PNL_FLOOR_USD: float = 200.0  # mirrors dashboard
    COHORT_DIGEST_MODERATE_WR_GAP_PP: float = 5.0  # mirrors dashboard
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
    # Revival cool-off (BL-NEW-REVIVAL-COOLOFF). Minimum days between
    # consecutive operator-issued revivals of the same signal via
    # Database.revive_signal_with_baseline. Set to 0 to disable. Bypass
    # per-call with force=True (logs revive_signal_force_bypass WARNING
    # and tags the audit row).
    SIGNAL_REVIVAL_MIN_SOAK_DAYS: int = 7
    # BL-NEW-LOSERS-CONTRARIAN-REVIVAL-CRITERIA-TIGHTENING: thresholds for
    # the read-only revival_criteria evaluator. Defaults derived from
    # healthy-signal baselines in tasks/baselines_revival_criteria_2026_05_17.md;
    # operator may override via .env. No production-runtime side-effects.
    REVIVAL_CRITERIA_MIN_TRADES: int = 100
    REVIVAL_CRITERIA_MIN_WINDOW_DAYS: int = 7
    REVIVAL_CRITERIA_MIN_WINDOW_TRADES: int = 50
    REVIVAL_CRITERIA_NO_BREAKOUT_PEAK_PCT: float = 5.0
    REVIVAL_CRITERIA_MAX_NO_BREAKOUT_AND_LOSS: float = 0.40  # healthy_max + margin
    REVIVAL_CRITERIA_EXIT_MACHINERY_MIN: float = 0.70  # healthy_min - margin
    REVIVAL_CRITERIA_WIN_WILSON_LB_MIN: float = 0.55  # not coin-flip per design D#3
    REVIVAL_CRITERIA_BOOTSTRAP_RESAMPLES: int = 10_000
    REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS: int = 30  # keep_on_provisional_until_<iso>
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
    # in scorer.py), which after the social-denominator recalibration ships as
    # 193 (equal to _PERP_ENABLED_MAX_RAW=193, so the guard opens by default).
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

    @field_validator("MIROFISH_URL")
    @classmethod
    def _validate_mirofish_url(cls, v: str) -> str:
        """Must be a `http(s)://` URL — empty value is NOT allowed (the
        client would silently fall through to fallback on every call).
        Catches misconfigs like `localhost:5001` (missing scheme) at
        Settings() construction time.
        """
        if not v.startswith("http://") and not v.startswith("https://"):
            raise ValueError(
                f"MIROFISH_URL must start with http:// or https://; got={v!r}"
            )
        return v

    @field_validator("DISCORD_WEBHOOK_URL")
    @classmethod
    def _validate_discord_webhook_url(cls, v: str) -> str:
        """Empty string is allowed (Discord-disabled — Telegram-only).
        Non-empty values must be `https://` Discord webhook URLs so we
        fail-fast on typos rather than silently dropping alerts.
        """
        if v == "":
            return v
        if not v.startswith("https://"):
            raise ValueError(
                "DISCORD_WEBHOOK_URL must be empty or an https:// URL; " f"got={v!r}"
            )
        return v

    @field_validator("NARRATIVE_SCANNER_HMAC_SECRET")
    @classmethod
    def _validate_narrative_scanner_hmac_secret(cls, v: str) -> str:
        """PR #110 V2 reviewer S1 fold: HMAC secret must be empty (gated-off
        sentinel) OR >= 32 chars. Rejects accidentally-too-short secrets that
        would be brute-forceable. 32 bytes hex = 64 chars (operator should
        generate via `secrets.token_hex(32)`).
        """
        if v and len(v) < 32:
            raise ValueError(
                "NARRATIVE_SCANNER_HMAC_SECRET must be empty (feature off) "
                f"or >= 32 chars (got len={len(v)}). Generate via "
                '`python3 -c "import secrets; print(secrets.token_hex(32))"` '
                "for a 64-char hex secret."
            )
        return v

    @field_validator("OPERATOR_ALERT_HMAC_SECRET")
    @classmethod
    def _validate_operator_alert_hmac_secret(cls, v: str) -> str:
        """Mirror NARRATIVE_SCANNER_HMAC_SECRET's empty-or->=32-chars rule
        (Reviewer 1 P1 fold)."""
        if v and len(v) < 32:
            raise ValueError(
                "OPERATOR_ALERT_HMAC_SECRET must be empty (feature off) "
                f"or >= 32 chars (got len={len(v)}). Generate via "
                '`python3 -c "import secrets; print(secrets.token_hex(32))"` '
                "for a 64-char hex secret."
            )
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

    @field_validator("SIGNAL_REVIVAL_MIN_SOAK_DAYS")
    @classmethod
    def _validate_revival_min_soak_days(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"SIGNAL_REVIVAL_MIN_SOAK_DAYS must be >= 0; got={v}")
        return v

    @field_validator(
        "REVIVAL_CRITERIA_MIN_TRADES",
        "REVIVAL_CRITERIA_MIN_WINDOW_TRADES",
        "REVIVAL_CRITERIA_MIN_WINDOW_DAYS",
        "REVIVAL_CRITERIA_BOOTSTRAP_RESAMPLES",
        "REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS",
    )
    @classmethod
    def _validate_revival_positive_int(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                f"revival-criteria count/days thresholds must be >= 1; got={v}"
            )
        return v

    @field_validator("HELD_POSITION_STALE_WARN_HOURS")
    @classmethod
    def _validate_held_position_stale_warn_hours(cls, v: int) -> int:
        # PR-#158 R2 IMPORTANT 2 fold: also reject absurdly large values that
        # would silently suppress all WARNs on a system the operator believes
        # is monitored — Class-3-adjacent silent-failure. 168h (1 week) is the
        # operational ceiling beyond which a stale held position is no longer
        # actionable (trade has materially aged past expected horizon).
        if v < 1 or v > 168:
            raise ValueError(
                f"HELD_POSITION_STALE_WARN_HOURS must be in [1, 168]; got={v}"
            )
        return v

    @field_validator(
        "REVIVAL_CRITERIA_MAX_NO_BREAKOUT_AND_LOSS",
        "REVIVAL_CRITERIA_EXIT_MACHINERY_MIN",
        "REVIVAL_CRITERIA_WIN_WILSON_LB_MIN",
    )
    @classmethod
    def _validate_revival_ratio(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"revival-criteria ratio must be in [0,1]; got={v}")
        return v

    @field_validator("REVIVAL_CRITERIA_NO_BREAKOUT_PEAK_PCT")
    @classmethod
    def _validate_revival_peak_pct(cls, v: float) -> float:
        if v < 0:
            raise ValueError(
                f"REVIVAL_CRITERIA_NO_BREAKOUT_PEAK_PCT must be >= 0; got={v}"
            )
        return v

    @field_validator("LIVE_MAX_OPEN_POSITIONS_PER_TOKEN")
    @classmethod
    def _validate_live_max_open_positions_per_token(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"LIVE_MAX_OPEN_POSITIONS_PER_TOKEN must be >= 1; got={v}")
        return v

    @model_validator(mode="after")
    def _validate_backtest_cli_retention_floor(self) -> "Settings":
        """V8 plan-review fold: backtest CLI tools default --days=30 against
        trending_snapshots / chain_matches / volume_spikes. Retention below 30
        silently truncates the backtest cohort at the CLI default."""
        backtest_floor = 30
        for field_name in (
            "TRENDING_SNAPSHOTS_RETENTION_DAYS",
            "CHAIN_MATCHES_RETENTION_DAYS",
            "VOLUME_SPIKES_RETENTION_DAYS",
        ):
            value = getattr(self, field_name)
            if value < backtest_floor:
                raise ValueError(
                    f"{field_name}={value} must be >= {backtest_floor} to cover "
                    f"backtest CLI default --days=30. Lower retention silently "
                    f"truncates backtest cohorts."
                )
        return self

    @model_validator(mode="after")
    def _validate_retention_covers_secondwave_window(self) -> "Settings":
        """V2#3 fold: prevent silent mis-config where prune retention <
        secondwave's evidence-window upper bound. The secondwave detector
        JOINs score_history / volume_snapshots for alerts in
        [SECONDWAVE_COOLDOWN_MIN_DAYS, SECONDWAVE_COOLDOWN_MAX_DAYS]; if the
        hourly prune at retention=R deletes rows older than R days and
        R < MAX_DAYS, the older end of the evidence window is silently
        truncated. Fail-fast at config load.
        """
        for field_name in (
            "SCORE_HISTORY_RETENTION_DAYS",
            "VOLUME_SNAPSHOTS_RETENTION_DAYS",
        ):
            value = getattr(self, field_name)
            if value < self.SECONDWAVE_COOLDOWN_MAX_DAYS:
                raise ValueError(
                    f"{field_name}={value} must be >= "
                    f"SECONDWAVE_COOLDOWN_MAX_DAYS={self.SECONDWAVE_COOLDOWN_MAX_DAYS}. "
                    f"Lower retention silently truncates secondwave's evidence window."
                )
        return self

    @model_validator(mode="after")
    def _validate_live_caps_relation(self) -> "Settings":
        """V1 reviewer I4: prevent cap-relation footgun. Operator could
        configure LIVE_TRADE_AMOUNT_USD > LIVE_MAX_EXPOSURE_USD which
        would silently block all live trades (Gate 7's exposure cap is
        smaller than Gate 8's per-trade cap → no single trade fits).
        Symptom is "no trades", not data corruption — bounded blast
        radius, but a footgun. Fail-fast at config load."""
        if self.LIVE_MAX_EXPOSURE_USD < self.LIVE_TRADE_AMOUNT_USD:
            raise ValueError(
                "LIVE_MAX_EXPOSURE_USD must be >= LIVE_TRADE_AMOUNT_USD; "
                f"got exposure={self.LIVE_MAX_EXPOSURE_USD}, "
                f"trade={self.LIVE_TRADE_AMOUNT_USD}. A single trade cannot "
                "exceed the aggregate cap or no trades will ever pass Gate 8."
            )
        return self

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

    @field_validator("INGEST_STARVATION_THRESHOLD_CYCLES")
    @classmethod
    def _validate_ingest_starvation_threshold(cls, v: int) -> int:
        if v < 1:
            raise ValueError("INGEST_STARVATION_THRESHOLD_CYCLES must be >= 1")
        return v

    @field_validator(
        "COINGECKO_MIN_REQUEST_INTERVAL_SEC",
        "COINGECKO_REQUEST_JITTER_SEC",
        "COINGECKO_429_COOLDOWN_SEC",
    )
    @classmethod
    def _validate_coingecko_burst_profile(cls, v: float) -> float:
        if v < 0:
            raise ValueError("CoinGecko burst-profile settings must be >= 0")
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


def load_settings(**kwargs) -> "Settings":
    """Construct Settings() but emit structured logger.error before re-raise on
    ValidationError, so the systemd Restart=always crash-loop has a
    journalctl-visible cause line.

    V4#1 review fold (tasks/design_score_volume_pruning_harden.md §D2):
    operators running with bad .env see a clear ``settings_validation_failed``
    event in journalctl rather than a bare Pydantic stack trace inside an
    infinite 10s respawn loop.

    BL-NEW-SETTINGS-VALIDATION-ALERT (cycle 14): also fires a best-effort
    curl-direct Telegram alert via ``scout.config_alert`` so operators get
    an active push instead of having to grep journalctl. The alert is
    fully wrapped in try/except — NEVER blocks the re-raise.

    ``**kwargs`` are forwarded to ``Settings(...)`` so tests can inject
    deliberate validator violations without env-mutation side effects.
    """
    import structlog  # local import — config.py stays structlog-free at module load
    from pydantic import ValidationError as _ValidationError

    try:
        return Settings(**kwargs)
    except _ValidationError as exc:
        structlog.get_logger().error("settings_validation_failed", error=str(exc))
        # BL-NEW-SETTINGS-VALIDATION-ALERT (cycle 14): best-effort TG alert.
        # Helper catches its own exceptions; outer try is defense-in-depth
        # against the import itself failing (corrupted bytecode, etc.).
        # PR-#160 R2 MINOR-2 fold: log the helper return value so the
        # silent-skip path (e.g. "skipped:no_creds") is visible in journalctl
        # — otherwise operator can't distinguish "alert delivered" from
        # "alert path never engaged" from a missing TG message alone.
        try:
            from scout.config_alert import _send_validation_alert_best_effort

            _alert_outcome = _send_validation_alert_best_effort(str(exc))
            structlog.get_logger().error(
                "settings_validation_alert_dispatched", outcome=_alert_outcome
            )
        except Exception:
            # Settings validation already failed; we don't want a
            # broken validation-alert path to mask the original error
            # via `raise` below. But silent `pass` leaves the operator
            # with NO trace of the alert-path failure either. Log
            # structurally so a double-failure is observable in
            # journalctl. PR Round 4 silent-swallow sweep.
            structlog.get_logger().exception("settings_validation_alert_dispatch_error")
        raise
