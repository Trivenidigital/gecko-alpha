"""Deterministic flag computation for narrative and memecoin pipelines."""

from __future__ import annotations

from scout.counter.models import RedFlag


def compute_narrative_flags(
    price_change_30d: float,
    commits_4w: int,
    reddit_subs: int,
    sentiment_up_pct: float,
    narrative_fit_score: float,
    token_vol_change_24h: float,
    category_vol_growth_pct: float,
    market_cap: float = 0.0,
    category_leader_mcap: float = 0.0,
) -> list[RedFlag]:
    """Compute deterministic red flags for narrative-driven tokens.

    Args:
        price_change_30d: Percentage price change over the last 30 days.
        commits_4w: Number of GitHub commits in the last 4 weeks.
        reddit_subs: Number of Reddit subscribers for the project.
        sentiment_up_pct: Percentage of positive sentiment (0-100).
        narrative_fit_score: How well the token fits its narrative (0-100).
        token_vol_change_24h: Token volume change in last 24h (percentage).
        category_vol_growth_pct: Category volume growth (percentage).
        market_cap: Token market cap in USD (default 0.0).
        category_leader_mcap: Market cap of the category leader in USD (default 0.0).

    Returns:
        List of RedFlag instances for every triggered condition.
    """
    flags: list[RedFlag] = []

    # already_peaked
    if price_change_30d > 100:
        flags.append(
            RedFlag(
                flag="already_peaked",
                severity="high",
                detail=f"30d price change {price_change_30d:.0f}% exceeds 100% threshold",
            )
        )
    elif price_change_30d > 50:
        flags.append(
            RedFlag(
                flag="already_peaked",
                severity="medium",
                detail=f"30d price change {price_change_30d:.0f}% exceeds 50% threshold",
            )
        )

    # dead_project
    if commits_4w == 0:
        flags.append(
            RedFlag(
                flag="dead_project",
                severity="high",
                detail="Zero commits in the last 4 weeks",
            )
        )
    elif commits_4w < 10:
        flags.append(
            RedFlag(
                flag="dead_project",
                severity="medium",
                detail=f"Only {commits_4w} commits in the last 4 weeks",
            )
        )

    # weak_community
    if reddit_subs < 100:
        flags.append(
            RedFlag(
                flag="weak_community",
                severity="high",
                detail=f"Reddit subscribers ({reddit_subs}) below 100",
            )
        )
    elif reddit_subs < 1000:
        flags.append(
            RedFlag(
                flag="weak_community",
                severity="medium",
                detail=f"Reddit subscribers ({reddit_subs}) below 1000",
            )
        )

    # negative_sentiment
    if sentiment_up_pct < 40:
        flags.append(
            RedFlag(
                flag="negative_sentiment",
                severity="high",
                detail=f"Positive sentiment at {sentiment_up_pct:.0f}%, below 40% threshold",
            )
        )
    elif sentiment_up_pct < 50:
        flags.append(
            RedFlag(
                flag="negative_sentiment",
                severity="medium",
                detail=f"Positive sentiment at {sentiment_up_pct:.0f}%, below 50% threshold",
            )
        )

    # volume_divergence
    if token_vol_change_24h < -10 and category_vol_growth_pct > 10:
        flags.append(
            RedFlag(
                flag="volume_divergence",
                severity="high",
                detail=(
                    f"Token volume down {token_vol_change_24h:.0f}% while "
                    f"category up {category_vol_growth_pct:.0f}%"
                ),
            )
        )

    # overvalued_vs_leaders
    if category_leader_mcap > 0 and market_cap > 0:
        if market_cap > category_leader_mcap * 0.5:
            flags.append(
                RedFlag(
                    flag="overvalued_vs_leaders",
                    severity="medium",
                    detail=f"Token mcap ${market_cap:,.0f} is >{50}% of leader ${category_leader_mcap:,.0f}",
                )
            )

    # narrative_mismatch
    if narrative_fit_score < 40:
        flags.append(
            RedFlag(
                flag="narrative_mismatch",
                severity="high",
                detail=f"Narrative fit score {narrative_fit_score:.0f} below 40",
            )
        )
    elif narrative_fit_score < 60:
        flags.append(
            RedFlag(
                flag="narrative_mismatch",
                severity="medium",
                detail=f"Narrative fit score {narrative_fit_score:.0f} below 60",
            )
        )

    return flags


