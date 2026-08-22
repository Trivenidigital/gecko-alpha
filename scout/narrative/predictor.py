"""PREDICT phase — laggard selection, Claude scoring, control picks, dedup storage."""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import structlog

from scout import cg_api
from scout.db import Database
from scout.narrative.models import CategoryAcceleration, LaggardToken
from scout.narrative.prompts import NARRATIVE_FIT_SYSTEM, NARRATIVE_FIT_TEMPLATE
from scout.coingecko_budget import BUCKET_DISCOVERY, governed_cg_call
from scout.ratelimit import coingecko_limiter

log = structlog.get_logger()


# ------------------------------------------------------------------
# 1. fetch_laggards
# ------------------------------------------------------------------


async def fetch_laggards(
    session: aiohttp.ClientSession,
    category_id: str,
    api_key: str = "",
    api_tier: str = "demo",
    settings=None,
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
        "price_change_percentage": "24h,7d",
    }
    headers: dict[str, str] = {}
    headers.update(cg_api.auth_headers(api_key, api_tier))
    for attempt in range(2):  # 1 retry on 429
        # Governed: this lane owns its retry loop so it cannot use
        # _get_with_backoff, but it must still be counted and refusable.
        # DISCOVERY bucket (non-critical) so it can never eat the reserve
        # that keeps open positions re-priceable.
        _call = governed_cg_call(BUCKET_DISCOVERY, settings)
        if not _call.allowed:
            return []
        # finish(None) is guaranteed below so a CONNECTION/TIMEOUT failure —
        # which never reaches a response and so never reaches finish(status) —
        # still records one attempt with zero credits. Counting only the
        # request paths that produced a response makes a lane that is failing
        # at the transport layer invisible in the attempt rate.
        await coingecko_limiter.acquire()
        try:
            async with session.get(
                f"{cg_api.base_url(api_tier)}/coins/markets",
                params=params,
                headers=headers,
            ) as resp:
                _call.finish(resp.status)
                if resp.status == 429:
                    log.warning(
                        "fetch_laggards_rate_limited",
                        category_id=category_id,
                        attempt=attempt,
                    )
                    await coingecko_limiter.report_429()
                    if attempt == 0:
                        await asyncio.sleep(2)
                        continue
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
    return []  # pragma: no cover


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
    # narrative_prediction-fix arch-A1: defense-in-depth filter at fetch
    # time. _is_tradeable_candidate rejects empty/whitespace IDs, junk
    # prefixes (test-, wrapped-, bridged-), and non-ASCII tickers BEFORE
    # the prediction is stored. Catches the upstream class of bad data
    # so the dispatcher gate at signals.py:trade_predictions only has to
    # handle the harder "passes upstream but absent from our snapshot
    # tables" case.
    from scout.trading.filters import _is_tradeable_candidate

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
            if not _is_tradeable_candidate(coin_id, symbol):
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
    watchlist_users: int = 0,
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
        watchlist_users=watchlist_users,
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
    watchlist_users: int = 0,
) -> dict | None:
    """Call Claude to score a single token's narrative fit.

    Returns parsed dict or None on any error.

    Failure behaviour is deliberately unchanged: ANY exception yields ``None``,
    the caller counts it toward the 3-consecutive-failure breaker, and control
    rows keep being stored. What changed is that the failure now says WHY.

    The previous handler emitted ``score_token_error`` with only ``coin_id`` and
    ``symbol``. ``structlog``'s JSONRenderer is configured without
    ``format_exc_info``, so ``log.exception`` rendered no traceback at all —
    billing, auth, rate-limit, model-rejection and response-parsing failures
    were indistinguishable from one another. 63 such events in two days carried
    zero diagnostic content between them, and scored output had decayed to zero
    while control rows continued, so nothing looked broken from the outside.

    ``failing_step`` distinguishes a provider failure from a parse failure —
    the one split the classifier cannot make on its own, since a malformed
    response raises inside our code, not the SDK's.

    Secret-safe: the scoring prompt, the system prompt, the API key and the raw
    response are never logged. The message and traceback pass through
    ``redact`` / ``safe_traceback``, which strip credential shapes and bound
    length.
    """
    step = "build_client"
    try:
        import anthropic

        if client is None:
            client = anthropic.AsyncAnthropic(api_key=api_key)

        step = "build_prompt"
        prompt = build_scoring_prompt(
            token,
            accel,
            market_regime,
            top_3_coins,
            lessons,
            watchlist_users=watchlist_users,
        )
        step = "provider_call"
        response = await client.messages.create(  # type: ignore[union-attr]
            model=model,
            max_tokens=300,
            temperature=0,
            system=NARRATIVE_FIT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        step = "parse_response"
        raw = response.content[0].text  # type: ignore[index]
        return parse_scoring_response(raw)
    except Exception as exc:
        from scout.narrative.learn_outcome import (
            LearnOutcome,
            ProviderHealth,
            classify_provider_error,
            redact,
            safe_traceback,
        )

        # The outcome must agree with `failing_step`. `classify_provider_error`
        # falls back to FAILED_PROVIDER/UNKNOWN for anything it does not
        # recognise, so running EVERY exception through it would label a
        # malformed-response failure `FAILED_PROVIDER` while `failing_step` said
        # `parse_response` — two fields contradicting each other in the same
        # event, which is worse than the silence this handler replaced.
        if step == "provider_call":
            outcome, health = classify_provider_error(exc)
        elif step == "parse_response":
            # A response was received and decoded far enough to reach parsing,
            # so the provider demonstrably worked. The fault is ours.
            outcome, health = LearnOutcome.FAILED_PARSING, ProviderHealth.AVAILABLE
        else:
            # build_client / build_prompt: the provider was never contacted, so
            # its health is genuinely unknown rather than healthy or degraded.
            outcome, health = LearnOutcome.FAILED_INTERNAL, ProviderHealth.UNKNOWN
        log.error(
            "score_token_error",
            coin_id=token.coin_id,
            symbol=token.symbol,
            failing_step=step,
            exception_type=type(exc).__name__,
            exception_message=redact(str(exc)),
            status_code=getattr(exc, "status_code", None),
            provider_request_id=getattr(exc, "request_id", None),
            provider_health=health.value,
            outcome=outcome.value,
            model_identifier=model,
            traceback=safe_traceback(exc),
        )
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

    # Note: check-then-insert is not strictly atomic, but asyncio's single-threaded
    # event loop prevents true concurrent execution, making TOCTOU a non-issue here.
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
                counter_data_completeness, counter_scored_at, watchlist_users)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?)""",
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
                p.get("watchlist_users"),
            ),
        )
    await conn.commit()
