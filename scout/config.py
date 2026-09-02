"""Application configuration via Pydantic BaseSettings."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repo root, derived once at import time. Anchors TG_SOCIAL_SESSION_PATH /
# CHANNELS_FILE defaults so they don't depend on CWD ("./tg_social.session"
# resolves differently for systemd starts vs ad-hoc CLI invocations). The
# environment-variable overrides for these fields still take precedence —
# this only fixes the DEFAULT.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# The ledger's longest forward-return horizon, in days. MIRRORS the
# schema-bound constants in scout/outcome_ledger.py — the ``r7d`` entry of
# ``_HORIZONS`` and ``_FINALIZE_AFTER``, which are deliberately NOT Settings
# fields (each maps to a fixed ledger column, so changing one needs a schema
# migration).
#
# It is duplicated here rather than imported because scout.outcome_ledger
# imports scout.db, which imports this module — importing back would be
# circular. The duplication is pinned by a test that asserts the two agree, so
# a future change to _FINALIZE_AFTER cannot silently desynchronise the
# retention floor derived from it below.
LEDGER_R7D_HORIZON_DAYS = 7


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
    # INF §12a: freshness SLOs for the CoinGecko-ingestion outage watchdog
    # (scripts/cg_ingestion_watchdog.py). The in-process ingest watchdog above
    # is structurally blind to a persistent CG backoff-stop — the breaker trips
    # on the first CG lane (main.py:727) and short-circuits the scanner lanes
    # BEFORE they record a zero raw_count sample, so consecutive_misses never
    # accumulates (2026-07-14 outage: 1,559 backoff events, 0 alerts). These
    # SLOs drive a standalone cron watchdog that reads OUTPUT rows
    # (trending_snapshots + gainers/losers snapshots) instead. Values flow to the
    # cron script via .env (sourced by cg-ingestion-watchdog.sh); the script
    # argparse defaults mirror these. Bounds: ge=1 (a sub-hour SLO would page on
    # normal cadence jitter), le=168 (1-week ceiling — a longer SLO is a misconfig
    # that would silence the very outage class this watchdog exists to catch).
    TRENDING_SNAPSHOT_STALENESS_ALERT_HOURS: int = Field(default=3, ge=1, le=168)
    CG_OUTAGE_ALERT_HOURS: int = Field(default=2, ge=1, le=168)

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
    # Conviction gate RETIRED 2026-07-10 (backlog SIG-01 / NAR-01 / ALR-05).
    # Root cause: the 2026-06-02 social-denominator renormalization dropped the
    # max realized quant score to ~54, below MIN_SCORE=65 -> 0/1,995 candidates
    # scored in 6 weeks, MiroFish unreached (0 jobs since 06-01), and the legacy
    # conviction alert path fired 10x headlining "Conviction Score: N/A".
    # When False (default) run_cycle skips gate.evaluate + MiroFish enqueue +
    # send_alert entirely and emits one `conviction_gate_retired` log per cycle.
    # Reversal path: recalibrate MIN_SCORE to the realized score distribution
    # (see SIG-02 scorer-divisor cleanup), then flip this flag back to True.
    CONVICTION_GATE_ENABLED: bool = False

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
    # NoDecode: `list[str]` is a pydantic-settings "complex" field, so
    # EnvSettingsSource would json.loads() the raw env value BEFORE
    # parse_chains runs — turning the documented comma form
    # (`CHAINS=solana,base,ethereum`, .env.example) into a SettingsError at
    # construction (boot crash-loop). NoDecode suppresses that eager decode so
    # the raw string reaches parse_chains. (INF-01; mirrors #430 ALERT_UNIVERSE.)
    CHAINS: Annotated[list[str], NoDecode] = ["solana", "base", "ethereum"]

    # CoinGecko
    # Fraction-domain (0..1). 0 fires for any positive 1h move; >1 never fires.
    MOMENTUM_RATIO_THRESHOLD: float = Field(default=0.6, ge=0.0, le=1.0)
    # Minimum absolute 24h price change (%) required for momentum_ratio to fire.
    # Prevents stablecoin peg-wobble (0.05%/0.08% -> ratio 0.625 > 0.6) from triggering.
    MOMENTUM_MIN_24H_CHANGE_PCT: float = 3.0
    MIN_VOL_ACCEL_RATIO: float = 5.0
    COINGECKO_API_KEY: str = ""
    # CoinGecko key tier. Paid plans (Basic/Analyst/Lite = "Pro API") use a
    # DIFFERENT host (pro-api.coingecko.com) and auth name (x-cg-pro-api-key);
    # mixing tiers yields CG errors 10010/10011. Both key formats start with
    # "CG-" so the tier cannot be inferred from the key — set explicitly:
    # "demo" (free) or "pro" (any paid plan). See scout/cg_api.py.
    COINGECKO_API_TIER: str = "demo"
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
    # 2026-08-21 monthly-budget repair: 3 -> 2. See
    # tasks/plan_cg_monthly_budget_governor.md. The comment ABOVE reasons
    # entirely about "the 25/min limiter" — a RATE constraint. The account also
    # has a MONTHLY CREDIT ceiling (Basic = 100k/mo) which that model ignored
    # entirely, and which is what actually ran out on 2026-08-21 at 100.0%.
    # Restore to 3 only after a full measured billing month AND evidence of
    # discovery-quality loss from 2 pages — not by spending headroom by default.
    COINGECKO_VOLUME_SCAN_PAGES: int = 2

    # CG DISCOVERY cadence, decoupled from the 60s main cycle. The free leading
    # sources (DexScreener / GeckoTerminal) stay at 60s; only the paid CG
    # discovery lanes are throttled, because CG is the LAGGING source.
    #
    # held-position pricing is deliberately NOT gated by this — it keeps its own
    # HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES cadence and its
    # no_open_trades no-op, because it is the operationally critical surface
    # (the live trailing-stop evaluator reads the price_cache rows it writes).
    # 8, NOT the 5 of the original C profile. That figure was derived from the
    # main discovery bundle ALONE (7 calls x 288 rounds/day = ~62k/month, said
    # to fit the 65k envelope). It omitted every OTHER discovery-class CoinGecko
    # consumer, all of which are enabled in prod today:
    #
    #   narrative observer /coins/categories        1,488/mo
    #   narrative trending tracker                  1,488/mo
    #   narrative predictor /coins/markets          2,976/mo
    #   narrative evaluator (batch + fallback)      2,976/mo
    #   counter detail /coins/{id}                  1,488/mo
    #   secondwave /coins/markets                   1,488/mo
    #                                              ----------
    #                                              11,904/mo
    #
    # Under the CORRECTED model (bounded narrative fan-out + the evaluator,
    # which the first model omitted entirely) the discovery structural maximum
    # at every-8th is 55,924/month against the 65,000 envelope — 14.0% margin.
    # every-5th remains demonstrably impossible; 7 was retired in favour of 8
    # because the margin has to survive estimation error in bounds on loops.
    #
    # Hard caps protect against an estimation mistake; they are not a substitute
    # for a planned workload that fits. Operator ratification is wanted for this
    # deviation — set COINGECKO_DISCOVERY_INTERVAL_CYCLES=5 in .env to restore
    # the original profile, accepting that discovery will then hit its envelope
    # partway through the month and stop.
    COINGECKO_DISCOVERY_INTERVAL_CYCLES: int = Field(default=8, ge=1, le=1440)

    # Open-boundary pricing LIVENESS bound (monthly-budget repair 2026-08-21).
    # The engine's step-0c gate is a REGISTRY check ("a CG lane serves this
    # token_id shape"); this is the LIVENESS check ("CoinGecko ITSELF answered
    # recently").
    #
    # Deliberately NOT price_cache freshness. price_cache is also written by
    # DexScreener (outcome_ledger's dex enrollment poller), so its freshness
    # says nothing about CoinGecko: a fresh Dex row would present a dead
    # provider as alive. The signal is cg_budget.last_success_at, set only by a
    # CoinGecko HTTP 200. With CG dark — exhausted monthly credits, a
    # suspended account, a spent discovery envelope — a position can open and
    # then be unmonitored (no trailing stop, no SL) until max_duration
    # force-closes it at entry_price with a fabricated pnl_pct=0.
    #
    # Sized against the discovery cadence: at 8 cycles x 60s a discovery round
    # runs every ~8 min, so 1800s is >3x headroom and will not trip on ordinary
    # jitter or a couple of skipped rounds. Note this bounds PROVIDER silence,
    # not price_cache staleness. Set to 0 to disable the gate entirely (NOT recommended — that
    # restores the unmonitored-position class).
    PAPER_OPEN_CG_PRICING_MAX_AGE_SEC: int = Field(default=1800, ge=0, le=86_400)

    # -------- CoinGecko MONTHLY-CREDIT budget (2026-08-21 repair) --------
    # The provider enforces calls/minute and calls/MONTH as INDEPENDENT limits.
    # coingecko_limiter owns the first; these own the second. Modeling only the
    # first is what exhausted the Basic plan's 100,000 credits at 100.0% on
    # 2026-08-21 with 11 days to the reset.
    #
    # Partitioned rather than one shared pool, because a single number lets
    # discovery spend the allowance that keeps held positions re-priceable —
    # and an unpriceable open position is the GA-01 failure class.
    #
    #   discovery  65,000  <- corrected full model projects 55,428 (14.7% margin)
    #   critical   30,000  <- reserved for held-position re-pricing
    #   operational 5,000  <- reconciliation / margin for model error
    #   ------------------
    #   total     100,000
    #
    # v1 deliberately does NOT let unused critical reserve spill back into
    # discovery. That optimization needs a measured billing month behind it.
    COINGECKO_MONTHLY_CREDIT_ALLOWANCE: int = Field(default=100_000, ge=0)
    COINGECKO_MONTHLY_DISCOVERY_CREDITS: int = Field(default=65_000, ge=0)
    COINGECKO_MONTHLY_CRITICAL_CREDITS: int = Field(default=30_000, ge=0)
    COINGECKO_MONTHLY_OPERATIONAL_CREDITS: int = Field(default=5_000, ge=0)
    # Pace alarm: page when PROJECTED month-end consumption exceeds the
    # allowance by this fraction. Projection beats absolute 50/75/90% marks —
    # 60% spent on day 3 is an emergency, 60% on day 27 is fine.
    COINGECKO_BUDGET_PACE_ALERT_RATIO: float = Field(default=1.10, ge=1.0, le=10.0)
    # Durability window for the credit ledger, in CREDITS rather than minutes.
    # Persisting only in the hourly pass would lose up to an hour of spend to a
    # crash/deploy, and the process would come back believing it has capacity it
    # already burned. Bounded by spend rather than clock because the exposure
    # scales with spend.
    #
    # 25 credits is roughly 3.5 discovery rounds at the 7-call bundle — so
    # the worst-case loss is a few rounds, not the ~60 rounds an hourly-only
    # flush could discard. (An earlier comment claimed "well under one discovery
    # round", which was simply wrong: 25 > 7.)
    COINGECKO_BUDGET_FLUSH_EVERY_CREDITS: int = Field(default=25, ge=0, le=10_000)
    # /key reconciliation. Provider is the ACCEPTANCE truth; positive drift is
    # real capacity gone that no local counter explains, so it reduces
    # non-critical capacity rather than only being logged.
    COINGECKO_BUDGET_RECONCILE_ENABLED: bool = True
    # Discovery activation. Default FALSE so a deploy before the September 1
    # credit reset cannot resume CoinGecko discovery merely because the local
    # ledger starts empty. "Dark until the reset" is then a property of the
    # tree, not of an operator remembering not to deploy.
    COINGECKO_DISCOVERY_ENABLED: bool = False

    # -------- Executable caps on the VARIABLE CoinGecko consumers --------
    # Round-4 finding: the budget model had fixed per-pass estimates for
    # consumers whose call count is a LOOP BOUND, not a constant. The narrative
    # pass fans out as:
    #
    #   1 /coins/categories
    # + 1 /search/trending
    # + max_heating_per_cycle                           -> fetch_laggards
    # + max_heating * max_picks_per_category            -> scored-token detail
    # + max_heating * max_picks_per_category            -> control-token detail
    #
    # And the multipliers are LEARNER-TUNABLE, so the default (5,5 -> 57/pass)
    # is not the bound. scout/narrative/strategy_bounds.py permits
    # max_heating_per_cycle up to 10 and max_picks_per_category up to 10, i.e.
    # 1 + 1 + 10 + 100 + 100 = 212 calls/pass ~= 315,000/month from ONE
    # consumer, against a 100,000 plan. Even at the defaults it is 84,816/month
    # — more than the entire 65,000 discovery envelope. No main-bundle cadence
    # can absorb that, so the fan-out itself has to be bounded.
    #
    # This cap covers the whole per-pass fan-out (laggards AND both detail
    # loops), so the monthly model is 2 + this number per pass regardless of
    # how the learner tunes the strategy knobs.
    #
    # Today's production measurement (2026-08-19: narrative.observe_empty x44,
    # zero heating/laggard/detail events) shows the pass aborting right after an
    # empty /coins/categories — so the observed cost is ~1 call/pass. That is an
    # artifact of a failing upstream, NOT a bound, and it would vanish the moment
    # categories starts returning data. Modelling on it would repeat exactly the
    # mistake that produced the 62k headline.
    #
    # These caps degrade the pass (fewer tokens enriched) rather than failing it.
    NARRATIVE_MAX_CG_CALLS_PER_PASS: int = Field(default=8, ge=0, le=200)
    # /simple/price fallback chunks in evaluate_pending. The loop is already
    # bounded at 3 chunks (min(len(missing),60) step 20); this makes the bound a
    # SETTING so the budget model can cite it instead of re-deriving it.
    NARRATIVE_MAX_CG_EVAL_CALLS_PER_PASS: int = Field(default=4, ge=0, le=100)

    # Telegram resolver monthly CoinGecko ceiling. The resolver is EVENT-DRIVEN
    # (TG_SOCIAL_ENABLED=true in prod), so it has no natural cadence: a contract
    # can cost one lookup, a cashtag path costs search + markets, and misses can
    # be retried. The budget table previously carried "620/month" for it as an
    # ESTIMATE while calling the table a bound — the exact error this repair
    # exists to stop.
    #
    # Capping it inside OPERATIONAL is not enough on its own: TG volume could
    # consume the whole 5,000 bucket and starve /key reconciliation and the
    # outcome-ledger poller, which are FIXED duties sharing it. So the resolver
    # gets its own sub-ceiling and those two keep a guaranteed floor.
    COINGECKO_TG_RESOLVER_MONTHLY_CREDITS: int = Field(default=1_000, ge=0)
    # Reserved for /key reconciliation (~744/mo) + the ledger enrollment poll
    # (~2,976/mo). Nothing discretionary may push OPERATIONAL below this.
    COINGECKO_OPERATIONAL_FIXED_FLOOR_CREDITS: int = Field(default=3_800, ge=0)
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
    # Activation ceilings (both fail-closed; consumed by the .sh wrapper).
    # The writer prices via the KEYLESS GeckoTerminal public API — NOT the paid
    # CoinGecko plan — so these bound GT requests, which are shared per-IP with
    # the DEX-discovery lane. 25 identities/run = 50 provider calls against GT's
    # ~30 req/min tier; measured steady-state load is 1-2 identities/run.
    SOURCE_CALL_SNAPSHOT_MAX_IDENTITIES_PER_RUN: int = Field(default=25, ge=1, le=500)
    SOURCE_CALL_SNAPSHOT_MAX_REQUESTS_PER_DAY: int = Field(
        default=2000, ge=1, le=100_000
    )
    # Stage B CG coin_id lane. That lane prices from the LOCAL price_cache and
    # spends no provider requests, so its per-run ceiling bounds WRITE RATE, not
    # provider budget; the age bound decides how stale a cached price may be and
    # still count as an observation. Declared here for the same reason as the
    # knobs above — the .sh wrapper reads them from .env, and extra="forbid"
    # turns an undeclared knob into a boot-time ValidationError for the WHOLE
    # pipeline, not just this writer.
    SOURCE_CALL_SNAPSHOT_MAX_CG_IDENTITIES_PER_RUN: int = Field(
        default=32, ge=1, le=500
    )
    SOURCE_CALL_SNAPSHOT_MAX_PRICE_CACHE_AGE_MIN: int = Field(
        default=90, ge=1, le=10_080
    )

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

    # DASH-07 / SIG-09 (display-only regime strip, 2026-07-10): trailing-7d
    # per-trade paper PnL hostile-cue threshold. When the trailing-7d
    # SUM(pnl_usd)/COUNT over closed paper_trades falls BELOW this value, the
    # Today's Focus regime strip renders the figure with a hostile (red) tint.
    # DISPLAY-ONLY — gates no trading behaviour; the throttle half of SIG-09 is
    # evidence-gated and out of scope. Env-tunable server-side. Default
    # -10.0 USD/trade.
    REGIME_HOSTILE_PER_TRADE_USD: float = -10.0

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

    # ALR-09: base URL for dashboard deep links appended to paper-trade-open
    # TG alerts (e.g. http://HOST:8000/#/trade/{paper_trade_id}). The alert
    # body's final line is a one-tap page→row link into the Trading tab.
    # Operator-overridable via .env; empty disables the deep-link line.
    DASHBOARD_BASE_URL: str = "http://89.167.116.187:8000"

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

    # DEX-first Phase 1 (design_dex_first_discovery_2026_07_20): GT new-pools
    # research lane. Observe-only — discoveries persist to dex_pool_discoveries
    # + contract_coin_map; no candidate emission, no scoring, no alerts, no
    # paper trades. When False the pipeline is byte-identical (no GT new_pools
    # call). Public GT API (keyless): NOT on the CoinGecko credit budget.
    DEX_DISCOVERY_ENABLED: bool = False
    DEX_DISCOVERY_NETWORKS: list[str] = ["solana"]
    # Poll 1 cycle in N (60s cycles -> default one new_pools page per network
    # every ~3 min; GT public tier is 30 req/min so this is far under budget).
    DEX_DISCOVERY_POLL_EVERY_N_CYCLES: int = Field(default=3, ge=1, le=60)
    # Drop dust pools at ingest; GT reserve_in_usd below this is not recorded.
    DEX_DISCOVERY_MIN_LIQUIDITY_USD: float = Field(default=1000.0, ge=0.0)
    # PR-B: outcome-ledger enrollment budget per discovery pass — the ONLY
    # limit on ledger writes from this lane (no embedded fallback). Each NEW
    # discovery inside the budget is recorded via record_emission
    # (kind=gated_out_sample, surface=dex_new_pool); fresh mints without
    # in-DB price coverage are enrolled for DexScreener labeling. 0 disables
    # ledger writes while keeping discovery itself on.
    DEX_DISCOVERY_LEDGER_ENROLL_PER_CYCLE: int = Field(default=3, ge=0, le=100)
    # PR-C watchdog SLOs (read by scripts/dex-discovery-watchdog.sh from .env).
    # Staleness bar for the durable successful-poll heartbeat
    # (ingest_watchdog_state source='dex_discovery'); the lane polls ~every 3
    # minutes, so 2h is generous-but-decisive. Named clock-skew allowance for
    # future-dated heartbeats (beyond it = invalid-state breach, never healthy).
    DEX_DISCOVERY_POLL_STALENESS_ALERT_HOURS: int = Field(default=2, ge=1, le=168)
    DEX_DISCOVERY_WATCHDOG_CLOCK_SKEW_SECONDS: int = Field(default=300, ge=0, le=3600)

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
    # Weekly lesson consolidation is COMMENTARY ONLY — it rewrites the
    # `lessons_learned` prose and calls no Strategy.set, so it controls none of
    # the 14 strategy parameters. Default OFF: the owner ruling removed paid
    # model access from the learning path, and this was its last required
    # Anthropic caller. Historical lessons remain readable; nothing is deleted.
    # Set True only if a provider is funded and commentary is wanted again.
    NARRATIVE_WEEKLY_COMMENTARY_ENABLED: bool = False

    NARRATIVE_WEEKLY_LEARN_DAY: int = 6
    NARRATIVE_ENABLED: bool = False
    NARRATIVE_SNAPSHOT_RETENTION_DAYS: int = 7
    # REC-02: window + threshold for the narrative CA-resolver error-rate alarm.
    # The pipeline watchdog counts narrative_resolver_errors rows within this
    # window and pages when the count exceeds the threshold. Cross-process: the
    # /api/coin/lookup endpoint (dashboard) records errors; the pipeline reads
    # the count. Wiring fix for the branch previously fed a hardcoded 0 (§12a).
    NARRATIVE_RESOLVER_ERROR_WINDOW_HOURS: int = Field(default=24, ge=1)
    NARRATIVE_RESOLVER_ERROR_ALARM_THRESHOLD: int = Field(default=5, ge=1)
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
    # Ruling B (2026-08-16), executed 2026-08-22 after a verified backup.
    #
    # 21 -> 14 is a DESTRUCTIVE change: once the shorter cutoff has deleted
    # rows, restoring the value does not restore them.
    #
    # It is authorized because BOTH readers of volume_snapshots are bounded at
    # <= 14 days, which is what distinguishes B from ruling A:
    #   * get_vol_7d_avg      -- scanned_at >= now-7d  (hard 7-day window)
    #   * get_volume_history  -- scanned_at >= now-`days`, and the only
    #                            production caller (secondwave/detector.py)
    #                            passes days=SECONDWAVE_COOLDOWN_MAX_DAYS=14.
    # A was falsified because ITS binding reader, get_recent_scores(limit=3),
    # has no date window at all -- so retention silently supplied the boundary
    # and 22.8% of tokens changed. Here, hiding >14d rows cannot change either
    # reader's output by construction; the prod replay agreed exactly (1668
    # vol_7d-eligible contracts before and after, delta 0).
    #
    # Both boundaries are TRAILING from `now` -- the prune deletes
    # scanned_at <= now-14d and the readers ask for scanned_at >= now-Nd -- so
    # they abut without a gap and no lateness margin is required. This is the
    # structural difference from #548's volume_history_cg, whose window was
    # RETROSPECTIVE ([emitted, emitted+7d]) and therefore truncated from the
    # left at zero margin. If a future reader anchors its window to an event
    # time instead of `now`, that equivalence breaks and this floor must be
    # re-derived as horizon + measured lateness margin.
    VOLUME_SNAPSHOTS_RETENTION_DAYS: int = 14

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

    # -------- Hygiene-lane table retention (INF-02 / INF-06) --------
    # Independent hourly prunes via main._run_hourly_maintenance.
    # INF-02: trade_decision_events was the fastest-growing unpruned table
    # (~16.5K rows/day, absent from every prune list). ge floor = largest
    # consumer lookback (suppression_cost_rollup --window-days default 7d;
    # check_trade_decision_events uses 15min; evaluator's per-trade dedup is
    # bounded by PAPER_MAX_DURATION_HOURS=48h). 45d default clears them all.
    TRADE_DECISION_EVENTS_RETENTION_DAYS: int = Field(default=45, ge=7)
    # volume_history_cg retention. ONE owner: Database.prune_volume_history_cg,
    # driven from main._run_hourly_maintenance. The spike detector's duplicate
    # hardcoded `-7 days` prune has been removed (scout/spikes/detector.py).
    #
    # The previous comment here claimed 7d "matches the longest reader horizon
    # (ledger r7d labeling + spike 7d average) — harmless duplication". That was
    # FALSE, because it omitted LABEL-PROCESSING LATENESS.
    #
    # The ledger labeler computes peak7d as MAX(price) over the CLOSED window
    # [emitted, emitted + 7d] (scout/outcome_ledger.py::_peak_price_in_window).
    # The row it needs at the LEFT edge is exactly 7.0 days old at the earliest
    # instant the ledger row becomes finalizable (now >= emitted + 7d). A 7d
    # prune therefore leaves ZERO margin: any lateness at all truncates the
    # window from the left and yields a wrong-but-plausible peak7d rather than
    # a NULL — a silently biased label, not a missing one.
    #
    # Measured on prod 2026-08-18, n=273,296 completed ledger rows, lateness
    # past the r7d deadline (labeled_at - emitted_at - 7d):
    #     p50 0.153d | p90 2.529d | p99 2.857d | max 2.910d
    # i.e. essentially EVERY labeled row was already losing left-edge data, and
    # p90 rows were computing "peak7d" over roughly 4.5 days.
    #
    # Retention is therefore horizon + an explicit permitted lateness margin,
    # never a bare number. The margin is measured (max 2.910d), rounded up to a
    # whole day. Do NOT lower either value without re-running that measurement.
    LEDGER_LABEL_MAX_LATENESS_DAYS: int = Field(default=3, ge=1)
    VOLUME_HISTORY_CG_RETENTION_DAYS: int = Field(default=10, ge=8)

    # DASH-05 moved-already / too-late postmortem recorder. FORWARD-recording
    # only: gainers_snapshots has a 7-day retention so pre-run T-minus evidence
    # for past monsters (ANSEM +3,354% at TOO_LATE/score 0) is already gone —
    # backfill is impossible. Flag-gated OFF: when False the recorder never runs
    # and the pipeline is byte-identical (no scorer/gate/trade/alert change). The
    # detection predicate MIRRORS the dashboard's _trade_window_state "late" state
    # (dashboard/db.py): an OPEN paper trade whose pct-from-entry exceeds
    # MOVED_ALREADY_RUN_PCT_THRESHOLD. Dedup is per token via a UNIQUE(token_id)
    # on moved_already_postmortems — each token captures one postmortem the first
    # time it crosses into the moved-already state. See DASH-05 in
    # tasks/backlog_fable_analysis_2026_07_10.md.
    MOVED_ALREADY_POSTMORTEM_ENABLED: bool = False
    # 25.0 mirrors dashboard/db.py _trade_window_state's "late" boundary (pct > 25).
    MOVED_ALREADY_RUN_PCT_THRESHOLD: float = Field(default=25.0, gt=0)
    # T-minus evidence window (days) for the gainers_snapshots capture. Bounded at
    # 7 because gainers_snapshots is 7-day retention — nothing older exists.
    MOVED_ALREADY_EVIDENCE_WINDOW_DAYS: int = Field(default=7, ge=1, le=7)

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
    # Default True for 4-week measurement.
    #
    # Week-1 calibration performed 2026-07-11 (REC-05a, deferred at deploy).
    # Method (design_sqlite_wal_profile.md §D5 / wal_summary.sh baseline
    # section): SQLITE_WAL_BLOAT_BYTES = ceil(1.5 × p95_wal / 5MB) × 5MB,
    # floored at the 50MB default. Post-P0-vacuum steady state (per
    # tasks/findings_w3_analysis_gates_2026_07_11.md §REC-05): WAL ~14.1MB
    # median, spikes into the tens-of-MB, 0 bloat events >50MB in the last
    # 7d. Taking p95 ≈ 40MB from the spike ceiling:
    #   1.5 × 40MB = 60MB → round up to 5MB = 60MB → ≥50MB floor → 60MB.
    # This sits above the observed spike ceiling (so recurring tens-of-MB
    # spikes stop producing spurious WARNINGs) and below the 100MB
    # checkpoint threshold (so genuine runaway still warns pre-truncation).
    # Still env-tunable: override SQLITE_WAL_BLOAT_BYTES in .env once longer
    # forward telemetry refines p95. wal_summary.sh reads the same env var.
    SQLITE_WAL_PROFILE_ENABLED: bool = True
    SQLITE_WAL_BLOAT_BYTES: int = 60_000_000

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

    # -------- Dead-table retirement (NAR-06 + INF-07) --------
    # Opt-in-destructive kill switch for the retire_dead_tables_v1 migration
    # (scout/db.py). The migration's DROP TABLE statements are IRREVERSIBLE, so
    # they fire ONLY when this flag is true at a deploy — the flag is the
    # recorded-approval hook. Default-off (fail-closed): the migration is a
    # no-op that records nothing until the operator flips it. See db.py
    # _migrate_retire_dead_tables_v1 for the fail-closed rationale.
    RETIRE_DEAD_TABLES_ENABLED: bool = False

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

    # SIG-10: trust-weighted paper-trade sizing (paper-only, fail-closed).
    # When enabled, open_trade scales PAPER_TRADE_AMOUNT_USD by the opening
    # signal's trust tier (resolved from the signal-trust registry maturity
    # state at open time; see scout/trading/trust_sizing.py). Default OFF —
    # flat sizing is the pinned legacy behavior until the operator opts in.
    # A 0.0 multiplier (non_tradable) skips the open entirely. This is a
    # would_be_live sizing-policy knob; it does NOT relax the registry's
    # not_for_sizing gate for live/production paths (SIG-03 dispatch
    # quarantine still supersedes for narrative_prediction/tg_social).
    PAPER_TRUST_SIZING_ENABLED: bool = False
    # tier=multiplier CSV (same shape as LIVE_SIGNAL_SIZES). Keys are trust
    # tiers, not signal types: trusted / experimental / non_tradable.
    PAPER_TRUST_SIZE_MULTIPLIERS: str = "trusted=1.0,experimental=0.5,non_tradable=0.0"

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

    # BL-NEW-ALERT-UNIVERSE-FILTER: operator-facing universe guard on the
    # paper-trade-open Telegram alert path. Some CoinGecko-sourced ids are
    # tokenized equities / ETFs (e.g. `spy-bstocks-tokenized-stock`,
    # `invesco-qqq-etf-ondo-tokenized-etf`) that fall outside this product's
    # early micro-cap crypto universe — alerting on them is a trader-trust
    # killer. When enabled, any alert whose token_id (the CoinGecko slug)
    # contains one of EXCLUDE_ID_PATTERNS (case-insensitive substring) is
    # suppressed and audited as outcome='blocked_eligibility'
    # detail='universe_filter:<pattern>'. Default OFF (observe-first); the
    # pattern list is operator-extensible via .env (comma-separated). NOTE: the
    # paper ENGINE still opens trades on these tokens — this filter only stops
    # the operator-facing send.
    #
    # The single default `-tokenized-` covers every observed prod offender
    # (tokenized stocks AND ETFs); no legitimate major/large-cap slug contains
    # "tokenized". CAUTION: patterns are RAW case-insensitive substrings, so
    # keep them specific — a short pattern like `spy` would suppress legit
    # alerts for any slug containing those letters (e.g. `spy`-thing).
    #
    # NoDecode (pydantic-settings): `list[str]` is a "complex" field, so
    # EnvSettingsSource would json.loads() the raw env value BEFORE the
    # field_validator runs — turning a comma-separated .env value into a
    # SettingsError at construction (boot crash-loop). NoDecode suppresses that
    # eager JSON decode so the raw string reaches
    # parse_alert_universe_exclude_id_patterns below.
    ALERT_UNIVERSE_FILTER_ENABLED: bool = False
    ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS: Annotated[list[str], NoDecode] = [
        "-tokenized-",
    ]

    # ALR-03 engine universe-exclusion: the ALERT_* filter above suppresses
    # only the operator SEND — the paper ENGINE still OPENS trades on these
    # out-of-universe ids (tokenized equities / ETFs), contaminating
    # paper_trades and every downstream PnL surface. When enabled,
    # engine.open_trade blocks the OPEN for any token_id matching the SAME
    # EXCLUDE_ID_PATTERNS above (one universe definition, shared via
    # scout.token_ids.match_universe_exclude) with decision reason
    # 'universe_excluded'. Fail-closed (default OFF) so the two layers roll out
    # independently and a missing / garbled .env value can never silently start
    # dropping opens. Detection / tracker surfaces are unaffected — only the
    # paper-trade OPEN is blocked.
    ENGINE_UNIVERSE_FILTER_ENABLED: bool = False

    # ALR-02 detection-time alert lane. Fires an "early candidate detected"
    # Telegram alert on the SCORING pass — BEFORE the paper engine's dispatch
    # gate (which rejects ~99.99%) decides. Reframes the operator alert from
    # "the robot acted" to "an early candidate is here", serving the core
    # product promise (beat CG Highlights by minutes). Default OFF (a new noise
    # surface; observe-first). See tasks/design_detection_time_alert_lane.md.
    #
    # Trigger: a CG-sourced candidate first seen within DETECTION_ALERT_MAX_AGE_MIN
    # that is NOT yet on CG trending (no trending snapshot, or a trending
    # crossover later than the detection instant — see
    # engine._compute_lead_time_vs_trending; negative lead_time = early).
    # Reuses the operator universe filter (ALERT_UNIVERSE_*) and the 24h
    # per-token dedup window (TG_ALERT_DEDUP_WINDOW_HOURS). Audit rows land in
    # tg_alert_log with signal_type='detection_lane',
    # detail='detection_lane[:reason]' — no schema/CHECK change.
    DETECTION_ALERT_LANE_ENABLED: bool = False
    # Daily send cap (calendar day, UTC) — the hard noise budget. Counts
    # tg_alert_log rows with outcome='sent' detail='detection_lane' since UTC
    # midnight; overflow is audited outcome='blocked_cooldown'
    # detail='detection_lane:rate_limit'. 0 = a soft off-switch (no sends).
    DETECTION_ALERT_MAX_PER_DAY: int = Field(default=5, ge=0)
    # Freshness ceiling: only candidates whose authoritative
    # candidates.first_seen_at is within this many minutes are eligible, so the
    # lane surfaces genuinely-new detections rather than stale not-yet-trending
    # coins re-scored every cycle. Operationalizes "first_seen" in the trigger.
    DETECTION_ALERT_MAX_AGE_MIN: int = Field(default=180, ge=0)

    # ALR-02 quality gate (2026-07-14). The scarce daily budget is spent
    # HIGHEST-SCORE-FIRST among candidates whose quant_score clears this bar,
    # not merely freshest-first. Motivation — a 3.5-day evaluation
    # (2026-07-11→07-14, lane forced on, 20 alerts sent): the ungated
    # freshest-first lane filled all 5/day slots with quant_score=0 candidates
    # (0/20 ever trended, several were $50M-$228M established coins mislabeled
    # "EARLY DETECT"), while 8 of 10 genuine pre-trending early-catches in the
    # pool DID fire scoring signals (cat-in-hood qs=8, cash-dog-in-hood qs=10,
    # bycocket qs=4, ...) and were never sent. The gate + score-ordered
    # selection recover them (recall 8/10, ~15x precision lift; the 2 misses,
    # dodo/iota, are larger established coins that fired zero signals — an
    # accepted tradeoff). See tasks/design_detection_time_alert_lane.md.
    #
    # Single source of truth: a candidate qualifies iff quant_score >= this bar.
    # Because every scoring signal contributes positive points, quant_score == 0
    # iff no signal fired — so the default of 1 is exactly "at least one scoring
    # signal fired" (the validated coarse gate), with no separate boolean knob
    # to disagree with it. Tighten post-soak to raise precision (5 → ~71% on the
    # evaluation cohort) or set 0 to fully disable the gate (rollback) — both are
    # one clean .env change. The gate runs BEFORE the daily cap; the
    # pool→gated→sent funnel is emitted each run as a structured
    # `detection_alert_funnel` log.
    DETECTION_ALERT_MIN_QUANT_SCORE: int = Field(default=1, ge=0, le=100)

    # DORMANCY FLAG (reviewer dormant-deployment ruling, 2026-07-26). Master
    # kill-switch for the ENTIRE decision-receipt subsystem. **Default DISABLED**:
    # enabling requires explicit configuration + a clean process activation. When
    # False, the detection lane still runs (candidate evaluation, gating, ranking,
    # and SENDING are BYTE-IDENTICAL) but the receipts subsystem is fully skipped —
    # NO receipt inserts, NO replay/conflict lookups, NO disk-pressure work, NO
    # archive work, NO cohort accrual. Every receipt write is skipped BEFORE any
    # persistence/lookup. A single structured ``detection_receipts_disabled`` log
    # fires at the first skip per process; the per-cycle summary is still emitted
    # but clearly flags ``receipts_disabled=true`` + ``coverage_healthy=false`` so a
    # reader can NEVER mistake dormancy for clean coverage. Lets the receipt code
    # deploy DORMANT (present but inert) ahead of replacement-cohort activation.
    DETECTION_RECEIPTS_ENABLED: bool = False

    # Retention floor (days) for detection_decision_receipts — the ALR-02
    # decision-receipt audit substrate (behavior-neutral observability that
    # records one receipt per evaluated detection-lane candidate so the
    # gate-FAILER comparison cohort is recoverable). Pruned hourly in
    # scout/main.py::_run_hourly_maintenance. Floor >= 120d — DELIBERATELY well
    # beyond the analysis lifecycle: receipts MUST survive through BOTH outcome
    # horizons (24h + 72h from index decision), reconciliation, manifest freeze,
    # and final analysis. Pruning a row merely because its 72h window matured
    # INVALIDATES the cohort (see tasks/prereg_detection_gate_enrichment_cohort.md);
    # the 120d floor guarantees the whole cohort lifecycle fits on-disk. The
    # ``ge=120`` floor makes it structurally impossible to set a value that
    # could prune mid-analysis.
    DETECTION_DECISION_RECEIPTS_RETENTION_DAYS: int = Field(default=120, ge=120)

    # Cohort-completeness prune guard (reviewer correction e). An ISO-8601 UTC
    # timestamp marking the point up to which the gate-enrichment cohort's
    # manifest/final-analysis lifecycle is COMPLETE. Empty (the default) means
    # NO cohort has been closed → detection_decision_receipts is NEVER pruned,
    # so an in-flight cohort's receipts always survive manifest freeze + final
    # analysis. When set, only rows whose ``decided_at`` is at/older than BOTH
    # this marker AND the retention floor are pruned; rows newer than the marker
    # (a still-open cohort) are never pruned. Pruning receipts before their
    # cohort analysis completes INVALIDATES the cohort (see the prereg doc); this
    # guard makes that structurally impossible without an explicit operator
    # close-out. Set it only after the cohort's final analysis is frozen.
    DETECTION_RECEIPTS_COHORT_CLOSED_AT: str = ""

    # --- Receipt lifecycle archive (approved architecture, 2026-07-26) --------
    # Ordinary post-index receipts (the ~99.9% too_old re-polls) leave the hot
    # scout.db table ONLY via the 7-step fail-closed archival transaction in
    # scout/trading/receipt_archive.py: select frozen range → write temp
    # partition → flush+verify → reconcile count+hash → atomically publish
    # partition+manifest → confirm independent durable copy → THEN delete hot
    # rows. Cold partitions are gzip-compressed JSONL; an integrity manifest lives
    # HOT in scout.db. See tasks/capacity_detection_receipts_2026_07.md.
    #
    # Default OFF — archiving only runs when explicitly enabled. The hot tier
    # keeps: cohort defs/status, analytical-index receipts, in-lifecycle rows,
    # and anything unreconciled/under-investigation.
    DETECTION_RECEIPT_ARCHIVE_ENABLED: bool = False
    # Local directory for published cold partitions + temp partitions. Empty
    # disables archiving (the writer is inert). Must be on a durable filesystem.
    DETECTION_RECEIPT_ARCHIVE_DIR: str = ""
    # Maturity horizon (hours): a receipt is archive-ELIGIBLE only once its
    # outcome horizon has matured AND its source cycle is reconciled. >= 72 so the
    # 72h key-secondary endpoint is always observed on hot rows before archival.
    DETECTION_RECEIPT_ARCHIVE_HORIZON_HOURS: int = Field(default=72, ge=72)
    # Max receipts per cold partition (bounds partition size + verify cost).
    DETECTION_RECEIPT_ARCHIVE_PARTITION_MAX_ROWS: int = Field(
        default=50_000, ge=1_000, le=1_000_000
    )
    # Independent durable off-host copy destination (step 6). A LOCAL-ONLY dir is
    # INSUFFICIENT for durability, so this is a path the box's existing off-host
    # mechanism replicates (e.g. an rsync/scp target dir or an S3-synced dir).
    # DISCOVERY (srilu, 2026-07-26): the gecko backup (gecko-backup.service) is
    # LOCAL-ONLY (GECKO_BACKUP_DIR=/root/gecko-alpha, keep-3, no rsync/scp/s3);
    # no off-host tooling (rclone/restic/borg/aws) is provisioned. So this is
    # EMPTY by default → step 6 fails closed and holds rows HOT until an operator
    # provisions an off-host destination. "off-host = operator-provisioned
    # dependency." When set, the archiver requires the partition to also exist +
    # hash-match at this destination before deleting hot rows.
    DETECTION_RECEIPT_OFFHOST_DIR: str = ""
    # Disk-pressure fail-closed guard. Default OFF (a new behavior; observe-first
    # + avoids surprising the lane on a low-disk dev/CI box). When enabled, on
    # breach the receipts writer stops ACCRUING (send path UNCHANGED), alerts
    # (parse_mode=None), marks coverage unhealthy, and preserves existing
    # receipts+manifests — never silently prunes/samples/reduces detail.
    DETECTION_RECEIPT_DISK_GUARD_ENABLED: bool = False
    DETECTION_RECEIPT_DISK_MIN_FREE_GB: float = Field(default=10.0, ge=1.0)
    DETECTION_RECEIPT_DISK_MIN_FREE_PCT: float = Field(default=15.0, ge=1.0, le=90.0)
    # Cooldown (hours) between disk-pressure TELEGRAM alerts. Without it the lane
    # would page on every breached cycle (~500/day at 2.9-min cadence). The
    # structured ``detection_receipt_disk_pressure`` WARNING stays per-cycle in the
    # journal; only the operator page is rate-limited. The last-alert timestamp is
    # in-memory (module-level) — a process restart resets it, which is acceptable
    # (a restart is itself a signal, and one extra page is harmless).
    DETECTION_RECEIPT_DISK_ALERT_COOLDOWN_HOURS: float = Field(
        default=6.0, ge=0.0, le=168.0
    )
    # Hard-critical free-space bound (GB). Below this (a materially worse crossing
    # than DETECTION_RECEIPT_DISK_MIN_FREE_GB), a page is dispatched IMMEDIATELY,
    # bypassing the cooldown — an escalation. Must be < the min-free bound (a
    # deeper breach). The cooldown is notification-rate control ONLY; it never
    # delays receipt suspension or coverage invalidation, which fire on ANY breach.
    DETECTION_RECEIPT_DISK_CRITICAL_FREE_GB: float = Field(default=3.0, ge=0.5)

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

    # SIG-03 dispatch-quarantine: signal_types whose paper-trade OPENS are
    # blocked at the single dispatch authority (scout/trading/engine.py
    # :meth:`TradingEngine.open_trade`). Detection / tracker / research surfaces
    # are UNAFFECTED — this only stops the paper-trade admission. Blocked opens
    # are recorded to trade_decision_events with reason='quarantined'. Removes
    # standing negative-EV lanes (narrative_prediction 16% win / −$1,542/6w;
    # tg_social 21% win / −$324) without deleting their detection telemetry.
    # Empty list disables the feature (clean revert). Operator-extensible via
    # .env (comma-separated). NOTE: tg_social dispatches through
    # scout/social/telegram/dispatcher.py (NOT should_open), and
    # narrative_prediction through should_open — engine.open_trade is the ONE
    # place BOTH lanes converge, so the gate lives there (single source).
    #
    # NoDecode (pydantic-settings): `list[str]` is a "complex" field, so
    # EnvSettingsSource would json.loads() the raw env value BEFORE the
    # field_validator runs — turning a comma-separated .env value into a
    # SettingsError at construction (boot crash-loop). NoDecode suppresses that
    # eager JSON decode so the raw string reaches
    # parse_signal_dispatch_quarantine below.
    SIGNAL_DISPATCH_QUARANTINE: Annotated[list[str], NoDecode] = [
        "narrative_prediction",
        "tg_social",
    ]

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
    # Bounded-pilot admission cap for losers_contrarian. 0 = no cap (current
    # behaviour, and the default). When > 0, `trade_losers` refuses to open the
    # (N+1)th trade of the current pilot cohort.
    #
    # The cohort is anchored on `signal_params.drawdown_baseline_at`, which
    # `revive_signal_with_baseline` stamps atomically at revival — an existing
    # persisted anchor, not a new primitive, and immutable for the life of a
    # revival. Trades opened before that instant belong to an earlier cohort and
    # are not counted.
    #
    # Why this exists: an operator watching for "entry 200 happened" and then
    # disabling the row is a monitored cutoff, not a cap — `trade_losers`
    # iterates the full fresh-losers batch and can admit more entries before the
    # disable write lands. A pilot that states a bound must be able to refuse
    # the entry that would exceed it.
    #
    # Scope of the guarantee: EXACT under one active admission writer (asyncio
    # runs one coroutine at a time and `trade_losers` awaits each open). It is
    # check-then-act, not an atomic reservation, so concurrent writers can
    # overshoot by the number of racers — the same property `engine.open_trade`
    # documents for its own duplicate/exposure checks. Overshoot is detected and
    # logged at error level (`pilot_entry_cap_exceeded`) rather than silently
    # tolerated.
    PAPER_LOSERS_PILOT_MAX_ENTRIES: int = 0
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
    # Catchup replays historical messages through the SAME path as live ones
    # (handle_new_message), and nothing else filters on message age — so a
    # catchup pass would alert/trade on months-old calls as if they just
    # fired. A channel whose watermark is 0 would replay its ENTIRE history.
    # This bounds that: a replayed message older than the threshold is still
    # persisted, but is not resolved, alerted, or traded.
    #
    # Applies to CATCHUP REPLAY ONLY (handle_new_message(is_replay=True)),
    # never to live messages. The age is derived from our clock against
    # Telegram's, so a fast local clock inflates it; gating live traffic on
    # that would let clock skew silently suppress the entire signal lane
    # while every health surface still reported green. See the guard in
    # listener.py.
    #
    # 720 = 12h. Sized to the hazard, not to signal freshness: the replay
    # this exists to stop is months old, while a restart/outage gap is hours,
    # and messages recovered from such a gap are still worth acting on. A
    # tighter bound (60) would discard same-day recovery to block nothing
    # extra. 0 disables the check entirely — the pre-guard behaviour, and the
    # documented escape hatch.
    TG_SOCIAL_MAX_MESSAGE_AGE_MIN: int = Field(default=720, ge=0)
    TG_SOCIAL_FLOOD_WAIT_MAX_SEC: int = 600
    TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC: int = 300
    TG_SOCIAL_RESOLUTION_RETRY_DELAY_SEC: int = 60
    TG_SOCIAL_CHANNEL_SILENCE_ALERT_HOURS: int = 72
    TG_SOCIAL_CHANNEL_SILENCE_CHECK_INTERVAL_SEC: int = 3600
    # TG shadow (Stage A, tasks/design_tg_signal_rehabilitation_2026_08_12.md).
    # Counterfactual TG actionability evaluation written to `tg_act_shadow`
    # while the lane stays quarantined — no trading mutation, zero new API
    # calls. Default OFF; activation additionally requires a registered
    # CallerFeatureProvider (Stage B), so flipping this alone arms nothing.
    #
    # The DECISION thresholds below participate in the `gate_version`
    # fingerprint: changing one starts a new generation prospectively rather
    # than silently re-labelling an existing cohort.
    #
    # Three are excluded. `TG_SHADOW_ENABLED` is an activation control — a
    # disable/enable cycle must RESUME the same generation (design §Re-enable
    # semantics), which it cannot do if the flag's own value moves the
    # fingerprint. `TG_SHADOW_LAG_THRESHOLD_MIN` and
    # `TG_SHADOW_SCAN_CADENCE_MIN` are operational watchdog tuning: they
    # change when the operator is paged, never what a decision is, and binding
    # them would discard accumulated evidence to record a monitoring
    # preference.
    TG_SHADOW_ENABLED: bool = False
    TG_SHADOW_LAG_THRESHOLD_MIN: int = 60
    TG_SHADOW_SCAN_CADENCE_MIN: int = 30
    TG_SHADOW_MCAP_MIN_USD: float = 10_000.0
    TG_SHADOW_MCAP_MAX_USD: float = 500_000_000.0
    TG_SHADOW_REQUIRE_LIQUIDITY: bool = False
    TG_SHADOW_MIN_CALLER_COVERAGE: float = 0.5
    TG_CALLER_MIN_ELIGIBLE_CLUSTERS: int = 10
    # Identity classes ('coingecko', 'dex:solana', 'dex:robinhood', ...) held
    # to be structurally unpriceable. EMPTY until the Stage C PQ harness
    # returns a per-stratum verdict — pre-populating it would encode a guess
    # as policy.
    TG_SHADOW_UNPRICEABLE_IDENTITY_CLASSES: list[str] = []
    # BL-062 signal-stacking: require >=N scoring signals for first_signal admission
    FIRST_SIGNAL_MIN_SIGNAL_COUNT: int = 2
    # BL-062 peak-fade early-kill: sustained-fade exit between trail and expiry
    PEAK_FADE_ENABLED: bool = True
    PEAK_FADE_MIN_PEAK_PCT: float = 10.0
    PEAK_FADE_RETRACE_RATIO: float = 0.7
    # BL-NEW-MOMENTUM-DEATH — dry-run-only sub-peak-fade exit lane. The
    # expired-lane backtest (tasks/findings_expired_lane_backtest_2026_07_10.md)
    # found 92/93 expired paper trades peaked BELOW the 10% PEAK_FADE arming
    # floor, so peak_fade was structurally unreachable for them (§9c: the lever
    # exists but the data path never reaches it). This lane catches the
    # [MIN_PEAK_PCT, PEAK_FADE_MIN_PEAK_PCT) band peak_fade cannot reach, reusing
    # the same sustained-fade shape (6h AND 24h checkpoints < RETRACE_RATIO*peak).
    # Ships DRY_RUN first: the backtest's running-peak is checkpoint-proxied, so
    # the dry-run must observe the LIVE running peak before the flip. Soak gate:
    # n>=15 would-fire events + confirm no live runner clipped, then a separate
    # PAPER_MOMENTUM_DEATH_DRY_RUN=False flip PR. Fail-closed: ENABLED default off.
    PAPER_MOMENTUM_DEATH_ENABLED: bool = False
    PAPER_MOMENTUM_DEATH_MIN_PEAK_PCT: float = 5.0
    PAPER_MOMENTUM_DEATH_DRY_RUN: bool = True
    # SIG-04 absolute time-death exit — dry-run-only, sub-leg-1 flat-at-24h lane.
    # W3 analysis-gates verdict (tasks/findings_w3_analysis_gates_2026_07_11.md
    # §SIG-04, clean cohort n=323): trades still FLAT (<=FLAT_PCT) at the 24h
    # checkpoint whose running peak never reached ladder leg 1 are dead capital —
    # exiting them at 24h nets +$1,842 (bootstrap 95% CI [+$1,339,+$2,379],
    # all-positive) vs a false-trigger cost of only 18 winners/-$438. DISTINCT
    # band from momentum_death / peak_fade: those gate on a *sustained fade from a
    # recorded peak*; this gates on *absolute flatness at 24h for a peak that
    # never reached leg 1* (leg_1_filled_at IS NULL AND max(cp_1h,cp_6h,cp_24h) <
    # PAPER_LADDER_LEG_1_PCT AND checkpoint_24h_pct <= FLAT_PCT AND
    # elapsed >= CHECKPOINT_H). Ships DRY_RUN first: the backtest running-peak is
    # checkpoint-proxied, so the dry-run must observe the LIVE peak before the
    # flip. Soak gate: n>=15 would-fire events + 0 live runners clipped, then a
    # separate PAPER_TIME_DEATH_DRY_RUN=False flip PR. Attribution caveat: 78% of
    # whole-book benefit overlaps SIG-05/#434 stop-avoidance — the distinct
    # mandate is the sub-3%-peak cohort only. Fail-closed: ENABLED default off.
    PAPER_TIME_DEATH_ENABLED: bool = False
    PAPER_TIME_DEATH_DRY_RUN: bool = True
    PAPER_TIME_DEATH_FLAT_PCT: float = 0.0
    PAPER_TIME_DEATH_CHECKPOINT_H: int = 24
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
    # SIG-05 stop-loss fill realism (live-gate integrity). When ENABLED, a
    # stop-triggered close books at max(current_price, sl_price*(1 - gap))
    # instead of an arbitrarily-deep crash snapshot that gapped far through
    # the stop between 30-min eval cycles (measured drain: stops filled
    # -28.1% avg on a -10% config). Models a real stop order filling NEAR the
    # stop with a bounded gap allowance (PAPER_STOP_GAP_BPS). The raw observed
    # price is recorded in the exit provenance detail (exit_provenance=
    # 'stop_gap_model' + a stop_fill_slippage_model decision event) so
    # realized-vs-modeled stays auditable. Fail-closed: default off leaves the
    # exact pre-existing fill (book at current_price, provenance 'market').
    PAPER_STOP_FILL_SLIPPAGE_MODEL: bool = False
    PAPER_STOP_GAP_BPS: int = 300  # max 3% gap through the stop on a modeled fill
    # SIG-05 part 2 (near-stop priority refresh) — DEFERRED, follow-up.
    # Intent: when an open trade's unrealized pct is within
    # PAPER_NEAR_STOP_REFRESH_BAND_PCT (~5pp) of its stop, mark it for a
    # priority price refresh so the stop check runs against fresher data.
    # Deferred because (a) a clean integration exceeds ~40 LOC — it needs a
    # near-stop query joining paper_trades×price_cache plus a restructure of
    # fetch_held_position_prices()'s cadence gate, which interacts with the
    # existing in-flight-skip + stale-visibility folds; and (b) it adds no
    # marginal freshness under the current prod config
    # (HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES=1 already refreshes every
    # held position each pipeline cycle) — it only pays off once the operator
    # throttles that interval > 1. Part 1 (the fill model above) is the
    # structural live-gate fix and caps the fill damage regardless of refresh
    # cadence. Revive with a PAPER_NEAR_STOP_REFRESH_BAND_PCT setting when the
    # refresh interval is throttled.
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

    # -------- Cross-venue execution mandate (scout.live.mandate) --------
    # *** THE AUTONOMY GATE FOR THE SIGNAL-DRIVEN LIVE PATH. ***
    # `LiveEngine._dispatch_live` fires from a paper-trade signal with no human in
    # the loop, and until this existed the only things in front of it were the
    # "is live trading wired up" flags. The Solana lane has had a three-lock
    # promotion gate since it shipped; this is the same discipline, venue-neutral,
    # and the Solana lane consults it too.
    #
    # EVERY DEFAULT REFUSES. The allowlists default to EMPTY, which means "nothing
    # is permitted" — never "nothing was restricted, so everything is permitted".
    # A typo therefore closes the gate.
    #
    #   DISABLED            refuses everything. The default.
    #   SIMULATION_ONLY     not an executing mode; refuses like DISABLED.
    #   SUPERVISED_LIVE     execution permitted within the envelope.
    #   BOUNDED_AUTONOMOUS  additionally requires N supervised executions already
    #                       recorded in the ledger for that venue family.
    LIVE_EXECUTION_MANDATE_MODE: Literal[
        "DISABLED",
        "SIMULATION_ONLY",
        "SUPERVISED_LIVE",
        "BOUNDED_AUTONOMOUS",
    ] = "DISABLED"
    # Second lock. The mode alone does not authorize execution, so promoting the
    # path cannot happen by editing one value.
    LIVE_EXECUTION_MANDATE_ENABLED: bool = False
    # CSV allowlists. Empty permits nothing.
    LIVE_EXECUTION_MANDATE_FAMILIES: str = ""
    LIVE_EXECUTION_MANDATE_VENUES: str = ""
    # The bounded envelope. Each must be present, finite and positive before any
    # order is authorized; 0 means "unset", which refuses.
    LIVE_EXECUTION_MANDATE_PER_TRADE_MAX_USD: Decimal = Decimal("0")
    LIVE_EXECUTION_MANDATE_DAILY_MAX_USD: Decimal = Decimal("0")
    LIVE_EXECUTION_MANDATE_MAX_OPEN_POSITIONS: int = 0
    # Completed supervised executions required in the ledger before
    # BOUNDED_AUTONOMOUS runs for a venue family. Counted from solana_executions
    # (dex) / live_trades.mandate_mode (cex), never from a checklist: "we did the
    # supervised trades" is a claim the database settles. Below 1 is refused.
    LIVE_EXECUTION_MANDATE_MIN_SUPERVISED_EXECUTIONS: int = 3

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

    # LIVE-02 interim fail-closed guard. The live close/exit loop
    # (live_evaluator_loop) is the only thing that ever sells a live position;
    # a routing engine that can buy while the closer is off is the buy-only
    # orphan-money state the reconciler exists to prevent. scout/main.py spawns
    # the loop only when this is True; LiveEngine __init__ CRASHES if
    # LIVE_MODE=live AND LIVE_USE_ROUTING_LAYER=True AND this is False
    # (fail-closed — refuse to boot buy-only).
    #
    # DEFAULT FLIPPED True -> False, 2026-08-01. Autonomous selling must be an
    # ACT OF CONFIGURATION, never something you get by omission. The old default
    # meant an absent .env key silently armed the only code path that can sell a
    # real position; the supervised Kraken and Solana lanes are operator-invoked
    # and do not need it. Turning autonomous exits on is now explicit and
    # auditable: set LIVE_CLOSER_ENABLED=True.
    #
    # Consequence, deliberately kept: with LIVE_MODE=live and
    # LIVE_USE_ROUTING_LAYER=True, boot now FAILS unless this is explicitly
    # True. That is the fail-closed guard doing its job — an autonomous buyer
    # must not start without its matching seller.
    LIVE_CLOSER_ENABLED: bool = False

    # Credentials (live mode only; never in .env.example — see spec §4.4)
    BINANCE_API_KEY: SecretStr | None = None
    BINANCE_API_SECRET: SecretStr | None = None

    # -------- Kraken spot pilot (PR-K1, 2026-07-31) --------
    # Credentials for KrakenSpotAdapter. Same posture as BINANCE_* above:
    # live mode only, never in .env.example. The key MUST be issued without
    # "Funds permissions - Withdraw" — KrakenSpotAdapter.preflight_credentials_check
    # fails closed if the withdrawal-scoped probe does anything other than
    # return EGeneral:Permission denied.
    KRAKEN_API_KEY: SecretStr | None = None
    KRAKEN_API_SECRET: SecretStr | None = None
    # Overridable so tests / a staging proxy can retarget the host.
    KRAKEN_API_BASE_URL: str = "https://api.kraken.com"
    KRAKEN_HTTP_TIMEOUT_SEC: float = 10.0
    # PR-K2: how long KrakenSpotAdapter.await_fill_confirmation waits between
    # order-state polls. 2s is a deliberate compromise for a supervised
    # single-order pilot — fast enough that an operator watching a limit order
    # sees the fill promptly, slow enough that a long wait cannot approach
    # Kraken's private-endpoint rate-limit counter.
    KRAKEN_FILL_POLL_INTERVAL_SEC: float = 2.0
    # PR-K2: settle delay between the two sweeps that
    # KrakenSpotAdapter.resolve_order_submission requires before it will
    # report not_accepted. A just-accepted order is briefly visible in
    # neither OpenOrders nor ClosedOrders, and not_accepted is the verdict
    # that tells a caller a resend is safe — so the delay exists to stop a
    # propagation window from being read as a rejection.
    KRAKEN_SUBMISSION_SETTLE_SEC: float = 3.0

    # -------- Kraken supervised pilot envelope (PR-K3, 2026-07-31) --------
    # The mechanical caps around the ONE operator-invoked supervised trade
    # (scout.live.kraken_pilot). Every default is the refusing one: the runner
    # will not place an order until the operator has deliberately set the
    # master gate AND named the single approved pair.
    #
    # These are the pilot lane's caps only. They do NOT gate the signal-driven
    # live engine, which has its own LIVE_* envelope above.
    KRAKEN_PILOT_ENABLED: bool = False
    # Canonical BASE symbol of the ONE approved pair, e.g. "BTC". Empty (the
    # default) means no pair is approved and the runner refuses — a pilot that
    # can trade "whatever was passed on the command line" is not a pilot.
    KRAKEN_PILOT_PAIR: str = ""
    KRAKEN_PILOT_QUOTE: str = "USD"
    KRAKEN_PILOT_MIN_ORDER_USD: float = 10.0
    KRAKEN_PILOT_MAX_ORDER_USD: float = 25.0
    # Cumulative notional ceiling across ALL pilot orders in one UTC day,
    # counted over every non-rejected kraken live_trades row. Backstops the
    # per-order cap against "one more small order" repetition.
    KRAKEN_PILOT_MAX_DAILY_GROSS_USD: float = 100.0
    KRAKEN_PILOT_FILL_TIMEOUT_SEC: float = 120.0
    KRAKEN_PILOT_EVIDENCE_DIR: str = "pilot_evidence"
    # Warn-only: how far the operator's limit price may sit from the current
    # mid before the runner flags it in the approval block. NOT a block — a
    # marketable limit is a legitimate supervised choice, it just has to be a
    # deliberate one.
    KRAKEN_PILOT_PRICE_DEVIATION_WARN_PCT: float = 5.0
    # Headroom added to the required balance so a fee cannot turn a
    # just-affordable order into a venue rejection. Doubles as the tolerance
    # band for the post-trade balance reconciliation.
    KRAKEN_PILOT_FEE_HEADROOM_PCT: float = 1.0

    # -------- Solana execution lane (PR-S1, 2026-07-31) --------
    # Venue-client layer for the supervised SOL->USDC swap
    # (scout.live.solana). Every default is the refusing or the read-only one.
    #
    # *** SOLANA_RPC_URL IS READ-ONLY. ***
    # The lane NEVER broadcasts through it. scout.live.solana.rpc_client
    # deliberately implements no send_transaction method at all — the only
    # submission path is the Jito block engine, and that is a structural
    # guarantee (there is no code to call) rather than a policy one. Reads and
    # simulation through this URL are expected and safe; a public RPC is fine
    # for both. If you are adding a broadcast method here, stop.
    SOLANA_RPC_URL: str = "https://api.mainnet-beta.solana.com"
    # *** THE RESOLVER'S ENDPOINT MUST BE A SINGLE CONSISTENT NODE. ***
    # scout.live.solana.resolver reaches `definitively_not_submitted` — the one
    # verdict that licenses clearing the lane and rebuilding — by combining two
    # facts: the signature is absent, AND the current block height has passed
    # lastValidBlockHeight. Read from a LOAD-BALANCED endpoint those two facts
    # can come from different nodes, and a node that is simultaneously ahead on
    # height and behind on (or missing) the signature status produces a FALSE
    # definitively_not_submitted. The runner would then retire a live row and
    # invite a rerun: two swaps where the operator authorised one.
    #
    # api.mainnet-beta.solana.com is exactly such a round-robin. Point this at
    # a single dedicated node (Helius, Triton, QuickNode, your own validator).
    # Empty means "use SOLANA_RPC_URL", which the runner accepts only when that
    # URL is not itself a known round-robin — see
    # scout.live.solana_pilot.resolver_endpoint.
    SOLANA_RESOLVER_RPC_URL: str = ""
    # The resolver POOL, in preference order. Supersedes the singular key above
    # when non-empty; the singular key is read as a one-element pool otherwise,
    # because that is what is deployed and it stays authoritative rather than
    # becoming a legacy alias nobody remembers to migrate.
    #
    # A second endpoint buys two things the first cannot. READ FAILOVER: a
    # resolution that cannot reach its node returns `unresolved`, which BLOCKS
    # the lane — so a single endpoint being down is an outage of the recovery
    # path, not just of a convenience. CORROBORATION: `definitively_not
    # _submitted` is the one verdict that clears the lane, and it is built out
    # of an ABSENCE. A second endpoint is asked to see the same absence before
    # it is acted on, and disagreement collapses the verdict to `unresolved`.
    #
    # Empty is fully supported and is what ships first: one endpoint, no
    # corroboration, and the evidence records that the verdict was
    # uncorroborated so a single-node reading is never mistaken for two.
    #
    # Every endpoint must be a single dedicated node (the round-robin refusal
    # applies to all of them) and must prove it is on mainnet-beta via
    # getGenesisHash before it is used — see scout.live.solana.resolver_pool.
    SOLANA_RESOLVER_RPC_URLS: Annotated[list[str], NoDecode] = []
    # Per-endpoint budget for the genesis + health probes that admit an
    # endpoint to the pool. Deliberately much shorter than
    # SOLANA_HTTP_TIMEOUT_SEC: this is a liveness question, and an endpoint
    # that needs 15s to say "ok" has already answered it.
    SOLANA_RESOLVER_HEALTH_TIMEOUT_SEC: float = 5.0
    # Above this, an endpoint is DEGRADED: still on the right chain and still
    # caught up, so it stays usable as a fallback, but it is demoted behind the
    # faster endpoints. Excluding it instead would trade a slow resolver for no
    # resolver, which is the worse failure — an unresolvable signature blocks
    # the lane.
    SOLANA_RESOLVER_MAX_LATENCY_MS: float = 2_000.0
    # Age-derived blockhash expiry, bounded on BOTH sides.
    #
    # A Solana blockhash is valid for at most 150 slots (~60-90s), so a
    # sufficiently old ledger row has provably expired whatever height was
    # recorded for it — which lets a row resolve from `created_at` alone when
    # its evidence file is gone. Below MIN, age proves nothing and the runner
    # falls back to the evidence file's lastValidBlockHeight.
    #
    # The MAX bound is the one that keeps this safe. `getSignatureStatuses`
    # with searchTransactionHistory only reaches as far back as the node
    # retains ledger history; past that, a LANDED transaction reads as absent.
    # Absent plus provably-expired would then auto-retire a row whose swap
    # actually executed — the single outcome the resolver exists to prevent.
    # So beyond MAX the verdict is forced to `unresolved` and the row waits for
    # a human. Lower MAX if your RPC provider's history window is shorter.
    SOLANA_RESOLVER_AGE_EXPIRY_MIN_SEC: float = 3600.0
    SOLANA_RESOLVER_AGE_EXPIRY_MAX_SEC: float = 86400.0
    # Path to a Solana CLI id.json (a 64-byte secret-key array). The RUNNER
    # loads it at call time; the key itself is NEVER config, never an env var,
    # never in argv. scout.live.solana.signer refuses the file unless its mode
    # is 0600 and its owner is the current uid.
    SOLANA_PILOT_KEYPAIR_PATH: str = ""
    # The wallet the pilot trades from, declared as a PUBLIC key.
    #
    # Everything before the approval prompt needs this — Jupiter builds for it,
    # tx_inspector checks the fee payer against it, the balance gate reads it —
    # and none of that may open the funded key file, because the funded key
    # must not be touched until the operator has authorized. A public key is
    # public, so declaring it in config costs nothing and buys the ordering.
    #
    # It is also a second lock: at signing time the loaded keypair must equal
    # this value or `signer.sign_transaction` refuses, so a swapped or
    # mistakenly-pointed key file cannot sign a transaction built for another
    # wallet.
    SOLANA_PILOT_SIGNER_PUBKEY: str = ""
    # Jupiter Swap API. Overridable so tests / a staging proxy can retarget it.
    JUPITER_API_BASE: str = "https://api.jup.ag/swap/v1"
    # Optional. Jupiter serves a keyless tier at 0.5 req/sec; a free portal key
    # raises that and is sent as the `x-api-key` header when set.
    JUPITER_API_KEY: SecretStr | None = None
    JITO_BLOCK_ENGINE_URL: str = "https://mainnet.block-engine.jito.wtf"
    # Revert protection vs. landing probability. An honest trade-off, and the
    # default is the one that LANDS.
    #
    # True  — `bundleOnly=true`. The transaction is submitted as a bundle with
    #         revert protection: if it would fail, it is not included and no
    #         fee is paid. Jito's own docs warn this "may reduce landing
    #         probability since the transaction must win the block-engine
    #         auction rather than having fallback routing options." There is NO
    #         fallback path: the bundle wins its auction or nothing happens.
    # False — normal routing. The transaction can land through Jito's regular
    #         paths as well as the auction. The cost is that a transaction
    #         which WOULD revert now lands and burns the ~5,000-lamport base
    #         fee instead of being silently dropped.
    #
    # False is right for this lane because the revert risk bundleOnly was
    # buying is already covered twice over: Jupiter simulates its own build,
    # and the lane runs an INDEPENDENT pre-sign simulation against current
    # chain state and refuses on any error. Paying for the same protection a
    # third time — in landing probability — buys nothing.
    #
    # Two live mainnet attempts (2026-08) were acknowledged by Jito with the
    # signature returned and never landed, at tips of 100,000 and 500,000
    # lamports against an observed P95 of 370,000. Both had bundleOnly on.
    #
    # This does NOT widen the broadcast surface: Jito remains the only path,
    # and rpc_client is still structurally incapable of sending.
    SOLANA_JITO_BUNDLE_ONLY: bool = False
    SOLANA_HTTP_TIMEOUT_SEC: float = 15.0

    # Ceilings enforced by tx_inspector against the transaction Jupiter built.
    # These are not requests to Jupiter — they are the limits above which we
    # refuse to SIGN what came back, which is what makes them meaningful: a
    # remotely-built transaction is only as safe as the checks we run on it.
    SOLANA_PILOT_SLIPPAGE_BPS: int = 100
    SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS: int = 1_000_000
    SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS: int = 1_000_000
    # Ceiling on base signature fee + priority fee + Jito tip + ATA RENT
    # combined. Caps every lamport the transaction moves out of the wallet
    # independently of how it is split, so a build that respects each
    # component ceiling but stacks them still fails.
    #
    # 8_500_000 covers the worst case the inspector can price — 3 ATA creates
    # at the maximum component ceilings: 3 x 2,039,280 + priority 1,000,000 +
    # tip 1,000,000 + base 5,000 = 8,122,840 — with headroom. That is ~0.0085
    # SOL (~$1.30 at $150/SOL), trivial absolute exposure against a $5-10
    # trade, and loosening the aggregate loosens no individual lever: the
    # priority and tip ceilings still bind on their own.
    #
    # Most of the rent comes back. It is a rent-exempt DEPOSIT rather than a
    # fee, and the temporary wSOL account Jupiter opens is closed to the owner
    # at the end of the swap — but the lamports must be available while the
    # transaction runs, so the ceiling and the balance gate both count them.
    SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS: int = 8_500_000
    # How many associated-token-account creations a single build may pay for.
    # Used by the config floor below; a first-ever SOL->USDC swap plausibly
    # creates two (wSOL + USDC), and 3 leaves room for a route that opens an
    # intermediate.
    SOLANA_PILOT_MAX_ATA_CREATES: int = 3
    SOLANA_PILOT_MIN_ORDER_USD: float = 5.0
    SOLANA_PILOT_MAX_ORDER_USD: float = 10.0

    # -------- Solana lane limits engine --------
    # The rest of the execution envelope, enforced by
    # scout.live.solana.limits.LimitsEngine alongside the per-trade band and
    # the fee ceilings above. BOUNDED_AUTONOMOUS inherits every one of these
    # unchanged — the engine never sees SOLANA_MODE.
    #
    # Cap on AUTHORIZED notional per UTC day, summed over the lane's
    # live_trades rows. Authorized rather than realised: a cap that only
    # counted settled trades would let a burst of in-flight ones straight
    # through, which is the exact shape of a runaway.
    SOLANA_MAX_DAILY_NOTIONAL_USD: float = 25.0
    # Positions the lane may hold at once. 1 makes the supervised lane
    # strictly one-trade-at-a-time; raising it is the first real autonomy
    # decision and is deliberately a separate value from the concurrency
    # limit below, because holding two positions and having two transactions
    # in flight are different risks.
    SOLANA_MAX_OPEN_POSITIONS: int = 1
    # Executions that may be in flight simultaneously. Belt to the lane lock's
    # braces: the lock stops two PROCESSES, this stops one process from
    # starting a second execution while an earlier one is unresolved.
    SOLANA_MAX_CONCURRENT_EXECUTIONS: int = 1
    # How stale a quote may be at SUBMISSION time. Checked after the approval
    # prompt, which is unbounded operator time: the price that was authorized
    # is not necessarily the price that will execute, and the on-chain
    # minimum-output bound protects the trade but not the operator's intent.
    SOLANA_MAX_QUOTE_AGE_SEC: float = 120.0
    # Headroom over the full required balance (swap + every lamport the bytes
    # charge). Was an inline constant; a fee market that moves between the
    # build and the block must not turn a just-affordable swap into an
    # on-chain failure after the operator has authorized it.
    SOLANA_BALANCE_HEADROOM_PCT: float = 10.0
    # Mint allowlists. The lane is SOL -> USDC today; these are what make a
    # second pair a configuration change rather than a code change. Defaults
    # are wrapped SOL and mainnet USDC — the same addresses as
    # scout.live.solana.constants, inlined here to keep config free of a
    # scout.live dependency (that module remains the source of truth).
    SOLANA_ALLOWED_INPUT_MINTS: Annotated[list[str], NoDecode] = [
        "So11111111111111111111111111111111111111112"
    ]
    SOLANA_ALLOWED_OUTPUT_MINTS: Annotated[list[str], NoDecode] = [
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    ]
    # AMM labels the route may use, as Jupiter reports them ("Raydium",
    # "Orca", ...). EMPTY MEANS UNRESTRICTED, and the check says so in its
    # detail rather than implying a restriction that is not configured:
    # Jupiter routes through dozens of AMMs and rotates which it picks, so a
    # closed default would refuse essentially every real route.
    SOLANA_ALLOWED_ROUTE_LABELS: Annotated[list[str], NoDecode] = []

    # -------- Solana lane operations --------
    # Stuck-execution watchdog (CLAUDE.md §12a, in the shape that fits this
    # table). A row-rate SLO is the WRONG shape for solana_executions: it is
    # only written when a trade runs, so silence is normal and says nothing.
    # The analogue is STUCK STATE — a row that entered a non-terminal state and
    # has not moved since — and the thresholds differ per state because the
    # states mean different things.
    SOLANA_EXECUTION_WATCHDOG_ENABLED: bool = True
    # A human reading the decision screen. Generous on purpose.
    SOLANA_STUCK_AWAITING_AUTHORIZATION_SEC: float = 3600.0
    # Machine steps that take seconds. Sitting here means the process died
    # between two durable writes.
    SOLANA_STUCK_PRE_SUBMISSION_SEC: float = 900.0
    # *** THE ONE THAT MUST NOT SIT. *** A row still in submission_attempted
    # means a transaction may exist and nobody is asking the cluster about it.
    SOLANA_STUCK_SUBMISSION_SEC: float = 180.0
    # Landed, waiting on confirmation / finalization / reconciliation.
    SOLANA_STUCK_POST_SUBMISSION_SEC: float = 900.0
    # Already escalated and already blocking the lane. Alerted anyway, on a
    # longer fuse: a blocked lane nobody remembers is a lane that has silently
    # stopped trading.
    SOLANA_STUCK_UNKNOWN_SUBMISSION_SEC: float = 1800.0

    # -------- Bounded-autonomy preconditions --------
    # Completed, RECONCILED supervised executions required in the ledger before
    # BOUNDED_AUTONOMOUS will run. Counted from solana_executions, not from
    # memory or a checklist: the transition has to be impossible by flipping a
    # flag, and "we did the supervised trades" is a claim the database can
    # settle. Raising this is a tightening; lowering it below 1 is refused.
    SOLANA_AUTONOMY_MIN_SUPERVISED_EXECUTIONS: int = 3
    # Settle delay between the two sweeps that resolve an ambiguous
    # submission. Same rationale as KRAKEN_SUBMISSION_SETTLE_SEC above: a
    # just-forwarded transaction is briefly unknown to the RPC's signature
    # cache, and "definitively not submitted" is the verdict that tells an
    # operator a rebuild is safe — so a propagation window must never be read
    # as an absence.
    SOLANA_SUBMISSION_SETTLE_SEC: float = 5.0

    # -------- Solana DEX execution lane --------
    # The mechanical envelope around scout.live.solana_lane, the PERMANENT
    # Solana execution path. Every default is the refusing one, and none of
    # these gate the signal-driven live engine.
    #
    # *** SOLANA_MODE IS THE LANE'S MASTER CONTROL. ***
    # It replaces the old SOLANA_PILOT_ENABLED boolean, which could only say
    # on/off and had no way to express "rehearse" or "stop everything". The
    # five modes are the whole spectrum the lane will ever operate in, and
    # moving between them is CONFIGURATION — there is no second runner, no
    # second adapter and no second code path behind any of them.
    #
    #   DISABLED            refuses everything. The default.
    #   SIMULATION_ONLY     quotes, builds, inspects, simulates and prompts.
    #                       Never reads the funded key, never submits.
    #   SUPERVISED_LIVE     a human types the authorization before the funded
    #                       key signs. The boundary, unchanged.
    #   BOUNDED_AUTONOMOUS  a policy check substitutes for the typed prompt.
    #                       Requires SOLANA_BOUNDED_AUTONOMOUS_ENABLED as well,
    #                       so the mode alone cannot start it.
    #   EMERGENCY_STOPPED   refuses all execution, like the kill switch, and is
    #                       checked in the same places.
    #
    # The ONLY difference between SUPERVISED_LIVE and BOUNDED_AUTONOMOUS is
    # which authorization policy is asked. Same limits, same signer, same
    # state machine, same submission, same reconciliation, same evidence.
    SOLANA_MODE: Literal[
        "DISABLED",
        "SIMULATION_ONLY",
        "SUPERVISED_LIVE",
        "BOUNDED_AUTONOMOUS",
        "EMERGENCY_STOPPED",
    ] = "DISABLED"
    # Second lock on autonomy. BOUNDED_AUTONOMOUS additionally requires this,
    # so promoting the lane cannot happen by editing one value — and the
    # preconditions in the lane itself (recorded supervised executions,
    # configured limits) are checked on top of both.
    SOLANA_BOUNDED_AUTONOMOUS_ENABLED: bool = False
    # Tip REQUESTED from Jupiter via prioritizationFeeLamports.jitoTipLamports.
    # A request, not a guarantee: tx_inspector re-derives the tip actually
    # compiled into the transaction and enforces
    # SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS against that. Jito's documented
    # minimum is 1000 lamports and warns it "might not be sufficient" under
    # load; 100_000 buys a realistic chance of landing on a $7 swap while
    # staying two orders of magnitude under the ceiling.
    SOLANA_PILOT_JITO_TIP_LAMPORTS: int = 100_000
    # Ceiling on the quote's own priceImpactPct, expressed in PERCENT.
    # Jupiter reports the field as a decimal FRACTION ("0.0025" = 0.25%), so
    # the runner multiplies by 100 before comparing — see
    # scout.live.solana_pilot._quote_price_impact_pct.
    SOLANA_PILOT_MAX_PRICE_IMPACT_PCT: float = 1.0
    # Safety margin, in blocks, subtracted from lastValidBlockHeight when the
    # runner re-checks blockhash validity immediately after the operator
    # authorizes. A transaction whose blockhash expires between authorization
    # and landing is not dangerous, but it burns the authorization: the margin
    # means the runner refuses a build that is about to expire rather than
    # submitting one that almost certainly cannot land.
    SOLANA_PILOT_BLOCKHASH_SAFETY_MARGIN_BLOCKS: int = 15
    SOLANA_PILOT_EVIDENCE_DIR: str = "pilot_evidence"
    SOLANA_PILOT_CONFIRM_TIMEOUT_SEC: float = 90.0
    SOLANA_PILOT_FINALIZE_TIMEOUT_SEC: float = 90.0
    # Poll interval for the confirm / finalize waits. Mirrors
    # KRAKEN_FILL_POLL_INTERVAL_SEC; kept separate from
    # SOLANA_SUBMISSION_SETTLE_SEC because that value is the resolver's
    # between-sweeps delay and the two are tuned against different questions.
    SOLANA_PILOT_POLL_INTERVAL_SEC: float = 2.0
    # *** USDC ACCOUNT CLOSURE IS OFF, AND OFF IS THE ONLY DEFAULT. ***
    # A reverse (USDC->SOL) swap empties the wallet's USDC account, at which
    # point closing it would release its ~2,039,280 lamports of rent. That is a
    # SEPARATE decision from the swap and it is never bundled into one: with
    # this False, `solana_lane reverse` refuses any built transaction that
    # closes the USDC account, so "it did not happen quietly" is enforced
    # rather than assumed.
    #
    # Turning it on takes TWO deliberate acts — this setting AND the
    # --close-usdc-ata flag on the command — and the approval screen then shows
    # the closure and the exact rent on their own line. Leaving the account
    # open costs the rent and keeps the wallet ready to receive USDC again.
    SOLANA_REVERSE_CLOSE_USDC_ATA: bool = False

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
    # ALR-04 weekly alerts scoreboard (sent-alert -> paper-trade outcome loop).
    # Read-only; sent on the same weekly tick as the digest (WEEKDAY/_HOUR above).
    # Default OFF — operator opts in via .env once they want the weekly send.
    WEEKLY_ALERTS_SCOREBOARD_ENABLED: bool = False
    WEEKLY_ALERTS_SCOREBOARD_WINDOW_DAYS: int = 7
    # n-gate: below this many sent alerts resolving to a paper trade, the derived
    # stats are withheld behind an INSUFFICIENT_DATA line.
    WEEKLY_ALERTS_SCOREBOARD_MIN_LINKED: int = 5
    # Refresh-eligibility window (days). combo_refresh.refresh_all refreshes
    # every combo that had a trade opened within this window. It ALSO refreshes
    # currently-suppressed combos with no trade in the window (fix/frozen-
    # suppression-lock) so a suppressed signal cannot silently fall out of the
    # refresh set and latch at parole_exhausted forever. The same threshold
    # defines "older than the refresh window" for the permanent-suppression
    # §12b operator alert. Kept as a Setting so no hardcoded 30 leaks into the
    # query (What NOT To Do: no hardcoded thresholds).
    FEEDBACK_REFRESH_WINDOW_DAYS: int = 30

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
    # NoDecode: see CHAINS above. Without it, the perp-spec documented comma
    # form (`PERP_SYMBOLS=BTCUSDT,ETHUSDT`) json.loads-crashes at construction
    # before parse_perp_symbols runs. (INF-01.)
    PERP_SYMBOLS: Annotated[list[str], NoDecode] = []
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

    @computed_field
    @property
    def paper_trust_size_multipliers_map(self) -> dict[str, float]:
        """Parse PAPER_TRUST_SIZE_MULTIPLIERS CSV of tier=multiplier pairs.

        Keys are trust tiers (trusted / experimental / non_tradable), values
        are non-negative size multipliers applied to PAPER_TRADE_AMOUNT_USD
        (SIG-10). Mirrors live_signal_sizes_map parsing; raises ValueError on
        any malformed entry (missing '=' , empty key/value, or negative float).
        """
        if not self.PAPER_TRUST_SIZE_MULTIPLIERS:
            return {}
        out: dict[str, float] = {}
        for pair in self.PAPER_TRUST_SIZE_MULTIPLIERS.split(","):
            pair = pair.strip()
            if not pair:
                continue
            k, sep, v = pair.partition("=")
            k = k.strip().lower()
            if not sep or not k or not v.strip():
                raise ValueError(
                    f"PAPER_TRUST_SIZE_MULTIPLIERS malformed entry: {pair!r}"
                )
            mult = float(v.strip())
            if mult < 0:
                raise ValueError(
                    f"PAPER_TRUST_SIZE_MULTIPLIERS multiplier must be >= 0: {pair!r}"
                )
            out[k] = mult
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

    @field_validator("COINGECKO_API_TIER")
    @classmethod
    def _validate_cg_api_tier(cls, v: str) -> str:
        from scout.cg_api import VALID_TIERS

        if v not in VALID_TIERS:
            raise ValueError(
                f"COINGECKO_API_TIER must be one of {VALID_TIERS}, got {v!r}"
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

    @field_validator("PAPER_MOMENTUM_DEATH_MIN_PEAK_PCT")
    @classmethod
    def _validate_momentum_death_min_peak_pct(cls, v: float) -> float:
        # >0 only — intentionally NOT hard-coupled to PEAK_FADE_MIN_PEAK_PCT so
        # operators can sweep the floor during the dry-run soak. The band guard
        # (peak_pct < PEAK_FADE_MIN_PEAK_PCT) lives in the evaluator, not here.
        if v <= 0:
            raise ValueError(f"PAPER_MOMENTUM_DEATH_MIN_PEAK_PCT must be > 0; got={v}")
        return v

    @field_validator("PAPER_TIME_DEATH_CHECKPOINT_H")
    @classmethod
    def _validate_time_death_checkpoint_h(cls, v: int) -> int:
        # Positive number of hours only — a zero/negative checkpoint is
        # meaningless (the lane measures "still flat this many hours in").
        if v <= 0:
            raise ValueError(f"PAPER_TIME_DEATH_CHECKPOINT_H must be > 0; got={v}")
        return v

    @field_validator("PAPER_TIME_DEATH_FLAT_PCT")
    @classmethod
    def _validate_time_death_flat_pct(cls, v: float) -> float:
        # Sane percentage-change band. The backtested operating point is
        # FLAT<=0 and the soak may sweep it toward 0..3, so this is
        # intentionally NOT hard-coupled to any other threshold — it only
        # rejects absurd values (a pct change cannot sit below -100%, and a
        # "flat" ceiling at/above +100% is not flatness). The sub-leg-1 guard
        # (max(cp) < PAPER_LADDER_LEG_1_PCT) lives in the evaluator, not here.
        if not (-100.0 < v < 100.0):
            raise ValueError(
                f"PAPER_TIME_DEATH_FLAT_PCT must be in (-100, 100); got={v}"
            )
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

    @field_validator("DASHBOARD_BASE_URL")
    @classmethod
    def _validate_dashboard_base_url(cls, v: str) -> str:
        """Empty string is allowed (deep-link line disabled). Non-empty
        values must be `http(s)://` URLs so a schemeless misconfig produces
        a broken link fail-fast at construction rather than in every alert.
        """
        if v == "":
            return v
        if not v.startswith("http://") and not v.startswith("https://"):
            raise ValueError(
                "DASHBOARD_BASE_URL must be empty or start with http:// or "
                f"https://; got={v!r}"
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
    def _validate_cg_operational_floor_and_tg_ceiling(self) -> "Settings":
        """The TG ceiling must be ENFORCEABLE by the floor, not a parallel number.

        Discretionary OPERATIONAL traffic is stopped at
        ``operational_cap - fixed_floor``. If the declared TG ceiling exceeded
        that, the table would claim a bound the code does not enforce -- exactly
        the "620/month estimate presented as a bound" defect. If the floor were
        smaller than the fixed duties it protects, it would protect nothing.
        """
        cap = self.COINGECKO_MONTHLY_OPERATIONAL_CREDITS
        floor = self.COINGECKO_OPERATIONAL_FIXED_FLOOR_CREDITS
        if cap <= 0:
            return self
        if floor > cap:
            raise ValueError(
                f"COINGECKO_OPERATIONAL_FIXED_FLOOR_CREDITS={floor} exceeds "
                f"COINGECKO_MONTHLY_OPERATIONAL_CREDITS={cap}"
            )
        discretionary = cap - floor
        if self.COINGECKO_TG_RESOLVER_MONTHLY_CREDITS > discretionary:
            raise ValueError(
                f"COINGECKO_TG_RESOLVER_MONTHLY_CREDITS="
                f"{self.COINGECKO_TG_RESOLVER_MONTHLY_CREDITS} exceeds the "
                f"{discretionary} of discretionary operational capacity that the "
                f"reserved floor actually leaves. A ceiling the code cannot "
                f"enforce is an estimate wearing a bound's clothes."
            )
        return self

    @model_validator(mode="after")
    def _validate_cg_monthly_envelopes_fit_the_allowance(self) -> "Settings":
        """Envelope sum must not exceed the plan's monthly credit allowance.

        Pinned as a validator because over-subscription is SILENT: each envelope
        looks reasonable alone, discovery stops at its own cap, and the plan
        still runs dry — which is exactly the shape of the 2026-08-21 incident,
        one axis un-modelled while every visible number looked fine.
        """
        allowance = self.COINGECKO_MONTHLY_CREDIT_ALLOWANCE
        if allowance <= 0:
            return self
        total = (
            self.COINGECKO_MONTHLY_DISCOVERY_CREDITS
            + self.COINGECKO_MONTHLY_CRITICAL_CREDITS
            + self.COINGECKO_MONTHLY_OPERATIONAL_CREDITS
        )
        if total > allowance:
            raise ValueError(
                f"CoinGecko monthly envelopes sum to {total} which exceeds "
                f"COINGECKO_MONTHLY_CREDIT_ALLOWANCE={allowance}. Over-subscribing "
                f"the allowance means discovery can stop at its own cap while the "
                f"plan still runs dry."
            )
        return self

    @model_validator(mode="after")
    def _validate_volume_history_cg_covers_label_lateness(self) -> "Settings":
        """volume_history_cg must outlive the r7d window PLUS labeling lateness.

        The ledger labeler computes peak7d as MAX(price) over the closed window
        [emitted, emitted + 7d] (outcome_ledger._peak_price_in_window), so the
        left-edge row is exactly LEDGER_R7D_HORIZON_DAYS old at the earliest
        finalizable instant. Retention equal to the horizon leaves zero margin,
        and lateness then truncates the window from the left — yielding a
        wrong-but-plausible peak7d instead of a NULL.

        This is pinned as a validator rather than left to the field default
        because the failure is SILENT: a retention of 7 produces labels, just
        biased ones. Nothing downstream would raise.
        """
        required = LEDGER_R7D_HORIZON_DAYS + self.LEDGER_LABEL_MAX_LATENESS_DAYS
        if self.VOLUME_HISTORY_CG_RETENTION_DAYS < required:
            raise ValueError(
                f"VOLUME_HISTORY_CG_RETENTION_DAYS="
                f"{self.VOLUME_HISTORY_CG_RETENTION_DAYS} must be >= {required} "
                f"(= {LEDGER_R7D_HORIZON_DAYS}d r7d/peak7d horizon + "
                f"{self.LEDGER_LABEL_MAX_LATENESS_DAYS}d "
                f"LEDGER_LABEL_MAX_LATENESS_DAYS). Below this the ledger's "
                f"peak7d window is truncated from the left and labels are "
                f"silently biased low rather than left NULL."
            )
        return self

    # Ruling B pinned this at 14 as a HARD floor -- "never below 14 without a
    # new consumer study." The secondwave validator below is necessary but NOT
    # sufficient for that: it compares against SECONDWAVE_COOLDOWN_MAX_DAYS,
    # so lowering that knob would silently drag the permitted retention floor
    # down with it and re-open the deletion the ruling bounded. Pin the
    # absolute floor separately from the relative one.
    VOLUME_SNAPSHOTS_RETENTION_FLOOR_DAYS: int = 14

    @model_validator(mode="after")
    def _validate_chain_retention_covers_prospective_lookback(self) -> "Settings":
        """Ruling F residual: the ONE consumer left reading signal_events.

        `scout/conviction/prospective.py` derives its chains surface with
        `MIN(created_at) ... WHERE created_at >= now - LOOKBACK_DAYS`. It is
        window-bounded, so it is exempt from the first-seen migration -- but
        only while retention actually covers that window. Today the two knobs
        merely COINCIDE at 14; nothing enforced it, so the first cut of
        CHAIN_EVENT_RETENTION_DAYS below the lookback would silently truncate
        it, with no error, which is exactly the harm ruling F exists to remove.

        Making the coincidence an invariant is what lets that consumer stay
        un-migrated honestly.
        """
        if self.CHAIN_EVENT_RETENTION_DAYS < self.CONVICTION_PROSPECTIVE_LOOKBACK_DAYS:
            raise ValueError(
                f"CHAIN_EVENT_RETENTION_DAYS={self.CHAIN_EVENT_RETENTION_DAYS} "
                f"must be >= CONVICTION_PROSPECTIVE_LOOKBACK_DAYS="
                f"{self.CONVICTION_PROSPECTIVE_LOOKBACK_DAYS}. The prospective "
                f"watchlist derives first-seen from signal_events over that "
                f"lookback; lower retention truncates it silently."
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

    # Ordered AFTER the relative check on purpose: when both are violated the
    # secondwave message names the actual consumer and is the more actionable
    # one, so this floor only speaks in the case the relative check CANNOT
    # catch -- someone lowering SECONDWAVE_COOLDOWN_MAX_DAYS, which would
    # otherwise drag the permitted retention floor down with it.
    @model_validator(mode="after")
    def _validate_volume_snapshots_retention_floor(self) -> "Settings":
        if (
            self.VOLUME_SNAPSHOTS_RETENTION_DAYS
            < self.VOLUME_SNAPSHOTS_RETENTION_FLOOR_DAYS
        ):
            raise ValueError(
                f"VOLUME_SNAPSHOTS_RETENTION_DAYS="
                f"{self.VOLUME_SNAPSHOTS_RETENTION_DAYS} is below the ruling-B "
                f"hard floor of {self.VOLUME_SNAPSHOTS_RETENTION_FLOOR_DAYS} "
                f"days. Going lower requires a new consumer study: the readers "
                f"are bounded at 7d and SECONDWAVE_COOLDOWN_MAX_DAYS, and "
                f"deleted snapshots cannot be restored by raising the value "
                f"back."
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

    @model_validator(mode="after")
    def _validate_kraken_pilot_caps(self) -> "Settings":
        """PR-K3: the pilot's three notional caps must nest.

        MIN <= MAX <= MAX_DAILY_GROSS. An inverted pair is a silent
        no-trade footgun in the same shape as ``_validate_live_caps_relation``
        above: MIN > MAX rejects every order the operator can size, and
        MAX > MAX_DAILY_GROSS means the very first order breaches the daily
        ceiling. Both fail at config load rather than at 06:00 on trade day.
        """
        if self.KRAKEN_PILOT_MIN_ORDER_USD > self.KRAKEN_PILOT_MAX_ORDER_USD:
            raise ValueError(
                "KRAKEN_PILOT_MIN_ORDER_USD must be <= KRAKEN_PILOT_MAX_ORDER_USD; "
                f"got min={self.KRAKEN_PILOT_MIN_ORDER_USD}, "
                f"max={self.KRAKEN_PILOT_MAX_ORDER_USD}. No order size satisfies "
                "both bounds."
            )
        if self.KRAKEN_PILOT_MAX_ORDER_USD > self.KRAKEN_PILOT_MAX_DAILY_GROSS_USD:
            raise ValueError(
                "KRAKEN_PILOT_MAX_ORDER_USD must be <= "
                "KRAKEN_PILOT_MAX_DAILY_GROSS_USD; got "
                f"max={self.KRAKEN_PILOT_MAX_ORDER_USD}, "
                f"daily_gross={self.KRAKEN_PILOT_MAX_DAILY_GROSS_USD}. A single "
                "max-sized order would breach the daily ceiling."
            )
        return self

    @model_validator(mode="after")
    def _validate_solana_pilot_caps(self) -> "Settings":
        """PR-S1: the Solana lane's notional and fee ceilings must be coherent.

        Same shape as ``_validate_kraken_pilot_caps`` above. The fee clause is
        the one that matters most: if the combined ceiling is below what the
        component ceilings plus the protocol's own base signature fee can
        produce, ``tx_inspector`` rejects every transaction Jupiter builds and
        the failure shows up as "the lane never trades" on pilot day rather
        than as a config error now.
        """
        if self.SOLANA_PILOT_MIN_ORDER_USD > self.SOLANA_PILOT_MAX_ORDER_USD:
            raise ValueError(
                "SOLANA_PILOT_MIN_ORDER_USD must be <= SOLANA_PILOT_MAX_ORDER_USD; "
                f"got min={self.SOLANA_PILOT_MIN_ORDER_USD}, "
                f"max={self.SOLANA_PILOT_MAX_ORDER_USD}. No order size satisfies "
                "both bounds."
            )
        if not 0 < self.SOLANA_PILOT_SLIPPAGE_BPS <= 10_000:
            raise ValueError(
                "SOLANA_PILOT_SLIPPAGE_BPS must be in (0, 10000]; "
                f"got={self.SOLANA_PILOT_SLIPPAGE_BPS}. Zero slippage cannot "
                "route and >100% is not a bound."
            )
        for name in (
            "SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS",
            "SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS",
            "SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0; got={getattr(self, name)}")
        for name in (
            "SOLANA_RESOLVER_AGE_EXPIRY_MIN_SEC",
            "SOLANA_RESOLVER_AGE_EXPIRY_MAX_SEC",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0; got={getattr(self, name)}")
        if (
            self.SOLANA_RESOLVER_AGE_EXPIRY_MIN_SEC
            >= self.SOLANA_RESOLVER_AGE_EXPIRY_MAX_SEC
        ):
            raise ValueError(
                "SOLANA_RESOLVER_AGE_EXPIRY_MIN_SEC must be < "
                "SOLANA_RESOLVER_AGE_EXPIRY_MAX_SEC; got "
                f"min={self.SOLANA_RESOLVER_AGE_EXPIRY_MIN_SEC}, "
                f"max={self.SOLANA_RESOLVER_AGE_EXPIRY_MAX_SEC}. An empty or "
                "inverted window means age can never establish expiry, so a row "
                "whose evidence file is gone could never self-resolve."
            )
        for name in (
            "SOLANA_RESOLVER_HEALTH_TIMEOUT_SEC",
            "SOLANA_RESOLVER_MAX_LATENCY_MS",
        ):
            if getattr(self, name) <= 0:
                # Zero is not "no limit" for either: a zero timeout admits no
                # endpoint and a zero latency budget marks every endpoint
                # degraded, so both spellings of "off" silently break the pool.
                raise ValueError(f"{name} must be > 0; got={getattr(self, name)}")
        # A daily cap below one trade's maximum admits no trade at all, which
        # is a lane that refuses everything for a reason no message explains.
        if self.SOLANA_MAX_DAILY_NOTIONAL_USD < self.SOLANA_PILOT_MAX_ORDER_USD:
            raise ValueError(
                "SOLANA_MAX_DAILY_NOTIONAL_USD must be >= "
                "SOLANA_PILOT_MAX_ORDER_USD; got "
                f"daily={self.SOLANA_MAX_DAILY_NOTIONAL_USD}, "
                f"per-trade max={self.SOLANA_PILOT_MAX_ORDER_USD}. A daily cap "
                "below one trade's ceiling refuses every trade."
            )
        for name in ("SOLANA_MAX_OPEN_POSITIONS", "SOLANA_MAX_CONCURRENT_EXECUTIONS"):
            if getattr(self, name) < 1:
                # Zero is not "unlimited" — it is a lane that never trades, and
                # DISABLED is how you say that.
                raise ValueError(
                    f"{name} must be >= 1; got={getattr(self, name)}. Use "
                    "SOLANA_MODE=DISABLED to stop the lane."
                )
        if self.SOLANA_MAX_QUOTE_AGE_SEC <= 0:
            raise ValueError(
                "SOLANA_MAX_QUOTE_AGE_SEC must be > 0; got "
                f"{self.SOLANA_MAX_QUOTE_AGE_SEC}. Zero rejects every quote, "
                "including the one fetched a moment ago."
            )
        if self.SOLANA_BALANCE_HEADROOM_PCT < 0:
            raise ValueError(
                "SOLANA_BALANCE_HEADROOM_PCT must be >= 0; got "
                f"{self.SOLANA_BALANCE_HEADROOM_PCT}"
            )
        for name in ("SOLANA_ALLOWED_INPUT_MINTS", "SOLANA_ALLOWED_OUTPUT_MINTS"):
            if not getattr(self, name):
                # Unlike the route allowlist, an EMPTY mint allowlist is not
                # "unrestricted" — it is a lane with no permitted pair, and
                # reading it as unrestricted would turn a config typo into an
                # any-token lane.
                raise ValueError(
                    f"{name} must name at least one mint; an empty mint "
                    "allowlist is not 'unrestricted', it is a lane with no "
                    "tradable pair."
                )
        for name in (
            "SOLANA_STUCK_AWAITING_AUTHORIZATION_SEC",
            "SOLANA_STUCK_PRE_SUBMISSION_SEC",
            "SOLANA_STUCK_SUBMISSION_SEC",
            "SOLANA_STUCK_POST_SUBMISSION_SEC",
            "SOLANA_STUCK_UNKNOWN_SUBMISSION_SEC",
        ):
            if getattr(self, name) <= 0:
                # A zero threshold reports every row as stuck the instant it is
                # written, which trains the operator to ignore the alert — the
                # failure mode a watchdog exists to avoid.
                raise ValueError(f"{name} must be > 0; got={getattr(self, name)}")
        if self.SOLANA_AUTONOMY_MIN_SUPERVISED_EXECUTIONS < 1:
            raise ValueError(
                "SOLANA_AUTONOMY_MIN_SUPERVISED_EXECUTIONS must be >= 1; got "
                f"{self.SOLANA_AUTONOMY_MIN_SUPERVISED_EXECUTIONS}. Zero would "
                "let the lane go autonomous having never executed anything "
                "under supervision, which is the precondition's whole point."
            )
        if self.SOLANA_PILOT_MAX_ATA_CREATES < 0:
            raise ValueError(
                "SOLANA_PILOT_MAX_ATA_CREATES must be >= 0; "
                f"got={self.SOLANA_PILOT_MAX_ATA_CREATES}"
            )
        # The total-fee ceiling now bounds every lamport the transaction moves
        # out of the wallet, not just the fees: tx_inspector counts ATA rent
        # against it. So the floor has to include the rent a legitimate build
        # can charge, or the ceiling is set below what any real swap produces
        # and the lane refuses everything.
        #
        # 5000 lamports = the protocol's base fee for the pilot's single
        # required signature (scout.live.solana.constants.LAMPORTS_PER_SIGNATURE).
        # 2039280 = the rent-exempt minimum for a 165-byte SPL token account
        # (scout.live.solana.constants.ATA_RENT_LAMPORTS_FALLBACK). Both are
        # inlined rather than imported to keep config free of a scout.live
        # dependency; the constants module is the source of truth.
        base_fee = 5_000
        ata_rent_floor = self.SOLANA_PILOT_MAX_ATA_CREATES * 2_039_280
        floor = (
            self.SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS
            + self.SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS
            + base_fee
            + ata_rent_floor
        )
        if self.SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS < floor:
            raise ValueError(
                "SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS is below what a legitimate "
                "build can cost, so no swap could ever pass inspection. "
                f"got total={self.SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS}, "
                f"required>={floor} = priority "
                f"{self.SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS} + tip "
                f"{self.SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS} + base signature fee "
                f"{base_fee} + ATA rent {ata_rent_floor} "
                f"({self.SOLANA_PILOT_MAX_ATA_CREATES} accounts x 2039280). "
                "Lower SOLANA_PILOT_MAX_ATA_CREATES or the component ceilings "
                "if you want a tighter total."
            )
        return self

    @model_validator(mode="after")
    def _validate_solana_pilot_runner(self) -> "Settings":
        """PR-S2: the runner's envelope must be able to produce a live trade.

        Every clause here fails at config load rather than as "the lane never
        trades" on pilot day — the same failure shape
        ``_validate_solana_pilot_caps`` above exists to prevent. The tip clause
        is the sharpest: ``jupiter_client.build_swap_transaction`` raises on a
        requested tip above ``SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS`` BEFORE it
        calls Jupiter, so an inverted pair means every build attempt dies.
        """
        tip = self.SOLANA_PILOT_JITO_TIP_LAMPORTS
        if tip > self.SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS:
            raise ValueError(
                "SOLANA_PILOT_JITO_TIP_LAMPORTS must be <= "
                "SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS; got "
                f"tip={tip}, ceiling={self.SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS}. "
                "The build refuses a tip over the ceiling before it is even "
                "requested, so no swap could ever be constructed."
            )
        # 1000 lamports = Jito's documented minimum tip
        # (scout.live.solana.constants.JITO_MIN_TIP_LAMPORTS). Below it the
        # auction will not pick the transaction up, so the lane would build,
        # inspect, sign and submit something that cannot land.
        if tip < 1_000:
            raise ValueError(
                f"SOLANA_PILOT_JITO_TIP_LAMPORTS must be >= 1000; got {tip}. "
                "Jito's minimum tip is 1000 lamports and a transaction below "
                "it will not be picked up by the auction."
            )
        if not 0 < self.SOLANA_PILOT_MAX_PRICE_IMPACT_PCT <= 100:
            raise ValueError(
                "SOLANA_PILOT_MAX_PRICE_IMPACT_PCT must be in (0, 100]; got "
                f"{self.SOLANA_PILOT_MAX_PRICE_IMPACT_PCT}. Zero admits no "
                "route and >100% is not a bound."
            )
        if self.SOLANA_PILOT_BLOCKHASH_SAFETY_MARGIN_BLOCKS < 0:
            raise ValueError(
                "SOLANA_PILOT_BLOCKHASH_SAFETY_MARGIN_BLOCKS must be >= 0; got "
                f"{self.SOLANA_PILOT_BLOCKHASH_SAFETY_MARGIN_BLOCKS}. A negative "
                "margin would extend the authorization past blockhash expiry."
            )
        for name in (
            "SOLANA_PILOT_CONFIRM_TIMEOUT_SEC",
            "SOLANA_PILOT_FINALIZE_TIMEOUT_SEC",
            "SOLANA_PILOT_POLL_INTERVAL_SEC",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0; got={getattr(self, name)}")
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
        # NoDecode (see field decl) suppresses pydantic-settings' eager JSON
        # decode, so the RAW env/init string reaches here. Mirrors
        # parse_alert_universe_exclude_id_patterns — accept three shapes:
        #   * comma-separated string ("solana,base") — the documented .env form
        #   * JSON array string ('["solana","base"]') — back-compat with JSON envs
        #   * native list                            — test / programmatic construction
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(c).strip() for c in parsed if str(c).strip()]
            return [c.strip() for c in s.split(",") if c.strip()]
        return v

    @field_validator("PERP_SYMBOLS", mode="before")
    @classmethod
    def parse_perp_symbols(cls, v: str | list[str]) -> list[str]:
        # NoDecode (see field decl) delivers the RAW env/init string here.
        # Mirrors parse_alert_universe_exclude_id_patterns — accept comma-
        # separated, JSON-array-string, and native-list shapes; symbols
        # normalize to upper-case.
        if isinstance(v, str):
            s = v.strip()
            parsed = None
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                except json.JSONDecodeError:
                    parsed = None
            if isinstance(parsed, list):
                v = [str(sym).strip().upper() for sym in parsed if str(sym).strip()]
            else:
                v = [sym.strip().upper() for sym in s.split(",") if sym.strip()]
        elif isinstance(v, list):
            v = [str(s).strip().upper() for s in v if str(s).strip()]
        if len(v) > 200:
            # Binance URL-length + subscription-rate safety (design spec §3.4).
            raise ValueError("PERP_SYMBOLS exceeds max length 200")
        return v

    @field_validator("ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS", mode="before")
    @classmethod
    def parse_alert_universe_exclude_id_patterns(cls, v: str | list[str]) -> list[str]:
        # NoDecode (see field decl) suppresses pydantic-settings' eager JSON
        # decode, so the RAW env/init string reaches here. Accept three shapes:
        #   * comma-separated string ("-a,-b")  — the documented .env form
        #   * JSON array string ('["-a","-b"]') — back-compat with JSON envs
        #   * native list                       — test / programmatic construction
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(p).strip() for p in parsed if str(p).strip()]
            return [p.strip() for p in s.split(",") if p.strip()]
        return v

    @field_validator("SIGNAL_DISPATCH_QUARANTINE", mode="before")
    @classmethod
    def parse_signal_dispatch_quarantine(cls, v: str | list[str]) -> list[str]:
        # NoDecode (see field decl) suppresses pydantic-settings' eager JSON
        # decode, so the RAW env/init string reaches here. Accept three shapes:
        #   * comma-separated string ("a,b")    — the documented .env form
        #   * JSON array string ('["a","b"]')   — back-compat with JSON envs
        #   * native list                       — test / programmatic construction
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(p).strip() for p in parsed if str(p).strip()]
            return [p.strip() for p in s.split(",") if p.strip()]
        return v

    @field_validator(
        "SOLANA_ALLOWED_INPUT_MINTS",
        "SOLANA_ALLOWED_OUTPUT_MINTS",
        "SOLANA_ALLOWED_ROUTE_LABELS",
        mode="before",
    )
    @classmethod
    def parse_solana_allowlists(cls, v: str | list[str]) -> list[str]:
        # NoDecode (see field decls) delivers the RAW env/init string here.
        # Same three shapes as every other list field in this file.
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(p).strip() for p in parsed if str(p).strip()]
            return [p.strip() for p in s.split(",") if p.strip()]
        return v

    @field_validator("SOLANA_RESOLVER_RPC_URLS", mode="before")
    @classmethod
    def parse_solana_resolver_rpc_urls(cls, v: str | list[str]) -> list[str]:
        # NoDecode (see field decl) suppresses pydantic-settings' eager JSON
        # decode, so the RAW env/init string reaches here. Accept three shapes:
        #   * comma-separated string ("https://a,https://b") — the .env form
        #   * JSON array string ('["https://a"]')  — back-compat with JSON envs
        #   * native list                          — test / programmatic
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(u).strip() for u in parsed if str(u).strip()]
            return [u.strip() for u in s.split(",") if u.strip()]
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

    @field_validator("TG_SHADOW_LAG_THRESHOLD_MIN", "TG_SHADOW_SCAN_CADENCE_MIN")
    @classmethod
    def _validate_tg_shadow_minutes(cls, v: int, info) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be > 0; got={v}")
        return v

    @field_validator("TG_SHADOW_MIN_CALLER_COVERAGE")
    @classmethod
    def _validate_tg_shadow_min_caller_coverage(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(
                f"TG_SHADOW_MIN_CALLER_COVERAGE is a rate in [0,1]; got={v}"
            )
        return v

    @field_validator("TG_CALLER_MIN_ELIGIBLE_CLUSTERS")
    @classmethod
    def _validate_tg_caller_min_eligible_clusters(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"TG_CALLER_MIN_ELIGIBLE_CLUSTERS must be >= 0; got={v}")
        return v

    @model_validator(mode="after")
    def _validate_tg_shadow_mcap_band(self) -> "Settings":
        if self.TG_SHADOW_MCAP_MIN_USD >= self.TG_SHADOW_MCAP_MAX_USD:
            raise ValueError(
                "TG_SHADOW_MCAP_MIN_USD "
                f"({self.TG_SHADOW_MCAP_MIN_USD}) must be < "
                f"TG_SHADOW_MCAP_MAX_USD ({self.TG_SHADOW_MCAP_MAX_USD})"
            )
        return self

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