def compute_memecoin_flags(
    buy_pressure: float,
    liquidity_usd: float,
    token_age_days: float,
    vol_liq_ratio: float,
    holder_count: int,
    goplus_creator_pct: float,
    goplus_is_honeypot: bool,
) -> list[RedFlag]:
    """Compute deterministic red flags for memecoin tokens.

    Args:
        buy_pressure: Buy-side ratio (0.0 to 1.0).
        liquidity_usd: Liquidity pool size in USD.
        token_age_days: Age of the token in days.
        vol_liq_ratio: Volume-to-liquidity ratio.
        holder_count: Number of unique token holders.
        goplus_creator_pct: Creator wallet percentage of supply (0-100).
        goplus_is_honeypot: Whether GoPlus flagged token as honeypot.

    Returns:
        List of RedFlag instances for every triggered condition.
    """
    flags: list[RedFlag] = []

    # wash_trading
    if buy_pressure > 0.95 or buy_pressure < 0.05:
        flags.append(
            RedFlag(
                flag="wash_trading",
                severity="high",
                detail=f"Buy pressure {buy_pressure:.2f} outside [0.05, 0.95]",
            )
        )
    elif buy_pressure > 0.90 or buy_pressure < 0.10:
        flags.append(
            RedFlag(
                flag="wash_trading",
                severity="medium",
                detail=f"Buy pressure {buy_pressure:.2f} outside [0.10, 0.90]",
            )
        )

    # deployer_concentration
    if goplus_creator_pct > 20:
        flags.append(
            RedFlag(
                flag="deployer_concentration",
                severity="high",
                detail=f"Creator holds {goplus_creator_pct:.1f}% of supply (>20%)",
            )
        )
    elif goplus_creator_pct > 10:
        flags.append(
            RedFlag(
                flag="deployer_concentration",
                severity="medium",
                detail=f"Creator holds {goplus_creator_pct:.1f}% of supply (>10%)",
            )
        )

    # liquidity_trap
    if liquidity_usd < 15000:
        flags.append(
            RedFlag(
                flag="liquidity_trap",
                severity="high",
                detail=f"Liquidity ${liquidity_usd:,.0f} below $15,000",
            )
        )
    elif liquidity_usd < 30000:
        flags.append(
            RedFlag(
                flag="liquidity_trap",
                severity="medium",
                detail=f"Liquidity ${liquidity_usd:,.0f} below $30,000",
            )
        )

    # token_too_new
    if token_age_days < 0.25:
        flags.append(
            RedFlag(
                flag="token_too_new",
                severity="high",
                detail=f"Token age {token_age_days:.2f} days (<6 hours)",
            )
        )
    elif token_age_days < 0.5:
        flags.append(
            RedFlag(
                flag="token_too_new",
                severity="medium",
                detail=f"Token age {token_age_days:.2f} days (<12 hours)",
            )
        )

    # suspicious_volume
    if vol_liq_ratio > 50:
        flags.append(
            RedFlag(
                flag="suspicious_volume",
                severity="high",
                detail=f"Volume/liquidity ratio {vol_liq_ratio:.1f} exceeds 50",
            )
        )
    elif vol_liq_ratio > 20:
        flags.append(
            RedFlag(
                flag="suspicious_volume",
                severity="medium",
                detail=f"Volume/liquidity ratio {vol_liq_ratio:.1f} exceeds 20",
            )
        )

    # honeypot_risk
    if goplus_is_honeypot:
        flags.append(
            RedFlag(
                flag="honeypot_risk",
                severity="high",
                detail="GoPlus flagged token as honeypot",
            )
        )

    # low_holders
    if holder_count < 50:
        flags.append(
            RedFlag(
                flag="low_holders",
                severity="high",
                detail=f"Only {holder_count} holders (<50)",
            )
        )
    elif holder_count < 200:
        flags.append(
            RedFlag(
                flag="low_holders",
                severity="medium",
                detail=f"Only {holder_count} holders (<200)",
            )
        )

    return flags
