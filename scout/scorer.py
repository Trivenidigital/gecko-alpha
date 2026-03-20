"""Quantitative scoring engine for candidate tokens.

Scoring weights (must always document rationale):
- vol_liq_ratio (>MIN_VOL_LIQ_RATIO): 30 points -- Primary pump precursor
- market_cap_range (MIN-MAX_MARKET_CAP): 20 points -- Pre-discovery range
- holder_growth (>20 new/hour): 25 points -- Organic accumulation
- token_age (<MAX_TOKEN_AGE_DAYS): 10 points -- Early stage
- social_mentions (>50 in 24h): 15 points -- CT discovery signal (optional)

CoinGecko signals:
- momentum_ratio (1h/24h > MOMENTUM_RATIO_THRESHOLD): 20 points -- Accelerating
- vol_acceleration (vol/7d_avg > MIN_VOL_ACCEL_RATIO): 25 points -- Volume spike
- cg_trending_rank (rank <= 10): 15 points -- Social discovery

Total possible: 160 raw points, capped at 100
"""

from scout.config import Settings
from scout.models import CandidateToken


def score(token: CandidateToken, settings: Settings) -> tuple[int, list[str]]:
    """Score a candidate token based on 8 quantitative signals.

    Pure function -- no I/O.

    Returns:
        (score, signals_fired) where score is 0-100 and signals_fired
        is a list of signal names that contributed to the score.
    """
    points = 0
    signals: list[str] = []

    # Signal 1: Volume/Liquidity Ratio -- 30 points
    # Primary pump precursor: high volume relative to liquidity indicates
    # strong buying pressure that hasn't yet been reflected in price
    if token.liquidity_usd > 0:
        ratio = token.volume_24h_usd / token.liquidity_usd
        if ratio > settings.MIN_VOL_LIQ_RATIO:
            points += 30
            signals.append("vol_liq_ratio")

    # Signal 2: Market Cap Range -- 20 points
    # Pre-discovery sweet spot: large enough to have real liquidity,
    # small enough to have significant upside potential
    if settings.MIN_MARKET_CAP <= token.market_cap_usd <= settings.MAX_MARKET_CAP:
        points += 20
        signals.append("market_cap_range")

    # Signal 3: Holder Growth -- 25 points
    # Organic accumulation: new wallets acquiring the token indicates
    # genuine interest rather than wash trading
    if token.holder_growth_1h > 20:
        points += 25
        signals.append("holder_growth")

    # Signal 4: Token Age -- 10 points
    # Early stage: younger tokens have more pump potential
    if token.token_age_days < settings.MAX_TOKEN_AGE_DAYS:
        points += 10
        signals.append("token_age")

    # Signal 5: Social Mentions -- 15 points (optional)
    # CT discovery signal: early social chatter before mainstream awareness
    if token.social_mentions_24h > 50:
        points += 15
        signals.append("social_mentions")

    # Signal 6: Momentum ratio (CoinGecko) -- 20 points
    # Move is accelerating: most of 24h gain happened in the last 1h
    if (
        token.price_change_1h is not None
        and token.price_change_24h is not None
        and token.price_change_24h != 0
    ):
        ratio = token.price_change_1h / token.price_change_24h
        if ratio > settings.MOMENTUM_RATIO_THRESHOLD:
            points += 20
            signals.append("momentum_ratio")

    # Signal 7: Volume acceleration (CoinGecko) -- 25 points
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

    # Signal 8: CG trending rank -- 15 points
    # Entry into CG trending list = social discovery inflection point
    if token.cg_trending_rank is not None and token.cg_trending_rank <= 10:
        points += 15
        signals.append("cg_trending_rank")

    points = min(points, 100)
    return (points, signals)
