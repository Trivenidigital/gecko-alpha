"""EVALUATE phase — multi-checkpoint outcome tracking with peak price monitoring.

Evaluates pending predictions at 6h, 24h, and 48h checkpoints, classifying
each as HIT / MISS / NEUTRAL based on strategy thresholds.
"""

import json
from datetime import datetime, timedelta, timezone

import aiohttp
import structlog

from scout.db import Database
from scout.narrative.strategy import Strategy
from scout.ratelimit import coingecko_limiter

log = structlog.get_logger()

# CoinGecko /coins/markets supports max 250 IDs per request.
_BATCH_SIZE = 250


def classify_checkpoint(change_pct: float, hit: float, miss: float) -> str:
    """Classify a price change as HIT, MISS, or NEUTRAL.

    Args:
        change_pct: Percentage change from prediction price.
        hit: Threshold at or above which the change is a HIT.
        miss: Threshold at or below which the change is a MISS.

    Returns:
        "HIT", "MISS", or "NEUTRAL".
    """
    if change_pct >= hit:
        return "HIT"
    if change_pct <= miss:
        return "MISS"
    return "NEUTRAL"


def pick_final_class(
    cls_6h: str | None, cls_24h: str | None, cls_48h: str | None
) -> str | None:
    """Return the final outcome class based on the 48-hour checkpoint.

    The 48h checkpoint is the definitive verdict. Returns None if 48h
    has not been evaluated yet.
    """
    return cls_48h


async def fetch_prices_batch(
    session: aiohttp.ClientSession,
    coin_ids: list[str],
    api_key: str = "",
) -> dict[str, float]:
    """Fetch current USD prices for a list of CoinGecko coin IDs.

    Batches requests in groups of 250 (CoinGecko per_page limit).
    On 429 or network error, logs a warning and returns partial results.

    Args:
        session: aiohttp client session.
        coin_ids: List of CoinGecko coin identifiers.
        api_key: Optional CoinGecko Demo API key.

    Returns:
        Mapping of coin_id to current USD price.
    """
    prices: dict[str, float] = {}
    if not coin_ids:
        return prices

    headers: dict[str, str] = {}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key

    for i in range(0, len(coin_ids), _BATCH_SIZE):
        batch = coin_ids[i : i + _BATCH_SIZE]
        ids_param = ",".join(batch)
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "ids": ids_param,
            "per_page": str(_BATCH_SIZE),
        }
        await coingecko_limiter.acquire()
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 429:
                    log.warning(
                        "coingecko_rate_limited",
                        batch_start=i,
                        batch_size=len(batch),
                    )
                    await coingecko_limiter.report_429()
                    continue
                if resp.status != 200:
                    log.warning(
                        "coingecko_prices_error",
                        status=resp.status,
                        batch_start=i,
                    )
                    continue
                data = await resp.json()
                for coin in data:
                    cid = coin.get("id")
                    price = coin.get("current_price")
                    if cid and price is not None:
                        prices[cid] = float(price)
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("coingecko_prices_fetch_error", error=str(exc), batch_start=i)
            continue

    return prices


