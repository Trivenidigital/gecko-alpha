"""Quantitative scoring engine for candidate tokens.

Scoring weights (must always document rationale):
- vol_liq_ratio (>MIN_VOL_LIQ_RATIO): 30 points -- Primary pump precursor
- market_cap_range (tiered: 8/5/2 pts): Pre-discovery range
- holder_growth (>20 new/hour): 25 points -- Organic accumulation
  (capability-gated on MORALIS holder enrichment; excluded from the divisor
  when unconfigured -- SIG-02)
- token_age (bell curve, peak 12-48h): 0-15 points -- Early stage
DexScreener signals:
- buy_pressure (buy_ratio > BUY_PRESSURE_THRESHOLD): 15 points -- Organic buying vs wash trade

CoinGecko signals:
- momentum_ratio (1h/24h > MOMENTUM_RATIO_THRESHOLD): 20 points -- Accelerating
- vol_acceleration (vol/7d_avg > MIN_VOL_ACCEL_RATIO): 25 points -- Volume spike
- cg_trending_rank (rank <= 10): 15 points -- Social discovery
- gt_trending (rank <= GT_TRENDING_TOP_N): 15 points -- GT per-chain DEX trending (BL-052)

Velocity signal:
- score_velocity (rising over 3 scans): 10 points -- Active accumulation

Chain bonus:
- solana_bonus (chain == solana): 5 points -- Meme premium

Max raw (all capabilities configured): 30+8+25+15+15+20+25+15+15+5+10+10 = 193.
The normalization divisor is DERIVED from the active-signal set (see
``normalization_divisor``): a capability-gated signal that cannot fire in the
current runtime is excluded so it does not dilute realized scores (SIG-02).
Normalized to 0-100 scale, then co-occurrence multiplier (1.15x if 3+ signals) applied.
"""

from collections.abc import Callable

import structlog

from scout.config import Settings
from scout.models import CandidateToken

logger = structlog.get_logger(__name__)

# Raw point weight of every scoring signal that participates in the 0-100
# normalization divisor. The divisor is DERIVED from this table (see
# ``normalization_divisor``) rather than a hardcoded constant, so it can never
# silently drift from the signal set — and a capability-gated signal that
# cannot fire in the current runtime is EXCLUDED from the divisor instead of
# diluting realized scores (SIG-02).
#
# Deliberately excluded (matching pre-SIG-02 behavior): cryptopanic_bullish
# (+10, gated off — see Signal 13) and stable_paired_liq (a bonus that is not
# part of the denominator).
_SIGNAL_WEIGHTS: dict[str, int] = {
    "vol_liq_ratio": 30,
    "market_cap_range": 8,
    "holder_growth": 25,
    "token_age": 15,
    "buy_pressure": 15,
    "momentum_ratio": 20,
    "vol_acceleration": 25,
    "cg_trending_rank": 15,
    "gt_trending": 15,
    "solana_bonus": 5,
    "score_velocity": 10,
    "perp_anomaly": 10,
}

# Signals that can only fire when an external enrichment capability is
# configured. When a predicate returns False the signal CANNOT fire, so
# ``score`` skips its contribution AND ``normalization_divisor`` omits its
# weight — a phantom must never sit in the divisor (SIG-02). holder_growth
# requires MORALIS holder enrichment (holder_enricher.py:37); with no
# MORALIS_API_KEY, holder_snapshots are never written, holder_growth_1h stays
# 0, and the +25 is structurally unreachable.
_CAPABILITY_GATED_SIGNALS: dict[str, Callable[[Settings], bool]] = {
    "holder_growth": lambda s: bool(s.MORALIS_API_KEY),
}

# Theoretical maximum raw score with every capability configured — DERIVED
# from the weight table (was a hardcoded 193). Update the table, not a magic
# number, when weights change.
SCORER_MAX_RAW = sum(_SIGNAL_WEIGHTS.values())

# The max-raw value at which Signal 14 (perp anomaly) is included in the
# denominator. When SCORER_MAX_RAW equals this value the denominator guard
# opens automatically. Recalibrated after removing the dead 15-point
# social_mentions denominator: both constants now 193.
_PERP_ENABLED_MAX_RAW = 193

