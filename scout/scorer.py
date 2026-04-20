"""Quantitative scoring engine for candidate tokens.

Scoring weights (must always document rationale):
- vol_liq_ratio (>MIN_VOL_LIQ_RATIO): 30 points -- Primary pump precursor
- market_cap_range (tiered: 8/5/2 pts): Pre-discovery range
- holder_growth (>20 new/hour): 25 points -- Organic accumulation
- token_age (bell curve, peak 12-48h): 0-15 points -- Early stage
- social_mentions (>50 in 24h): 15 points -- CT discovery signal (optional)

DexScreener signals:
- buy_pressure (buy_ratio > BUY_PRESSURE_THRESHOLD): 15 points -- Organic buying vs wash trade

CoinGecko signals:
- momentum_ratio (1h/24h > MOMENTUM_RATIO_THRESHOLD): 20 points -- Accelerating
- vol_acceleration (vol/7d_avg > MIN_VOL_ACCEL_RATIO): 25 points -- Volume spike
- cg_trending_rank (rank <= 10): 15 points -- Social discovery

Velocity signal:
- score_velocity (rising over 3 scans): 10 points -- Active accumulation

Chain bonus:
- solana_bonus (chain == solana): 5 points -- Meme premium

Max raw: 30+8+25+15+15+15+20+25+15+5+10 = 183 points
Normalized to 0-100 scale, then co-occurrence multiplier (1.15x if 3+ signals) applied.
"""

from scout.config import Settings
from scout.models import CandidateToken

# Theoretical maximum raw score — update if signal weights change
SCORER_MAX_RAW = 183

# The max-raw value at which Signal 14 (perp anomaly) is included in the
# denominator. When SCORER_MAX_RAW reaches this value the denominator guard
# opens automatically. Must equal SCORER_MAX_RAW in the recalibration PR.
_PERP_ENABLED_MAX_RAW = 203

# Runtime guard for Signal 14. See design spec §3.9.
# The constant and flag BOTH must be true for the signal to fire, preventing
# silent score inflation if PERP_SCORING_ENABLED is flipped ahead of the
# recalibration PR that bumps SCORER_MAX_RAW to _PERP_ENABLED_MAX_RAW.
_PERP_SCORING_DENOMINATOR_READY = SCORER_MAX_RAW >= _PERP_ENABLED_MAX_RAW


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

    # Signal 3: Holder Growth -- 25 points
    if token.holder_growth_1h > 20:
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

    # Signal 5: Social Mentions -- 15 points (optional)
    if token.social_mentions_24h > 50:
        points += 15
        signals.append("social_mentions")

    # Signal 6: Buy pressure ratio (DexScreener) -- 15 points
    if token.txns_h1_buys is not None and token.txns_h1_sells is not None:
        total_txns = token.txns_h1_buys + token.txns_h1_sells
        if total_txns > 0:
            buy_ratio = token.txns_h1_buys / total_txns
            if buy_ratio > settings.BUY_PRESSURE_THRESHOLD:
                points += 15
                signals.append("buy_pressure")

    # Signal 7: Momentum ratio (CoinGecko/DexScreener) -- 20 points
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

    # Signal 10: Solana chain bonus -- 5 points
    if token.chain == "solana":
        points += 5
        signals.append("solana_bonus")

    # Signal 11: Score velocity bonus -- 10 points
    if historical_scores and len(historical_scores) >= 3:
        recent = list(reversed(historical_scores[:3]))
        if recent[0] < recent[1] < recent[2]:
            points += 10
            signals.append("score_velocity")

    # Signal 14 (was 12 pre-BL-053). Perp futures anomaly — 10 points
    # (GATED: PERP_SCORING_ENABLED + runtime denominator guard).
    # Double-gate: PERP_SCORING_ENABLED + SCORER_MAX_RAW >= 203. The second
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

    # Normalize to 0-100 scale
    points = min(100, int(points * 100 / SCORER_MAX_RAW))

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
