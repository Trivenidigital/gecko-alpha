"""Alert delivery to Telegram and Discord."""

import structlog

import aiohttp

from scout.config import Settings
from scout.exceptions import AlertDeliveryError
from scout.models import CandidateToken

logger = structlog.get_logger()


def format_alert_message(token: CandidateToken, signals: list[str]) -> str:
    """Format a candidate token into a human-readable alert message."""
    lines: list[str] = []

    lines.append("⚠️ WARNING: RESEARCH ONLY - Not financial advice")
    lines.append("")
    lines.append(f"*{token.token_name}* ({token.ticker}) — {token.chain}")
    lines.append(f"Market Cap: ${token.market_cap_usd:,.0f}")
    lines.append("")

    # Conviction breakdown
    conviction_display = f"{token.conviction_score:.1f}" if token.conviction_score is not None else "N/A"
    quant_display = str(token.quant_score) if token.quant_score is not None else "N/A"
    narrative_display = str(token.narrative_score) if token.narrative_score is not None else "N/A"

    lines.append(f"Conviction Score: {conviction_display}")
    lines.append(f"  Quant: {quant_display}")
    if token.narrative_score is not None:
        lines.append(f"  Narrative: {narrative_display}")

    # Signals
    lines.append("")
    lines.append("Signals: " + ", ".join(signals))

    # Virality
    if token.virality_class is not None:
        lines.append(f"Virality: {token.virality_class}")

    # Narrative summary
    if token.mirofish_report is not None:
        lines.append(f"Narrative: {token.mirofish_report}")

    # CoinGecko signal flags
    cg_flags = []
    if "momentum_ratio" in signals:
        cg_flags.append("Momentum: 1h gain accelerating vs 24h")
    if "vol_acceleration" in signals:
        cg_flags.append("Volume Spike: current vol >> 7d average")
    if "cg_trending_rank" in signals:
        cg_flags.append(f"CG Trending: rank #{token.cg_trending_rank or '?'}")
    if cg_flags:
        lines.append("")
        lines.append("CoinGecko Signals:")
        for flag in cg_flags:
            lines.append(f"  {flag}")

    # DEXScreener link
    lines.append("")
    lines.append(
        f"https://dexscreener.com/{token.chain}/{token.contract_address}"
    )

    return "\n".join(lines)


async def send_alert(
    token: CandidateToken,
    signals: list[str],
    session: aiohttp.ClientSession,
    settings: Settings,
) -> None:
    """Send alert to Telegram (required) and Discord (optional).

    Raises ``AlertDeliveryError`` if Telegram delivery fails.
    Discord failures are logged as warnings but do not raise.
    """
    message = format_alert_message(token, signals)

    # --- Telegram (required) ---
    telegram_url = (
        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    )
    payload = {
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }

    try:
        async with session.post(telegram_url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise AlertDeliveryError(
                    f"Telegram returned {resp.status}: {body}"
                )
    except AlertDeliveryError:
        raise
    except Exception as exc:
        raise AlertDeliveryError(f"Telegram send failed: {exc}") from exc

    # --- Discord (optional) ---
    if settings.DISCORD_WEBHOOK_URL:
        try:
            async with session.post(
                settings.DISCORD_WEBHOOK_URL,
                json={"content": message},
            ) as resp:
                if resp.status not in (200, 204):
                    logger.warning(
                        "Discord webhook returned error", status=resp.status
                    )
        except Exception:
            logger.warning("Discord webhook delivery failed", exc_info=True)
