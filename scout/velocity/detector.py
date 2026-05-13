"""CoinGecko 1h-velocity detector.

Flags tokens with an extreme 1h price move inside a target market-cap
band. Dedups per coin in a rolling window using the ``velocity_alerts``
table so the same coin doesn't page every cycle.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp
import structlog

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.db import Database

log = structlog.get_logger(__name__)


def _f(value) -> float | None:
    """Coerce CoinGecko numeric fields to float; return None on invalid."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def detect_velocity(
    db: "Database",
    raw_coins: list[dict],
    settings: "Settings",
) -> list[dict]:
    """Filter raw CoinGecko markets for velocity-alert candidates.

    Applies 1h-change / mcap-band / vol-mcap-ratio filters, dedups against
    ``velocity_alerts`` within ``VELOCITY_DEDUP_HOURS``, takes the top-N by
    1h change, and persists the resulting detections so the next cycle
    treats them as deduped.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    min_1h = float(getattr(settings, "VELOCITY_MIN_1H_PCT", 30.0))
    min_mcap = float(getattr(settings, "VELOCITY_MIN_MCAP", 500_000))
    max_mcap = float(getattr(settings, "VELOCITY_MAX_MCAP", 50_000_000))
    min_ratio = float(getattr(settings, "VELOCITY_MIN_VOL_MCAP_RATIO", 0.2))
    dedup_hours = int(getattr(settings, "VELOCITY_DEDUP_HOURS", 4))
    top_n = int(getattr(settings, "VELOCITY_TOP_N", 10))

    candidates: list[dict] = []
    for coin in raw_coins:
        coin_id = coin.get("id")
        if not coin_id:
            continue
        change_1h = _f(coin.get("price_change_percentage_1h_in_currency"))
        mcap = _f(coin.get("market_cap"))
        volume = _f(coin.get("total_volume"))
        if change_1h is None or mcap is None or volume is None:
            continue
        if change_1h < min_1h:
            continue
        if mcap < min_mcap or mcap > max_mcap:
            continue
        ratio = volume / mcap if mcap > 0 else 0.0
        if ratio < min_ratio:
            continue
        candidates.append(
            {
                "coin_id": coin_id,
                "symbol": (coin.get("symbol") or "???").upper(),
                "name": coin.get("name") or coin_id,
                "price_change_1h": change_1h,
                "price_change_24h": _f(coin.get("price_change_percentage_24h")),
                "market_cap": mcap,
                "volume_24h": volume,
                "vol_mcap_ratio": ratio,
                "current_price": _f(coin.get("current_price")),
            }
        )

    if not candidates:
        return []

    candidates.sort(key=lambda c: c["price_change_1h"], reverse=True)

    # Dedup against recent alerts
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=dedup_hours)).isoformat()
    cursor = await db._conn.execute(
        """SELECT coin_id FROM velocity_alerts
           WHERE datetime(detected_at) >= datetime(?)""",
        (cutoff,),
    )
    rows = await cursor.fetchall()
    recent_ids = {r[0] for r in rows}

    fresh: list[dict] = [c for c in candidates if c["coin_id"] not in recent_ids]
    fresh = fresh[:top_n]

    if not fresh:
        return []

    now = datetime.now(timezone.utc).isoformat()
    for det in fresh:
        await db._conn.execute(
            """INSERT INTO velocity_alerts
               (coin_id, symbol, name, price_change_1h, price_change_24h,
                market_cap, volume_24h, vol_mcap_ratio, current_price, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                det["coin_id"],
                det["symbol"],
                det["name"],
                det["price_change_1h"],
                det["price_change_24h"],
                det["market_cap"],
                det["volume_24h"],
                det["vol_mcap_ratio"],
                det["current_price"],
                now,
            ),
        )
    await db._conn.commit()

    log.info(
        "velocity_detected",
        count=len(fresh),
        ids=[d["coin_id"] for d in fresh],
    )
    return fresh


def _fmt_price(price: float | None) -> str:
    if price is None:
        return "?"
    if price >= 1:
        return f"${price:,.4f}"
    if price >= 0.01:
        return f"${price:.4f}"
    return f"${price:.8f}"


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "?"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def format_velocity_alert(detections: list[dict]) -> str:
    """Render a Markdown Telegram message for the given detections.

    Caller may pass raw dict fields; this function applies _escape_md to
    every user-data field interpolated into Markdown formatters (symbol,
    name). URL path fields (coin_id) are NOT escaped because Telegram
    requires literal characters inside [label](url) link targets. See
    CLAUDE.md §12b for the parse-mode hygiene rule.
    """
    from scout.alerter import _escape_md

    lines: list[str] = ["*Velocity Alerts* (1h pump)"]
    for det in detections:
        ch_1h = det.get("price_change_1h") or 0.0
        ch_24h = det.get("price_change_24h")
        mcap = det.get("market_cap")
        vol = det.get("volume_24h")
        ratio = det.get("vol_mcap_ratio") or 0.0
        price = det.get("current_price")
        ch_24h_s = f"{ch_24h:+.1f}%" if ch_24h is not None else "?"
        url = f"https://www.coingecko.com/en/coins/{det['coin_id']}"
        symbol_safe = _escape_md(det.get("symbol", ""))
        name_safe = _escape_md(det.get("name", ""))
        lines.append(
            f"\n*{symbol_safe}* — {name_safe}\n"
            f"1h: *{ch_1h:+.1f}%* | 24h: {ch_24h_s} | price: {_fmt_price(price)}\n"
            f"mcap: {_fmt_usd(mcap)} | vol: {_fmt_usd(vol)} | v/mc: {ratio:.2f}\n"
            f"[chart]({url})"
        )
    return "\n".join(lines)


async def alert_velocity_detections(
    detections: list[dict],
    session: aiohttp.ClientSession,
    settings: "Settings",
) -> None:
    """Send a single batched Telegram message for the detections."""
    if not detections:
        return
    # Deferred import to avoid an import cycle at module load time.
    from scout.alerter import send_telegram_message

    text = format_velocity_alert(detections)
    try:
        await send_telegram_message(
            text, session, settings, parse_mode="Markdown"
        )
    except Exception:
        log.exception("velocity_alert_send_failed", count=len(detections))
