"""Telegram formatter + dispatcher for social-velocity alerts.

Markdown-v1 parse mode, 4096-char cap, reuses :func:`scout.alerter._escape_md`
for the `AS_ROID`-style underscore-in-symbol case (design spec §9).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import aiohttp
import structlog

from scout.alerter import TELEGRAM_MAX_LENGTH, _escape_md, _truncate
from scout.social.models import ResearchAlert, SpikeKind

if TYPE_CHECKING:
    from scout.config import Settings

logger = structlog.get_logger(__name__)


def _fmt_price(price: Optional[float]) -> str:
    if price is None:
        return "—"
    if price >= 1:
        return f"${price:,.4f}"
    if price >= 0.01:
        return f"${price:.4f}"
    return f"${price:.8f}"


def _fmt_usd(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def _fmt_pct(pct: Optional[float]) -> str:
    if pct is None:
        return "—"
    return f"{pct:+.1f}%"


def _fmt_compact_number(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return f"{value:.0f}"


def _render_alert(alert: ResearchAlert) -> str:
    symbol = _escape_md(alert.symbol)
    name = _escape_md(alert.name)
    kinds = ", ".join(k.value for k in alert.spike_kinds)

    # Galaxy / social-vol / interactions line
    galaxy = f"{int(alert.galaxy_score)}" if alert.galaxy_score is not None else "—"
    jump = (
        f" (+{int(alert.galaxy_jump)})"
        if alert.galaxy_jump is not None and alert.galaxy_jump > 0
        else ""
    )
    sv_ratio = (
        f"{alert.social_spike_ratio:.1f}x" if alert.social_spike_ratio is not None else "—"
    )
    interactions = _fmt_compact_number(alert.interactions_24h)

    # Price line
    if alert.current_price is None and alert.price_change_1h is None and alert.price_change_24h is None:
        price_line = "price: —"
    else:
        price_line = (
            f"price: {_fmt_price(alert.current_price)} "
            f"(1h: {_fmt_pct(alert.price_change_1h)}, 24h: {_fmt_pct(alert.price_change_24h)})"
        )

    # mcap / sentiment
    sentiment = (
        f"{alert.sentiment:.2f}" if alert.sentiment is not None else "—"
    )

    # LunarCrush link uses the LC-native coin_id which may be numeric.
    lc_url = f"https://lunarcrush.com/coins/{alert.coin_id}"

    # CoinGecko chart link is only emitted when we have a real CG slug
    # matched via the price-enrichment cache. Constructing it from a
    # numeric LunarCrush coin_id (e.g. 12345) produces a 404 page.
    if alert.cg_slug:
        cg_url = f"https://www.coingecko.com/en/coins/{alert.cg_slug}"
        links_line = f"[LunarCrush]({lc_url}) · [chart]({cg_url})"
    else:
        links_line = f"[LunarCrush]({lc_url})"

    return (
        f"\n*{symbol}* — {name}\n"
        f"kinds: {kinds}\n"
        f"galaxy: {galaxy}{jump} | social vol: {sv_ratio} | interactions: {interactions}\n"
        f"{price_line}\n"
        f"mcap: {_fmt_usd(alert.market_cap)} | sentiment: {sentiment}\n"
        f"{links_line}"
    )


def format_social_alert(alerts: list[ResearchAlert]) -> str:
    """Render a single batched Telegram message for the given alerts."""
    lines: list[str] = ["*Social Velocity* (LunarCrush)"]
    for alert in alerts:
        lines.append(_render_alert(alert))
    return _truncate("\n".join(lines), TELEGRAM_MAX_LENGTH)


async def send_social_alert(
    alerts: list[ResearchAlert],
    session: aiohttp.ClientSession,
    settings: "Settings",
) -> tuple[bool, Optional[str]]:
    """Dispatch a batched social-velocity message.

    Returns ``(ok, reason)``: ``ok`` is True on success, in which case
    ``reason`` is ``None``. On failure ``ok`` is False and ``reason`` is a
    short string (e.g. ``"Telegram send failed: 401 Unauthorized"``) so the
    caller can log the actual cause at the dispatch site.
    """
    if not alerts:
        return False, "no alerts"
    from scout.alerter import send_telegram_message

    text = format_social_alert(alerts)
    try:
        await send_telegram_message(text, session, settings)
        return True, None
    except Exception as exc:
        logger.exception("social_alert_send_failed", count=len(alerts))
        return False, f"{type(exc).__name__}: {exc}"
