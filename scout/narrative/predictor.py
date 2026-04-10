"""PREDICT phase — laggard selection, Claude scoring, control picks, dedup storage."""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import structlog

from scout.db import Database
from scout.narrative.models import CategoryAcceleration, LaggardToken
from scout.narrative.prompts import NARRATIVE_FIT_SYSTEM, NARRATIVE_FIT_TEMPLATE
from scout.ratelimit import coingecko_limiter

log = structlog.get_logger()

CG_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"


# ------------------------------------------------------------------
# 1. fetch_laggards
# ------------------------------------------------------------------


async def fetch_laggards(
    session: aiohttp.ClientSession,
    category_id: str,
    api_key: str = "",
) -> list[dict]:
    """Fetch coins in a CoinGecko category sorted by market cap descending.

    Returns [] on any HTTP error (including 429).
    """
    params = {
        "vs_currency": "usd",
        "category": category_id,
        "order": "market_cap_desc",
        "per_page": "100",
        "sparkline": "false",
    }
    headers: dict[str, str] = {}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key
    await coingecko_limiter.acquire()
    try:
        async with session.get(CG_MARKETS_URL, params=params, headers=headers) as resp:
            if resp.status == 429:
                log.warning(
                    "fetch_laggards_rate_limited",
                    category_id=category_id,
                )
                await coingecko_limiter.report_429()
                return []
            if resp.status != 200:
                log.warning(
                    "fetch_laggards_error",
                    category_id=category_id,
                    status=resp.status,
                )
                return []
            data = await resp.json()
            result = data if isinstance(data, list) else []
            return result
    except Exception:
        log.exception("fetch_laggards_exception", category_id=category_id)
        return []


# ------------------------------------------------------------------
# 2. filter_laggards
# ------------------------------------------------------------------


def filter_laggards(
    tokens: list[dict],
    category_id: str,
    category_name: str,
    max_mcap: float,
    max_change: float,
    min_change: float,
    min_volume: float,
) -> list[LaggardToken]:
    """Filter raw CoinGecko market entries by thresholds.

    Sort by price_change_24h ascending (most behind first),
    tie-breaker: volume_24h / market_cap descending.
    """
    result: list[LaggardToken] = []
    for t in tokens:
        try:
            mcap = float(t.get("market_cap") or 0)
            change = float(t.get("price_change_percentage_24h") or 0)
            vol = float(t.get("total_volume") or 0)
            price = float(t.get("current_price") or 0)
            coin_id = t.get("id", "")
            symbol = t.get("symbol", "")
            name = t.get("name", "")
            if not coin_id:
                continue
        except (TypeError, ValueError):
            continue

        if mcap > max_mcap or mcap <= 0:
            continue
        if change > max_change or change < min_change:
            continue
        if vol < min_volume:
            continue

        result.append(
            LaggardToken(
                coin_id=coin_id,
                symbol=symbol,
                name=name,
                market_cap=mcap,
                price=price,
                price_change_24h=change,
                volume_24h=vol,
                category_id=category_id,
                category_name=category_name,
            )
        )

    # Sort: most negative change first; tie-break by vol/mcap descending
    result.sort(
        key=lambda tok: (
            tok.price_change_24h,
            -(tok.volume_24h / max(tok.market_cap, 1)),
        )
    )
    return result


# ------------------------------------------------------------------
# 3. partition_and_select
# ------------------------------------------------------------------


def partition_and_select(
    laggards: list[LaggardToken], max_picks: int
) -> tuple[list[LaggardToken], list[LaggardToken]]:
    """Randomly shuffle laggards, take first max_picks as scored, next as control.

    Returns (scored, control) where scored is sorted by price_change_24h
    for presentation. Both groups are random samples to avoid selection bias.
    """
    shuffled = list(laggards)
    random.shuffle(shuffled)
    scored = shuffled[:max_picks]
    control = shuffled[max_picks : max_picks * 2]
    # Sort scored group by price_change_24h for presentation
    scored.sort(key=lambda tok: tok.price_change_24h)
    return scored, control


# ------------------------------------------------------------------
# 4. build_scoring_prompt
# ------------------------------------------------------------------


def build_scoring_prompt(
    token: LaggardToken,
    accel: CategoryAcceleration,
    market_regime: str,
    top_3_coins: str,
    lessons_appendix: str,
) -> str:
    """Build the user prompt for Claude narrative-fit scoring."""
    vol_mcap_ratio = token.volume_24h / max(token.market_cap, 1)
    return NARRATIVE_FIT_TEMPLATE.format(
        category_name=accel.name,
        mcap_change=accel.current_velocity,
        acceleration=accel.acceleration,
        volume=accel.volume_24h,
        vol_growth=accel.volume_growth_pct,
        top_3_coins=top_3_coins,
        token_name=token.name,
        symbol=token.symbol,
        market_cap=token.market_cap,
        price_change_24h=token.price_change_24h,
        market_regime=market_regime,
        coin_count_change=accel.coin_count_change,
        vol_mcap_ratio=vol_mcap_ratio,
        lessons_appendix=lessons_appendix,
    )