async def evaluate_pending(
    session: aiohttp.ClientSession,
    db: Database,
    strategy: Strategy,
    api_key: str = "",
) -> None:
    """Evaluate all pending predictions against current prices.

    For each unevaluated prediction, checks whether 6h / 24h / 48h
    checkpoints have elapsed and classifies them.  Tracks peak price
    across all evaluation passes.

    Args:
        session: aiohttp client session for price fetching.
        db: Initialised Database instance.
        strategy: Strategy instance with hit/miss thresholds.
        api_key: Optional CoinGecko Demo API key.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    hit_pct = float(strategy.get("hit_threshold_pct"))  # type: ignore[arg-type]
    miss_pct = float(strategy.get("miss_threshold_pct"))  # type: ignore[arg-type]

    # Fetch all pending predictions (outcome_class IS NULL)
    cursor = await conn.execute(
        """SELECT id, coin_id, price_at_prediction, predicted_at,
                  outcome_6h_class, outcome_24h_class, outcome_48h_class,
                  peak_price, peak_change_pct, peak_at,
                  eval_retry_count
           FROM predictions
           WHERE outcome_class IS NULL"""
    )
    rows = await cursor.fetchall()
    if not rows:
        return

    # Collect unique coin IDs and batch-fetch prices
    unique_ids = list({row[1] for row in rows})
    prices = await fetch_prices_batch(session, unique_ids, api_key=api_key)

    now = datetime.now(timezone.utc)

    for row in rows:
        pred_id = row[0]
        coin_id = row[1]
        price_at_pred = float(row[2])
        predicted_at = datetime.fromisoformat(str(row[3])).replace(tzinfo=timezone.utc)
        cls_6h = row[4]
        cls_24h = row[5]
        cls_48h = row[6]
        peak_price = float(row[7]) if row[7] is not None else None
        peak_change_pct = float(row[8]) if row[8] is not None else None
        peak_at_raw = row[9]
        retry_count = int(row[10]) if row[10] is not None else 0

        current_price = prices.get(coin_id)

        # --- Guard against division by zero ---
        if price_at_pred <= 0:
            continue

        # --- Price unavailable handling ---
        if current_price is None:
            retry_count += 1
            if retry_count >= 3:
                await conn.execute(
                    """UPDATE predictions
                       SET outcome_class = 'UNRESOLVED',
                           outcome_reason = 'price_unavailable',
                           eval_retry_count = ?,
                           evaluated_at = ?
                       WHERE id = ?""",
                    (retry_count, now.isoformat(), pred_id),
                )
            else:
                await conn.execute(
                    "UPDATE predictions SET eval_retry_count = ? WHERE id = ?",
                    (retry_count, pred_id),
                )
            continue

        # --- Peak tracking ---
        reference = peak_price if peak_price is not None else price_at_pred
        if current_price > reference:
            peak_price = current_price
            peak_change_pct = ((current_price - price_at_pred) / price_at_pred) * 100
            peak_at_raw = now.isoformat()
            await conn.execute(
                """UPDATE predictions
                   SET peak_price = ?, peak_change_pct = ?, peak_at = ?
                   WHERE id = ?""",
                (peak_price, peak_change_pct, peak_at_raw, pred_id),
            )

        # --- Checkpoint evaluation ---
        elapsed = now - predicted_at
        change_pct = ((current_price - price_at_pred) / price_at_pred) * 100

        updates: dict[str, object] = {}

        # 6h checkpoint
        if cls_6h is None and elapsed >= timedelta(hours=6):
            cls_6h = classify_checkpoint(change_pct, hit_pct, miss_pct)
            updates["outcome_6h_price"] = current_price
            updates["outcome_6h_change_pct"] = round(change_pct, 4)
            updates["outcome_6h_class"] = cls_6h

        # 24h checkpoint
        if cls_24h is None and elapsed >= timedelta(hours=24):
            cls_24h = classify_checkpoint(change_pct, hit_pct, miss_pct)
            updates["outcome_24h_price"] = current_price
            updates["outcome_24h_change_pct"] = round(change_pct, 4)
            updates["outcome_24h_class"] = cls_24h

        # 48h checkpoint (final)
        if cls_48h is None and elapsed >= timedelta(hours=48):
            cls_48h = classify_checkpoint(change_pct, hit_pct, miss_pct)
            updates["outcome_48h_price"] = current_price
            updates["outcome_48h_change_pct"] = round(change_pct, 4)
            updates["outcome_48h_class"] = cls_48h
            updates["outcome_class"] = pick_final_class(cls_6h, cls_24h, cls_48h)
            updates["evaluated_at"] = now.isoformat()

        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [pred_id]
            await conn.execute(
                f"UPDATE predictions SET {set_clause} WHERE id = ?",
                values,
            )

    await conn.commit()