# Runtime guard for Signal 14. See design spec §3.9.
# The constant and flag BOTH must be true for the signal to fire, preventing
# silent score inflation if PERP_SCORING_ENABLED is flipped ahead of the
# recalibration PR that bumps SCORER_MAX_RAW to _PERP_ENABLED_MAX_RAW.
_PERP_SCORING_DENOMINATOR_READY = SCORER_MAX_RAW >= _PERP_ENABLED_MAX_RAW


def _signal_can_fire(name: str, settings: Settings) -> bool:
    """Whether a capability-gated signal's dependency is configured.

    Signals without a capability predicate always return True.
    """
    predicate = _CAPABILITY_GATED_SIGNALS.get(name)
    return predicate is None or predicate(settings)


def active_scoring_signals(settings: Settings) -> list[str]:
    """Signal names that CAN fire (and thus participate in the normalization
    divisor) under the current runtime capabilities (SIG-02)."""
    return [name for name in _SIGNAL_WEIGHTS if _signal_can_fire(name, settings)]


def normalization_divisor(settings: Settings) -> int:
    """0-100 normalization divisor for the current runtime.

    Equals the capability-on maximum (``SCORER_MAX_RAW``) minus the weight of
    any capability-gated signal that cannot fire — so a phantom signal never
    dilutes realized scores (SIG-02). Reads the module-level ``SCORER_MAX_RAW``
    so recalibration-regime tests that patch it keep working.
    """
    divisor = SCORER_MAX_RAW
    for name, predicate in _CAPABILITY_GATED_SIGNALS.items():
        if not predicate(settings):
            divisor -= _SIGNAL_WEIGHTS[name]
    return divisor


def log_active_scoring_config(settings: Settings) -> None:
    """Emit one structured line naming the active scoring signals and the
    resulting normalization divisor.

    Called once at pipeline startup so future phantom-signal drift (a signal
    in the divisor that can never fire in the current runtime) is visible in
    logs (SIG-02, observability).
    """
    active = active_scoring_signals(settings)
    inactive = [name for name in _SIGNAL_WEIGHTS if name not in active]
    logger.info(
        "scoring_config_active",
        active_signals=active,
        inactive_signals=inactive,
        normalization_divisor=normalization_divisor(settings),
        scorer_max_raw=SCORER_MAX_RAW,
    )


