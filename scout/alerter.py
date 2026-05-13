"""Alert delivery to Telegram and Discord."""

import json
import structlog

import aiohttp

from scout.config import Settings
from scout.exceptions import AlertDeliveryError
from scout.models import CandidateToken

logger = structlog.get_logger()


def format_alert_message(token: CandidateToken, signals: list[str]) -> str:
    """Format a candidate token into a human-readable alert message.

    Caller may pass raw model fields; this function applies _escape_md to
    every user-data field interpolated into Markdown formatters (token_name,
    ticker, chain, virality_class, signal names, mirofish_report). URL path
    fields (contract_address) are NOT escaped because Telegram requires
    literal characters inside [label](url) link targets. Sent with
    parse_mode='Markdown' in the send_alert payload. See CLAUDE.md
    §12b for the parse-mode hygiene rule.
    """
    lines: list[str] = []

    lines.append("⚠️ WARNING: RESEARCH ONLY - Not financial advice")
    lines.append("")
    lines.append(
        f"*{_escape_md(token.token_name)}* "
        f"({_escape_md(token.ticker)}) — {_escape_md(token.chain)}"
    )
    lines.append(f"Market Cap: ${token.market_cap_usd:,.0f}")
    lines.append("")

    # Conviction breakdown
    conviction_display = (
        f"{token.conviction_score:.1f}" if token.conviction_score is not None else "N/A"
    )
    quant_display = str(token.quant_score) if token.quant_score is not None else "N/A"
    narrative_display = (
        str(token.narrative_score) if token.narrative_score is not None else "N/A"
    )

    lines.append(f"Conviction Score: {conviction_display}")
    lines.append(f"  Quant: {quant_display}")
    if token.narrative_score is not None:
        lines.append(f"  Narrative: {narrative_display}")

    # Signals -- each signal_type contains underscores; escape per-element
    lines.append("")
    lines.append("Signals: " + ", ".join(_escape_md(s) for s in signals))

    # Virality
    if token.virality_class is not None:
        lines.append(f"Virality: {_escape_md(token.virality_class)}")

    # Narrative summary -- LLM-generated; can contain any markdown chars
    if token.mirofish_report is not None:
        lines.append(f"Narrative: {_escape_md(token.mirofish_report)}")

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

    # Source link -- use [chart](url) link syntax so MarkdownV1 does NOT
    # parse special chars inside the URL string. contract_address may
    # contain `_`, `*`, etc. and bare URL emission with parse_mode=Markdown
    # would silently mangle the link. Reviewer-2 fold on PR #111.
    lines.append("")
    if token.chain == "coingecko":
        url = f"https://www.coingecko.com/en/coins/{token.contract_address}"
    else:
        url = f"https://dexscreener.com/{token.chain}/{token.contract_address}"
    lines.append(f"[chart]({url})")

    return "\n".join(lines)


def format_daily_summary(data: dict) -> str:
    """Format the daily summary for Telegram."""
    lines: list[str] = []
    lines.append("📊 *Gecko-Alpha Daily Summary*")
    lines.append("")

    # Alerts
    lines.append(f"Alerts fired today: *{data['alerts_today']}*")

    # Win rate
    if data["outcomes_total"] > 0:
        lines.append(
            f"Win rate (4h+): *{data['win_rate_pct']}%* "
            f"({data['outcomes_wins']}/{data['outcomes_total']})"
        )
    else:
        lines.append("Win rate: No outcomes to measure yet")

    # Top signal combo
    if data["top_signal_combo"]:
        try:
            combo = json.loads(data["top_signal_combo"])
            lines.append(f"Top signal combo: {', '.join(combo)}")
        except (json.JSONDecodeError, TypeError):
            pass

    # Top 3 tokens
    top = data.get("top_tokens", [])
    if top:
        lines.append("")
        lines.append("*Top 3 Conviction Tokens:*")
        for i, t in enumerate(top, 1):
            conv = t.get("conviction_score")
            conv_str = f"{conv:.1f}" if conv is not None else "–"
            narr = t.get("narrative_score")
            narr_str = str(narr) if narr is not None else "–"
            lines.append(
                f"{i}. *{t['token_name']}* ({t['ticker']}) — "
                f"conv: {conv_str} | quant: {t.get('quant_score', '–')} | narr: {narr_str}"
            )
    else:
        lines.append("\nNo tokens scored today.")

    return "\n".join(lines)


async def send_telegram_message(
    text: str,
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    parse_mode: str | None = "Markdown",
) -> None:
    """Send a Telegram message.

    `parse_mode` defaults to `"Markdown"` for back-compat with all
    pre-existing callers. Pass `parse_mode=None` to send plain text
    (caller already escaped or doesn't want Markdown parsing —
    e.g., calibrate dry-run alerts whose body contains `[reason]`
    brackets that the Markdown parser would mis-handle as link
    anchors → silent 400 BAD_REQUEST per PR #76 silent-failure C1).
    """
    text = _truncate(text)
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: dict = {
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": text,
    }
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    try:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(
                    "Telegram daily summary failed", status=resp.status, body=body[:200]
                )
    except Exception as e:
        logger.warning("Telegram daily summary error", error=str(e))


TELEGRAM_MAX_LENGTH = 4096

# Characters that must be escaped for Telegram's legacy Markdown parse mode.
# Backslash must come first so a later pass does not double-escape the
# escape character we just inserted in front of an underscore.
# We deliberately do NOT escape hyphen / dot / paren because Markdown-v1 treats
# them literally; the intent here is to protect tokens named like AS_ROID from
# being interpreted as italics markers.
_MD_ESCAPE_CHARS = ("\\", "_", "*", "[", "]", "`")


def _escape_md(value: str) -> str:
    """Escape Markdown special characters for Telegram parse_mode='Markdown'.

    Safe to call with any value coerced to ``str`` -- returns an empty string
    for None. This helper is shared by the main alerter, the velocity
    alerter, and the social-velocity alerter.
    """
    if value is None:
        return ""
    out = str(value)
    for ch in _MD_ESCAPE_CHARS:
        out = out.replace(ch, f"\\{ch}")
    return out


def _truncate(text: str, max_len: int = TELEGRAM_MAX_LENGTH) -> str:
    """Truncate text to max_len, appending ... if truncated."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


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
    message = _truncate(format_alert_message(token, signals))

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
                raise AlertDeliveryError(f"Telegram returned {resp.status}: {body}")
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
                    logger.warning("Discord webhook returned error", status=resp.status)
        except Exception:
            logger.warning("Discord webhook delivery failed", exc_info=True)
