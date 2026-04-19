"""Match heating categories and tokens against user preference filters.

Pure functions that read from strategy dict — no side effects.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

VALID_ALERT_MODES = {"all", "preferred_only", "exclude_only"}


def should_alert_category(category_id: str, strategy: object) -> bool:
    """Check if a heating category matches user alert preferences.

    Args:
        category_id: The CoinGecko category ID (e.g. "artificial-intelligence").
        strategy: Strategy object with .get() method returning typed values.

    Returns:
        True if the category should trigger an alert.
    """
    mode = str(strategy.get("user_alert_mode"))  # type: ignore[union-attr]

    if mode not in VALID_ALERT_MODES:
        logger.warning("preferences.invalid_alert_mode", mode=mode)
        return True

    if mode == "all":
        return True

    preferred: list[str] = strategy.get("user_preferred_categories") or []  # type: ignore[assignment, union-attr]
    excluded: list[str] = strategy.get("user_excluded_categories") or []  # type: ignore[assignment, union-attr]

    if mode == "preferred_only":
        if not preferred:
            logger.warning(
                "preferences.preferred_only_empty_list — all categories allowed as fallback"
            )
            return True  # fallback: don't silently block everything
        return category_id in preferred

    if mode == "exclude_only":
        return category_id not in excluded

    return True


def should_alert_token(token_mcap: float, strategy: object) -> bool:
    """Check if a token meets market-cap preference filters.

    Args:
        token_mcap: The token's market cap in USD.
        strategy: Strategy object with .get() method returning typed values.

    Returns:
        True if the token passes mcap filters.
    """
    min_mcap = float(strategy.get("user_min_market_cap") or 0)  # type: ignore[union-attr]
    max_mcap = float(strategy.get("user_max_market_cap") or 0)  # type: ignore[union-attr]

    if min_mcap and token_mcap < min_mcap:
        return False
    if max_mcap and token_mcap > max_mcap:
        return False
    return True
