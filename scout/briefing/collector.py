"""Data collection for Market Briefing Agent.

Collects from 6 free external APIs + internal DB queries.
All external fetches return None on error (never crash the briefing).
"""

import asyncio
import json
from datetime import datetime, timezone

import aiohttp
import structlog

logger = structlog.get_logger()

_TIMEOUT = aiohttp.ClientTimeout(total=15)


# ---------------------------------------------------------------------------
# External API fetches
# ---------------------------------------------------------------------------


async def fetch_fear_greed(session: aiohttp.ClientSession) -> dict | None:
    """Fetch Fear & Greed Index from alternative.me.

    Returns {"value": 72, "classification": "Greed", "previous": 65} or None.
    """
    try:
        url = "https://api.alternative.me/fng/?limit=2"
        async with session.get(url, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning("fear_greed_fetch_failed", status=resp.status)
                return None
            data = await resp.json(content_type=None)
            entries = data.get("data", [])
            if not entries:
                return None
            current = entries[0]
            previous = entries[1] if len(entries) > 1 else {}
            return {
                "value": int(current.get("value", 0)),
                "classification": current.get("value_classification", "Unknown"),
                "previous": int(previous.get("value", 0)) if previous else None,
            }
    except Exception as e:
        logger.warning("fear_greed_error", error=str(e))
        return None


async def fetch_cg_global(
    session: aiohttp.ClientSession, api_key: str = ""
) -> dict | None:
    """Fetch CoinGecko /global endpoint for market overview.

    Uses the shared rate limiter. Returns market overview dict or None.
    """
    try:
        from scout.ratelimit import coingecko_limiter

        await coingecko_limiter.acquire()
        url = "https://api.coingecko.com/api/v3/global"
        headers = {}
        if api_key:
            headers["x-cg-demo-api-key"] = api_key
        async with session.get(url, headers=headers, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning("cg_global_fetch_failed", status=resp.status)
                return None
            payload = await resp.json(content_type=None)
            data = payload.get("data", {})
            mcap = data.get("total_market_cap", {})
            mcap_change = data.get("market_cap_change_percentage_24h_usd", 0)
            mcap_pct = data.get("market_cap_percentage", {})
            return {
                "total_mcap": mcap.get("usd", 0),
                "mcap_change_24h": mcap_change,
                "btc_dominance": round(mcap_pct.get("btc", 0), 1),
                "eth_dominance": round(mcap_pct.get("eth", 0), 1),
                "active_cryptocurrencies": data.get("active_cryptocurrencies", 0),
            }
    except Exception as e:
        logger.warning("cg_global_error", error=str(e))
        return None


async def fetch_funding_rates(
    session: aiohttp.ClientSession, api_key: str = ""
) -> dict | None:
    """Fetch BTC/ETH funding rates from CoinGlass.

    If 401/403, logs warning and returns None. Pass COINGLASS_API_KEY as header.
    """
    try:
        url = "https://open-api.coinglass.com/public/v2/funding"
        headers = {}
        if api_key:
            headers["coinglassSecret"] = api_key
        async with session.get(url, headers=headers, timeout=_TIMEOUT) as resp:
            if resp.status in (401, 403):
                logger.warning(
                    "coinglass_funding_auth_failed",
                    status=resp.status,
                    hint="Register at coinglass.com for a free API key",
                )
                return None
            if resp.status != 200:
                logger.warning("coinglass_funding_failed", status=resp.status)
                return None
            payload = await resp.json(content_type=None)
            data = payload.get("data", [])
            result = {}
            for item in data:
                symbol = (item.get("symbol") or "").upper()
                if symbol in ("BTC", "ETH"):
                    result[symbol.lower()] = item.get("uMarginFundingRate", 0)
            return result if result else None
    except Exception as e:
        logger.warning("coinglass_funding_error", error=str(e))
        return None


async def fetch_liquidations(
    session: aiohttp.ClientSession, api_key: str = ""
) -> dict | None:
    """Fetch 24h liquidation totals from CoinGlass.

    If 401/403, logs warning and returns None.
    """
    try:
        url = "https://open-api.coinglass.com/public/v2/liquidation_history"
        headers = {}
        if api_key:
            headers["coinglassSecret"] = api_key
        async with session.get(url, headers=headers, timeout=_TIMEOUT) as resp:
            if resp.status in (401, 403):
                logger.warning(
                    "coinglass_liquidation_auth_failed",
                    status=resp.status,
                    hint="Register at coinglass.com for a free API key",
                )
                return None
            if resp.status != 200:
                logger.warning("coinglass_liquidation_failed", status=resp.status)
                return None
            payload = await resp.json(content_type=None)
            data = payload.get("data", [])
            if not data:
                return None
            # Sum recent liquidations
            latest = data[0] if data else {}
            return {
                "total_24h": latest.get("volUsd", 0),
                "long_pct": latest.get("longRate", 50),
                "short_pct": latest.get("shortRate", 50),
            }
    except Exception as e:
        logger.warning("coinglass_liquidation_error", error=str(e))
        return None


async def fetch_defi_tvl(session: aiohttp.ClientSession) -> dict | None:
    """Fetch DeFi TVL from DeFi Llama /v2/chains.

    Returns total TVL, 1d change, and top chains.
    """
    try:
        url = "https://api.llama.fi/v2/chains"
        async with session.get(url, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning("defi_tvl_fetch_failed", status=resp.status)
                return None
            chains = await resp.json(content_type=None)
            if not isinstance(chains, list):
                return None
            total_tvl = sum(c.get("tvl", 0) for c in chains)
            # Compute approximate 1d change from chain-level data
            total_prev = 0
            for c in chains:
                tvl = c.get("tvl", 0)
                change_1d = c.get("change_1d", 0)
                if change_1d and tvl:
                    prev = tvl / (1 + change_1d / 100) if change_1d > -100 else tvl
                    total_prev += prev
                else:
                    total_prev += tvl
            change_pct = (
                ((total_tvl - total_prev) / total_prev * 100) if total_prev > 0 else 0
            )
            # Top 5 chains by TVL
            sorted_chains = sorted(chains, key=lambda c: c.get("tvl", 0), reverse=True)
            top_chains = [
                {
                    "name": c.get("name", ""),
                    "tvl": c.get("tvl", 0),
                    "change_1d": c.get("change_1d", 0),
                }
                for c in sorted_chains[:5]
            ]
            return {
                "total": total_tvl,
                "change_1d_pct": round(change_pct, 2),
                "top_chains": top_chains,
            }
    except Exception as e:
        logger.warning("defi_tvl_error", error=str(e))
        return None


async def fetch_crypto_news(session: aiohttp.ClientSession) -> list[dict] | None:
    """Fetch top 10 crypto headlines from CryptoCompare."""
    try:
        url = (
            "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&sortOrder=popular"
        )
        async with session.get(url, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning("crypto_news_fetch_failed", status=resp.status)
                return None
            payload = await resp.json(content_type=None)
            articles = payload.get("Data", [])[:10]
            return [
                {
                    "title": a.get("title", ""),
                    "source": a.get("source_info", {}).get("name", a.get("source", "")),
                    "url": a.get("url", ""),
                    "categories": a.get("categories", ""),
                }
                for a in articles
            ]
    except Exception as e:
        logger.warning("crypto_news_error", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Internal DB queries
# ---------------------------------------------------------------------------


async def collect_internal_data(db) -> dict:
    """Query our own DB for system intelligence."""
    conn = db._conn
    if conn is None:
        return {}

    return {
        "market_regime": await _get_current_regime(conn),
        "heating_categories": await _get_heating_categories(conn, hours=12),
        "cooling_categories": await _get_cooling_categories(conn, hours=12),
        "early_catches": await _get_recent_catches(conn, hours=12),
        "predictions": await _get_recent_predictions(conn, hours=12),
        "paper_pnl": await _get_paper_summary(conn),
        "volume_spikes": await _get_recent_spikes(conn, hours=12),
        "chain_completions": await _get_recent_chains(conn, hours=12),
    }


async def _get_current_regime(conn) -> str | None:
    try:
        cursor = await conn.execute("""SELECT market_regime FROM category_snapshots
               ORDER BY snapshot_at DESC LIMIT 1""")
        row = await cursor.fetchone()
        return row[0] if row else None
    except Exception:
        return None


async def _get_heating_categories(conn, hours: int = 12) -> list[dict]:
    try:
        cursor = await conn.execute(
            """SELECT category_name, acceleration, volume_growth_pct
               FROM narrative_signals
               WHERE datetime(detected_at) >= datetime('now', ?)
                 AND acceleration > 0
               ORDER BY acceleration DESC LIMIT 5""",
            (f"-{hours} hours",),
        )
        return [dict(r) for r in await cursor.fetchall()]
    except Exception:
        return []


async def _get_cooling_categories(conn, hours: int = 12) -> list[dict]:
    try:
        cursor = await conn.execute(
            """SELECT category_name, acceleration, volume_growth_pct
               FROM narrative_signals
               WHERE datetime(detected_at) >= datetime('now', ?)
                 AND acceleration < 0
               ORDER BY acceleration ASC LIMIT 5""",
            (f"-{hours} hours",),
        )
        return [dict(r) for r in await cursor.fetchall()]
    except Exception:
        return []


async def _get_recent_catches(conn, hours: int = 12) -> list[dict]:
    try:
        cursor = await conn.execute(
            """SELECT coin_id, symbol, name, narrative_lead_minutes,
                      pipeline_lead_minutes, peak_gain_pct
               FROM trending_comparisons
               WHERE datetime(created_at) >= datetime('now', ?)
                 AND (narrative_lead_minutes > 0 OR pipeline_lead_minutes > 0)
               ORDER BY created_at DESC LIMIT 10""",
            (f"-{hours} hours",),
        )
        return [dict(r) for r in await cursor.fetchall()]
    except Exception:
        return []


async def _get_recent_predictions(conn, hours: int = 12) -> list[dict]:
    try:
        cursor = await conn.execute(
            """SELECT coin_id, symbol, name, narrative_fit_score,
                      outcome_class, confidence
               FROM predictions
               WHERE is_control = 0
                 AND datetime(predicted_at) >= datetime('now', ?)
               ORDER BY predicted_at DESC LIMIT 10""",
            (f"-{hours} hours",),
        )
        return [dict(r) for r in await cursor.fetchall()]
    except Exception:
        return []


async def _get_paper_summary(conn) -> dict | None:
    try:
        cursor = await conn.execute("""SELECT
                 COUNT(*) as open_count,
                 COALESCE(SUM(amount_usd), 0) as total_exposure
               FROM paper_trades WHERE status = 'open'""")
        row = await cursor.fetchone()
        if not row:
            return None

        cursor2 = await conn.execute("""SELECT signal_type,
                 COUNT(*) as trades,
                 COALESCE(SUM(pnl_usd), 0) as pnl
               FROM paper_trades
               WHERE status != 'open'
                 AND datetime(closed_at) >= datetime('now', '-24 hours')
               GROUP BY signal_type""")
        by_signal = [dict(r) for r in await cursor2.fetchall()]

        return {
            "open_count": row[0],
            "total_exposure": round(row[1], 2),
            "by_signal_24h": by_signal,
        }
    except Exception:
        return None


async def _get_recent_spikes(conn, hours: int = 12) -> list[dict]:
    try:
        cursor = await conn.execute(
            """SELECT coin_id, symbol, name, spike_ratio, market_cap
               FROM volume_spikes
               WHERE datetime(detected_at) >= datetime('now', ?)
               ORDER BY spike_ratio DESC LIMIT 5""",
            (f"-{hours} hours",),
        )
        return [dict(r) for r in await cursor.fetchall()]
    except Exception:
        return []


async def _get_recent_chains(conn, hours: int = 12) -> list[dict]:
    try:
        cursor = await conn.execute(
            """SELECT token_id, pattern_name, steps_matched, total_steps,
                      conviction_boost, completed_at
               FROM chain_matches
               WHERE datetime(completed_at) >= datetime('now', ?)
               ORDER BY completed_at DESC LIMIT 5""",
            (f"-{hours} hours",),
        )
        return [dict(r) for r in await cursor.fetchall()]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Master collection function
# ---------------------------------------------------------------------------


async def collect_briefing_data(
    session: aiohttp.ClientSession,
    db,
    settings,
) -> dict:
    """Collect all data sources in parallel. Returns structured dict.

    External API failures return None (never crash). Internal queries
    are sequential but fast.
    """
    cg_api_key = getattr(settings, "COINGECKO_API_KEY", "")
    coinglass_key = getattr(settings, "COINGLASS_API_KEY", "")

    # External API calls (parallel)
    (
        fear_greed,
        cg_global,
        funding,
        liquidations,
        tvl,
        news,
    ) = await asyncio.gather(
        fetch_fear_greed(session),
        fetch_cg_global(session, api_key=cg_api_key),
        fetch_funding_rates(session, api_key=coinglass_key),
        fetch_liquidations(session, api_key=coinglass_key),
        fetch_defi_tvl(session),
        fetch_crypto_news(session),
        return_exceptions=True,
    )

    def _safe(val):
        return val if not isinstance(val, Exception) else None

    # Internal DB queries
    internal = await collect_internal_data(db)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fear_greed": _safe(fear_greed),
        "global_market": _safe(cg_global),
        "funding_rates": _safe(funding),
        "liquidations": _safe(liquidations),
        "defi_tvl": _safe(tvl),
        "news": _safe(news),
        "internal": internal,
    }