# ------------------------------------------------------------------
# 5. parse_scoring_response
# ------------------------------------------------------------------


def parse_scoring_response(text: str) -> dict:
    """Extract JSON from Claude response, handling optional markdown fences."""
    # Try to extract from ```json ... ``` block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        text = match.group(1)
    return json.loads(text.strip())


# ------------------------------------------------------------------
# 6. score_token
# ------------------------------------------------------------------


async def score_token(
    token: LaggardToken,
    accel: CategoryAcceleration,
    market_regime: str,
    top_3_coins: str,
    lessons: str,
    api_key: str,
    model: str,
    client: object | None = None,
) -> dict | None:
    """Call Claude to score a single token's narrative fit.

    Returns parsed dict or None on any error.
    """
    try:
        import anthropic

        if client is None:
            client = anthropic.Anthropic(api_key=api_key)

        prompt = build_scoring_prompt(token, accel, market_regime, top_3_coins, lessons)
        response = client.messages.create(  # type: ignore[union-attr]
            model=model,
            max_tokens=300,
            temperature=0,
            system=NARRATIVE_FIT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text  # type: ignore[index]
        return parse_scoring_response(raw)
    except Exception:
        log.exception("score_token_error", coin_id=token.coin_id, symbol=token.symbol)
        return None


# ------------------------------------------------------------------
# 7. is_cooling_down
# ------------------------------------------------------------------


async def is_cooling_down(db: Database, category_id: str) -> bool:
    """Check if category has an active signal still in cooldown."""
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")
    now = datetime.now(timezone.utc).isoformat()
    cursor = await conn.execute(
        """SELECT COUNT(*) FROM narrative_signals
           WHERE category_id = ? AND cooling_down_until > ?""",
        (category_id, now),
    )
    row = await cursor.fetchone()
    return (row[0] > 0) if row else False


# ------------------------------------------------------------------
# 8. record_signal
# ------------------------------------------------------------------


async def record_signal(
    db: Database,
    category_id: str,
    category_name: str,
    acceleration: float,
    volume_growth_pct: float,
    coin_count_change: int | None,
    cooldown_hours: int,
) -> int:
    """Record or increment a narrative signal for a category.

    If an active signal (cooling_down_until > now) exists, increment its
    trigger_count and return the new count. Otherwise insert a new signal
    with trigger_count=1.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # Check for existing active signal
    cursor = await conn.execute(
        """SELECT id, trigger_count FROM narrative_signals
           WHERE category_id = ? AND cooling_down_until > ?
           ORDER BY id DESC LIMIT 1""",
        (category_id, now_iso),
    )
    row = await cursor.fetchone()
    if row:
        new_count = row[1] + 1
        await conn.execute(
            "UPDATE narrative_signals SET trigger_count = ? WHERE id = ?",
            (new_count, row[0]),
        )
        await conn.commit()
        return new_count

    # Insert new signal
    cooldown_until = now + timedelta(hours=cooldown_hours)
    await conn.execute(
        """INSERT INTO narrative_signals
           (category_id, category_name, acceleration, volume_growth_pct,
            coin_count_change, trigger_count, detected_at, cooling_down_until)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
        (
            category_id,
            category_name,
            acceleration,
            volume_growth_pct,
            coin_count_change,
            now_iso,
            cooldown_until.isoformat(),
        ),
    )
    await conn.commit()
    return 1


# ------------------------------------------------------------------
# 9. store_predictions
# ------------------------------------------------------------------


async def store_predictions(db: Database, predictions: list[dict]) -> None:
    """INSERT OR IGNORE each prediction into the predictions table.

    Serialises strategy_snapshot and strategy_snapshot_ab as JSON strings.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    for p in predictions:
        strategy_snap = json.dumps(p.get("strategy_snapshot", {}))
        strategy_snap_ab = (
            json.dumps(p["strategy_snapshot_ab"])
            if p.get("strategy_snapshot_ab") is not None
            else None
        )
        await conn.execute(
            """INSERT OR IGNORE INTO predictions
               (category_id, category_name, coin_id, symbol, name,
                market_cap_at_prediction, price_at_prediction,
                narrative_fit_score, staying_power, confidence, reasoning,
                market_regime, trigger_count, is_control, is_holdout,
                strategy_snapshot, strategy_snapshot_ab, predicted_at,
                counter_risk_score, counter_flags, counter_argument,
                counter_data_completeness, counter_scored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?)""",
            (
                p["category_id"],
                p["category_name"],
                p["coin_id"],
                p["symbol"],
                p["name"],
                p["market_cap_at_prediction"],
                p["price_at_prediction"],
                p["narrative_fit_score"],
                p["staying_power"],
                p["confidence"],
                p["reasoning"],
                p.get("market_regime"),
                p.get("trigger_count"),
                1 if p.get("is_control") else 0,
                1 if p.get("is_holdout") else 0,
                strategy_snap,
                strategy_snap_ab,
                p["predicted_at"],
                p.get("counter_risk_score"),
                p.get("counter_flags"),
                p.get("counter_argument"),
                p.get("counter_data_completeness"),
                p.get("counter_scored_at"),
            ),
        )
    await conn.commit()
