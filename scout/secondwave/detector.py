"""Second-Wave Detection — scan DB, score re-accumulation, orchestrate loop."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiohttp
import structlog

from scout.config import Settings
from scout.db import Database
from scout.secondwave.alerts import format_secondwave_alert

logger = structlog.get_logger(__name__)

# Hardcoded to avoid fragile imports across ingestion modules.
CG_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"


def score_reaccumulation(
    candidate: dict,
    current_price: float | None,
    current_volume: float | None,
    current_market_cap: float | None,
    alert_market_cap: float,
    alert_price: float,
    volume_history: list[float],
    settings: Settings,
) -> tuple[int, list[str]]:
    """Compute re-accumulation score (0-100) and fired signals.

    4 signals: sufficient_drawdown (30), price_recovery (35),
    volume_pickup (20), strong_prior_signal (15).
    """
    points = 0
    signals: list[str] = []

    # Signal 1: Drawdown from peak (30 pts)
    if alert_market_cap and alert_market_cap > 0 and current_market_cap is not None:
        drawdown_pct = ((current_market_cap - alert_market_cap) / alert_market_cap) * 100
        if drawdown_pct <= -settings.SECONDWAVE_MIN_DRAWDOWN_PCT:
            points += 30
            signals.append("sufficient_drawdown")

    # Signal 2: Price recovery vs alert price (35 pts)
    if current_price is not None and alert_price and alert_price > 0:
        price_vs_alert_pct = (current_price / alert_price) * 100
        if price_vs_alert_pct >= settings.SECONDWAVE_MIN_RECOVERY_PCT:
            points += 35
            signals.append("price_recovery")

    # Signal 3: Volume pickup vs cooldown average (20 pts)
    if current_volume is not None and len(volume_history) >= 3:
        cooldown_avg = sum(volume_history) / len(volume_history)
        if cooldown_avg > 0:
            vol_ratio = current_volume / cooldown_avg
            if vol_ratio >= settings.SECONDWAVE_VOL_PICKUP_RATIO:
                points += 20
                signals.append("volume_pickup")

    # Signal 4: Prior signal strength (15 pts)
    if candidate.get("peak_quant_score", 0) >= 75:
        points += 15
        signals.append("strong_prior_signal")

    return (min(100, points), signals)


def build_secondwave_candidate(
    scan_row: dict,
    score: int,
    signals: list[str],
    current_price: float,
    current_volume: float | None,
    current_market_cap: float,
    volume_history: list[float],
    price_is_stale: bool,
) -> dict:
    """Shape a SecondWaveCandidate dict ready for db.insert_secondwave_candidate."""
    alert_market_cap = scan_row.get("alert_market_cap") or 0.0
    alert_price = scan_row.get("alert_price") or 0.0
    alerted_at_str = scan_row.get("alerted_at")
    alerted_at = (
        datetime.fromisoformat(alerted_at_str) if alerted_at_str else None
    )
    now = datetime.now(timezone.utc)
    days_since = (now - alerted_at).total_seconds() / 86400.0 if alerted_at else 0.0

    price_drop_from_peak_pct = (
        ((current_market_cap - alert_market_cap) / alert_market_cap) * 100
        if alert_market_cap
        else 0.0
    )
    price_vs_alert_pct = (
        (current_price / alert_price) * 100 if alert_price else 0.0
    )
    cooldown_avg = (
        sum(volume_history) / len(volume_history) if volume_history else 0.0
    )
    volume_vs_cooldown_avg = (
        (current_volume / cooldown_avg)
        if (current_volume is not None and cooldown_avg > 0)
        else 0.0
    )

    return {
        "contract_address": scan_row["contract_address"],
        "chain": scan_row.get("chain", ""),
        "token_name": scan_row.get("token_name", ""),
        "ticker": scan_row.get("ticker", ""),
        "coingecko_id": scan_row.get("coingecko_id"),
        "peak_quant_score": int(scan_row.get("peak_quant_score", 0)),
        "peak_signals_fired": scan_row.get("peak_signals_fired") or [],
        "first_seen_at": alerted_at_str or now.isoformat(),
        "original_alert_at": alerted_at_str,
        "original_market_cap": alert_market_cap,
        "alert_market_cap": alert_market_cap,
        "days_since_first_seen": round(days_since, 2),
        "price_drop_from_peak_pct": round(price_drop_from_peak_pct, 2),
        "current_price": current_price,
        "current_market_cap": current_market_cap,
        "current_volume_24h": current_volume,
        "price_vs_alert_pct": round(price_vs_alert_pct, 2),
        "volume_vs_cooldown_avg": round(volume_vs_cooldown_avg, 2),
        "price_is_stale": price_is_stale,
        "reaccumulation_score": score,
        "reaccumulation_signals": signals,
        "detected_at": now.isoformat(),
        "alerted_at": now.isoformat(),
    }


async def fetch_current_prices(
    session: aiohttp.ClientSession,
    coingecko_ids: list[str],
    settings: Settings,
) -> dict[str, dict]:
    """Batch-fetch CoinGecko live prices. Returns dict keyed by coingecko id."""
    if not coingecko_ids:
        return {}
    ids_param = ",".join(coingecko_ids)
    headers: dict[str, str] = {}
    if settings.COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = settings.COINGECKO_API_KEY
    params = {"vs_currency": "usd", "ids": ids_param, "per_page": 250}
    try:
        async with session.get(
            CG_MARKETS_URL,
            params=params,
            headers=headers,
        ) as resp:
            if resp.status != 200:
                logger.warning("secondwave_cg_markets_error", status=resp.status)
                return {}
            data = await resp.json()
            return {
                entry["id"]: {
                    "current_price": entry.get("current_price") or 0.0,
                    "total_volume": entry.get("total_volume") or 0.0,
                    "market_cap": entry.get("market_cap") or 0.0,
                }
                for entry in (data if isinstance(data, list) else [])
                if entry.get("id")
            }
    except Exception:
        logger.exception("secondwave_cg_markets_exception")
        return {}


async def run_once(
    session: aiohttp.ClientSession,
    db: Database,
    settings: Settings,
) -> int:
    """Execute one scan-confirm-alert cycle. Returns number of alerts fired."""
    from scout.alerter import send_telegram_message  # local import to avoid cycles

    scan_candidates = await db.get_secondwave_scan_candidates(
        min_age_days=settings.SECONDWAVE_COOLDOWN_MIN_DAYS,
        max_age_days=settings.SECONDWAVE_COOLDOWN_MAX_DAYS,
        min_peak_score=settings.SECONDWAVE_MIN_PRIOR_SCORE,
        dedup_days=settings.SECONDWAVE_DEDUP_DAYS,
    )
    if not scan_candidates:
        return 0

    # Resolve CoinGecko coin_id for each candidate via symbol lookup against
    # the predictions table (narrative-agent tokens).
    for scan_row in scan_candidates:
        cg_id = await db.get_coingecko_id_by_symbol(scan_row.get("ticker") or "")
        scan_row["coingecko_id"] = cg_id

    cg_ids = [c["coingecko_id"] for c in scan_candidates if c.get("coingecko_id")]
    fresh_prices = await fetch_current_prices(session, cg_ids, settings) if cg_ids else {}

    alerts_fired = 0
    for scan_row in scan_candidates:
        volume_history = await db.get_volume_history(
            scan_row["contract_address"],
            days=settings.SECONDWAVE_COOLDOWN_MAX_DAYS,
        )

        cg_id = scan_row.get("coingecko_id")
        if cg_id and cg_id in fresh_prices:
            pd = fresh_prices[cg_id]
            current_price = pd["current_price"]
            current_volume = pd["total_volume"]
            current_market_cap = pd["market_cap"]
            price_is_stale = False
        else:
            current_price = scan_row.get("alert_price") or 0.0
            current_volume = None
            current_market_cap = scan_row.get("alert_market_cap") or 0.0
            price_is_stale = True

        score, signals = score_reaccumulation(
            scan_row,
            current_price=current_price,
            current_volume=current_volume,
            current_market_cap=current_market_cap,
            alert_market_cap=scan_row.get("alert_market_cap") or 0.0,
            alert_price=scan_row.get("alert_price") or 0.0,
            volume_history=volume_history,
            settings=settings,
        )

        if score < settings.SECONDWAVE_ALERT_THRESHOLD:
            continue

        sw = build_secondwave_candidate(
            scan_row=scan_row,
            score=score,
            signals=signals,
            current_price=current_price,
            current_volume=current_volume,
            current_market_cap=current_market_cap,
            volume_history=volume_history,
            price_is_stale=price_is_stale,
        )
        await db.insert_secondwave_candidate(sw)
        await send_telegram_message(format_secondwave_alert(sw), session, settings)
        alerts_fired += 1

    return alerts_fired


async def secondwave_loop(
    session: aiohttp.ClientSession,
    settings: Settings,
) -> None:
    """Run the second-wave detector on SECONDWAVE_POLL_INTERVAL."""
    db = Database(settings.DB_PATH)
    await db.initialize()
    logger.info("secondwave_loop_started", interval=settings.SECONDWAVE_POLL_INTERVAL)
    try:
        while True:
            try:
                fired = await run_once(session, db, settings)
                logger.info("secondwave_cycle_complete", alerts_fired=fired)
            except Exception:
                logger.exception("secondwave_loop_error")
            await asyncio.sleep(settings.SECONDWAVE_POLL_INTERVAL)
    finally:
        await db.close()
