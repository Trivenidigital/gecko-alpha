"""Quantitative scoring engine for candidate tokens.

Scoring weights (must always document rationale):
- vol_liq_ratio (>MIN_VOL_LIQ_RATIO): 30 points -- Primary pump precursor
- market_cap_range (tiered: 8/5/2 pts): Pre-discovery range
- holder_growth (>20 new/hour): 25 points -- Organic accumulation
- token_age (<MAX_TOKEN_AGE_DAYS): 10 points -- Early stage
- social_mentions (>50 in 24h): 15 points -- CT discovery signal (optional)

DexScreener signals:
- buy_pressure (buy_ratio > 65%): 15 points -- Organic buying vs wash trade

CoinGecko signals:
- momentum_ratio (1h/24h > MOMENTUM_RATIO_THRESHOLD): 20 points -- Accelerating
- vol_acceleration (vol/7d_avg > MIN_VOL_ACCEL_RATIO): 25 points -- Volume spike
- cg_trending_rank (rank <= 10): 15 points -- Social discovery

Velocity signal:
- score_velocity (rising over 3 scans): 10 points -- Active accumulation

Chain bonus:
- solana_bonus (chain == solana): 5 points -- Meme premium

Total possible: 178 raw points, normalized to 0-100 scale, then co-occurrence multiplier applied
"""

from scout.config import Settings
from scout.models import CandidateToken


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

    # Hard disqualifier: liquidity floor
    if token.liquidity_usd < settings.MIN_LIQUIDITY_USD:
        return (0, [])

    # Signal 1: Volume/Liquidity Ratio -- 30 points
    # Primary pump precursor: high volume relative to liquidity indicates
    # strong buying pressure that hasn't yet been reflected in price
    if token.liquidity_usd > 0:
        ratio = token.volume_24h_usd / token.liquidity_usd
        if ratio > settings.MIN_VOL_LIQ_RATIO:
            points += 30
            signals.append("vol_liq_ratio")

    # Signal 2: Market Cap Tier Curve -- 2-8 points
    # Graduated scoring: sweet spot is $10K-$100K, tapering to $500K
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
    # Organic accumulation: new wallets acquiring the token indicates
    # genuine interest rather than wash trading
    if token.holder_growth_1h > 20:
        points += 25
        signals.append("holder_growth")

    # Signal 4: Token Age (bell curve) -- 0-10 points
    # Peak window is 1-3 days; too early = no liquidity, too late = dead
    age = token.token_age_days
    if age < 0.5:
        pass  # < 12h: 0 pts
    elif age < 1.0:
        points += 5  # 12-24h: 5 pts
        signals.append("token_age")
    elif age <= 3.0:
        points += 10  # 1-3 days: 10 pts (peak)
        signals.append("token_age")
    elif age <= 5.0:
        points += 5  # 3-5 days: 5 pts
        signals.append("token_age")
    # > 5 days: 0 pts

    # Signal 5: Social Mentions -- 15 points (optional)
    # CT discovery signal: early social chatter before mainstream awareness
    if token.social_mentions_24h > 50:
        points += 15
        signals.append("social_mentions")

    # Signal 6: Buy pressure ratio (DexScreener) -- 15 points
    # Distinguishes organic buying from wash trading
    if (
        token.txns_h1_buys is not None
        and token.txns_h1_sells is not None
    ):
        total_txns = token.txns_h1_buys + token.txns_h1_sells
        if total_txns > 0:
            buy_ratio = token.txns_h1_buys / total_txns
            if buy_ratio > 0.65:
                points += 15
                signals.append("buy_pressure")

    # Signal 7: Momentum ratio (CoinGecko) -- 20 points
    # Move is accelerating: most of 24h gain happened in the last 1h
    # Both values must be positive — negative/negative ratios are crashes, not pumps
    if (
        token.price_change_1h is not None
        and token.price_change_24h is not None
        and token.price_change_1h > 0
        and token.price_change_24h > 0
    ):
        ratio = token.price_change_1h / token.price_change_24h
        if ratio > settings.MOMENTUM_RATIO_THRESHOLD:
            points += 20
            signals.append("momentum_ratio")

    # Signal 8: Volume acceleration (CoinGecko) -- 25 points
    # Volume spike vs baseline: primary pump precursor
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
    # Entry into CG trending list = social discovery inflection point
    if token.cg_trending_rank is not None and token.cg_trending_rank <= 10:
        points += 15
        signals.append("cg_trending_rank")

    # Signal 10: Solana chain bonus -- 5 points (renumbered)
    # Solana has disproportionate meme coin activity
    if token.chain == "solana":
        points += 5
        signals.append("solana_bonus")

    # Signal 11: Score velocity bonus -- 10 points
    # Rising score across consecutive scans indicates active accumulation
    if historical_scores and len(historical_scores) >= 3:
        # historical_scores is newest-first; check if strictly increasing (oldest to newest)
        recent = list(reversed(historical_scores[:3]))
        if recent[0] < recent[1] < recent[2]:
            points += 10
            signals.append("score_velocity")

    # Normalize: max raw = 178 pts -> scale to 0-100
    points = min(100, int(points * 100 / 178))

    # Co-occurrence multiplier: applied after normalization
    vol_liq_fired = "vol_liq_ratio" in signals
    holder_fired = "holder_growth" in signals
    if vol_liq_fired and holder_fired:
        points = int(points * 1.2)
    elif vol_liq_fired and not holder_fired:
        points = int(points * 0.8)

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