def score(
    token: CandidateToken,
    settings: Settings,
    historical_scores: list[float] | None = None,
) -> tuple[int, list[str]]:
    """Score a candidate token based on quantitative signals.

    Pure function -- no I/O.

    Args:
        historical_scores: Previous scores (newest first) for velocity bonus.
            Passed in by caller (main.py) who reads from DB.

    Returns:
        (score, signals_fired) where score is 0-100 and signals_fired
        is a list of signal names that contributed to the score.
    """
    points = 0
    signals: list[str] = []

    # Hard disqualifier: liquidity floor.
    # Exempt CoinGecko-listed tokens — they have no on-chain pool
    # liquidity data (liquidity_usd=0) but are listed on major exchanges
    # with real order-book liquidity. The liquidity floor is meant for
    # DEX memecoins where a thin pool means un-tradable.
    if token.liquidity_usd < settings.MIN_LIQUIDITY_USD and token.chain != "coingecko":
        return (0, ["DISQUALIFIED_LOW_LIQUIDITY"])

    # Signal 1: Volume/Liquidity Ratio -- 30 points
    if token.liquidity_usd > 0:
        ratio = token.volume_24h_usd / token.liquidity_usd
        if ratio > settings.MIN_VOL_LIQ_RATIO:
            points += 30
            signals.append("vol_liq_ratio")

    # Signal 2: Market Cap Tier Curve -- 2-8 points
    cap = token.market_cap_usd
    if settings.MIN_MARKET_CAP <= cap <= 100_000:
        points += 8
        signals.append("market_cap_range")
    elif 100_000 < cap <= 250_000:
        points += 5
        signals.append("market_cap_range")
    elif 250_000 < cap <= settings.MAX_MARKET_CAP:
        points += 2
        signals.append("market_cap_range")

    # Signal 3: Holder Growth -- 25 points (capability-gated on MORALIS holder
    # enrichment). Without a MORALIS_API_KEY holder_growth_1h is never
    # populated, so the signal cannot fire and is also dropped from the
    # normalization divisor (SIG-02) — a phantom must not dilute scores.
    if _signal_can_fire("holder_growth", settings) and token.holder_growth_1h > 20:
        points += 25
        signals.append("holder_growth")

    # Signal 4: Token Age (bell curve) -- 0-15 points
    # Peak at 12-48h. Too new (<3h) = inflated metrics. Too old (>7d) = already discovered.
    age_hours = token.token_age_days * 24
    if age_hours < 3:
        pass  # < 3h: 0 pts (too new)
    elif age_hours < 12:
        points += 8  # 3-12h: 8 pts
        signals.append("token_age")
    elif age_hours <= 48:
        points += 15  # 12-48h: 15 pts (peak)
        signals.append("token_age")
    elif token.token_age_days <= 7:
        points += 5  # 48h-7d: 5 pts
        signals.append("token_age")
    # > 7 days: 0 pts

    # Signal 5: Buy pressure ratio (DexScreener) -- 15 points
    if token.txns_h1_buys is not None and token.txns_h1_sells is not None:
        total_txns = token.txns_h1_buys + token.txns_h1_sells
        if total_txns > 0:
            buy_ratio = token.txns_h1_buys / total_txns
            if buy_ratio > settings.BUY_PRESSURE_THRESHOLD:
                points += 15
                signals.append("buy_pressure")

    # Signal 6: Momentum ratio (CoinGecko/DexScreener) -- 20 points
    # Requires 24h change >= MOMENTUM_MIN_24H_CHANGE_PCT so stablecoin peg
    # wobble (e.g. 0.05%/0.08% -> ratio 0.625) doesn't trigger the signal.
    if (
        token.price_change_1h is not None
        and token.price_change_24h is not None
        and token.price_change_1h > 0
        and token.price_change_24h >= settings.MOMENTUM_MIN_24H_CHANGE_PCT
    ):
        ratio = token.price_change_1h / token.price_change_24h
        if ratio > settings.MOMENTUM_RATIO_THRESHOLD:
            points += 20
            signals.append("momentum_ratio")

    # Signal 8: Volume acceleration -- 25 points
    if (
        token.volume_24h_usd is not None
        and token.vol_7d_avg is not None
        and token.vol_7d_avg > 0
    ):
        vol_ratio = token.volume_24h_usd / token.vol_7d_avg
        if vol_ratio > settings.MIN_VOL_ACCEL_RATIO:
            points += 25
            signals.append("vol_acceleration")

    # Signal 9: CG trending rank -- 15 points
    if token.cg_trending_rank is not None and token.cg_trending_rank <= 10:
        points += 15
        signals.append("cg_trending_rank")

    # Signal 10: GeckoTerminal per-chain trending rank -- 15 points (BL-052)
    if (
        token.gt_trending_rank is not None
        and token.gt_trending_rank <= settings.GT_TRENDING_TOP_N
    ):
        points += 15
        signals.append("gt_trending")
        logger.info(
            "gt_trending_signal_fired",
            token=token.ticker,
            contract_address=token.contract_address,
            chain=token.chain,
            gt_trending_rank=token.gt_trending_rank,
        )

    # Signal 11: Solana chain bonus -- 5 points
    if token.chain == "solana":
        points += 5
        signals.append("solana_bonus")

    # Signal 13: CryptoPanic bullish news (BL-053) -- 10 points, gated.
    # SCORER_MAX_RAW does NOT include CryptoPanic's +10 — the ceiling-clamp
    # `min(points, 100)` at the end of score() keeps outputs well-formed
    # while the flag is off. Flipping CRYPTOPANIC_SCORING_ENABLED to True
    # is an operator-visible distribution shift and should ship with
    # a recalibration PR (SCORER_MAX_RAW bump + recalibrated tests).
    if (
        settings.CRYPTOPANIC_SCORING_ENABLED
        and token.latest_news_sentiment == "bullish"
        and (token.news_count_24h or 0) >= 1
        and not token.macro_news_flag
    ):
        points += 10
        signals.append("cryptopanic_bullish")

    # Signal 12: Score velocity bonus -- 10 points
    if historical_scores and len(historical_scores) >= 3:
        recent = list(reversed(historical_scores[:3]))
        if recent[0] < recent[1] < recent[2]:
            points += 10
            signals.append("score_velocity")

    # Signal 14 (was 12 pre-BL-053). Perp futures anomaly — 10 points
    # (GATED: PERP_SCORING_ENABLED + runtime denominator guard).
    # Double-gate: PERP_SCORING_ENABLED + SCORER_MAX_RAW >= _PERP_ENABLED_MAX_RAW.
    # gate is the runtime guard that prevents the scoring flag from silently
    # inflating scores before the recalibration PR lands. Tests monkeypatch
    # both. See design spec docs/superpowers/specs/
    # 2026-04-20-bl054-perp-ws-anomaly-detector-design.md §3.9.
    #
    # Enrichment truth = scorer truth: the DB is authoritative. We only check
    # whether the field is set (not None), not the ratio threshold — that was
    # already enforced by the anomaly classifier when writing to DB.
    if (
        settings.PERP_SCORING_ENABLED
        and _PERP_SCORING_DENOMINATOR_READY
        and token.perp_last_anomaly_at is not None
        and (token.perp_funding_flip or token.perp_oi_spike_ratio is not None)
    ):
        points += 10
        signals.append("perp_anomaly")

    # BL-NEW-QUOTE-PAIR: stable_paired_liq — +5 raw / +2 normalized.
    # Tokens paired with a known stablecoin AND liquidity_usd >= 50K signal
    # cleaner exit dynamics (no secondary stable-leg slippage). Counts toward
    # co-occurrence multiplier — adding to signals list is intended.
    # Match is case-sensitive against settings.STABLE_QUOTE_SYMBOLS; DexScreener
    # canonically returns uppercase symbols. If the API ever shifts, the
    # case-sensitivity test catches the regression and we'll normalize parser-side.
    # The isinstance guard surfaces upstream corruption (R6 PR review CRITICAL):
    # a non-string quote_symbol that bypassed Pydantic validation would silently
    # not fire (`int in tuple[str,...]` is False) — log it explicitly so it can
    # be diagnosed instead of vanishing into a non-fire.
    quote_symbol = token.quote_symbol
    if quote_symbol is not None and not isinstance(quote_symbol, str):
        logger.warning(
            "stable_paired_liq_invalid_symbol_type",
            contract_address=token.contract_address,
            quote_symbol_type=type(quote_symbol).__name__,
        )
        quote_symbol = None
    if (
        quote_symbol in settings.STABLE_QUOTE_SYMBOLS
        and token.liquidity_usd >= settings.STABLE_PAIRED_LIQ_THRESHOLD_USD
    ):
        points += settings.STABLE_PAIRED_BONUS
        signals.append("stable_paired_liq")

    # Normalize to 0-100 scale. The divisor derives from the active-signal set
    # (SIG-02) so a capability-gated signal that cannot fire does not dilute
    # realized scores.
    points = min(100, int(points * 100 / normalization_divisor(settings)))

    # Co-occurrence multiplier: reward multi-signal confluence
    if len(signals) >= settings.CO_OCCURRENCE_MIN_SIGNALS:
        points = int(points * settings.CO_OCCURRENCE_MULTIPLIER)

    points = min(points, 100)
    return (points, signals)


def signal_confidence(signals: list[str]) -> str:
    """Compute signal confidence level from fired signals.

    HIGH if 3+ signals fired, MEDIUM if 2, LOW if 0-1.
    """
    count = len(signals)
    if count >= 3:
        return "HIGH"
    elif count == 2:
        return "MEDIUM"
    return "LOW"
