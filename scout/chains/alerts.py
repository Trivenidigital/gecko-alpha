"""Format and send high-conviction chain alerts."""

from __future__ import annotations

import structlog

from scout.chains.models import ActiveChain, ChainPattern
from scout.config import Settings
from scout.db import Database

logger = structlog.get_logger()


def format_chain_alert(chain: ActiveChain, pattern: ChainPattern) -> str:
    """Build a Telegram-ready chain completion message."""
    duration_h = (chain.last_step_time - chain.anchor_time).total_seconds() / 3600.0
    hit_rate_str = (
        f"{pattern.historical_hit_rate * 100:.1f}%"
        if pattern.historical_hit_rate is not None
        else "n/a"
    )
    lines = [
        "=== CONVICTION CHAIN COMPLETE ===",
        f"Pattern: {pattern.name} ({len(chain.steps_matched)}/{len(pattern.steps)} steps)",
        f"Token: {chain.token_id} ({chain.pipeline})",
        "",
        "Timeline:",
    ]
    for step_num in sorted(chain.steps_matched):
        step = next((s for s in pattern.steps if s.step_number == step_num), None)
        if step is None:
            continue
        lines.append(f"  step {step_num}: {step.event_type}")
    lines.extend(
        [
            "",
            f"Chain duration: {duration_h:.2f}h",
            f"Historical hit rate: {hit_rate_str} ({pattern.total_triggers} prior triggers)",
            f"Conviction boost: +{pattern.conviction_boost} points",
        ]
    )
    return "\n".join(lines)


async def send_chain_alert(
    db: Database,
    chain: ActiveChain,
    pattern: ChainPattern,
    settings: Settings,
) -> None:
    """Best-effort Telegram delivery. Never raises."""
    message = format_chain_alert(chain, pattern)
    try:
        import aiohttp

        from scout.alerter import send_telegram_message

        async with aiohttp.ClientSession() as session:
            await send_telegram_message(message, session, settings, parse_mode=None)
    except Exception:
        logger.exception(
            "chain_alert_send_failed",
            pattern=pattern.name,
            token_id=chain.token_id,
        )
