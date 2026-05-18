"""Held-position price-refresh lane (§12c-narrow remediation).

Queries `paper_trades WHERE status='open'` every Nth pipeline cycle, batches a
single `/simple/price` call to CoinGecko for held token_ids, and emits raw-coin
dicts in `/coins/markets` shape compatible with `Database.cache_prices()`.

Why this lane exists: the existing ingestion lanes (CoinGecko markets/trending,
DexScreener, GeckoTerminal) only write to `price_cache` for tokens currently
in their respective active surfaces. Tokens that drop off all surfaces have
their cache rows freeze, and the trailing-stop evaluator silently can't fire
price-based exits on those positions. This lane closes that gap by refreshing
held tokens regardless of whether they appear elsewhere.

See `tasks/findings_open_position_price_freshness_2026_05_12.md` for the
empirical evidence and `tasks/plan_held_position_price_freshness.md` for the
design rationale.

Coverage caveat: this lane uses `/simple/price` (CoinGecko), so it covers
tokens with CG IDs. DEX-only-discovered tokens (contract-addr-shaped, no CG
listing) are skipped — tracked as follow-up BL-NEW-DEX-PRICE-COVERAGE.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp
import structlog

from scout.ingestion.coingecko import CG_BASE, _get_with_backoff

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.db import Database

logger = structlog.get_logger()

# Module-level cycle counter for cadence throttling. Increments every call
# to fetch_held_position_prices() regardless of whether refresh fires; refresh
# fires when (counter % HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES) == 0.
_cycle_counter: int = 0

# BL-NEW-HELD-POSITION-REFRESH-RATE-GAP (cycle 13): module-level dedup for
# the per-token persistent-stale WARN. Keyed by token_id, value = last warn
# UTC timestamp. 24h dedup window. In-memory by design — resets on
# pipeline restart so post-deploy/post-restart re-emits a fresh snapshot
# (operator wants "what's stale right now," not "what was stale before
# the last restart that we already alerted on but you forgot about").
# Pruned to ≥7d-old entries each refresh (R2 IMPORTANT 1 fold) to bound
# memory growth from stale entries of closed positions.
_warned_today: dict[str, datetime] = {}
_WARNED_PRUNE_AGE = timedelta(days=7)

# /simple/price accepts up to ~250 ids per call. Current production cohort
# is ~150 held positions, well within one batch. If the cohort grows past
# 250, batching logic below splits into multiple sequential calls.
SIMPLE_PRICE_BATCH_SIZE = 250


def _is_cg_coin_id(token_id: str | None) -> bool:
    """Heuristic: skip obvious contract addresses; pass everything else.

    CG coin_ids are lowercase alphanumeric + hyphens/underscores.
    Contract addresses look different:
      - EVM: starts with '0x', 40+ hex chars
      - Solana base58 mints: mixed case, 32-44 chars (e.g.,
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" — too short to be
        caught by a length>60 check; the mixed-case check catches it).

    Permissive on the CG side, strict on the obvious-contract side —
    false-negatives at CG just produce `not_found_in_cg` log entries and
    skip, not data corruption.
    """
    if not token_id:
        return False
    if token_id.startswith("0x"):
        return False
    if len(token_id) > 60:
        return False
    # CG coin_ids are lowercase; mixed-case is a strong signal of base58
    # (Solana mints, etc.). The triage script that produced the empirical
    # held cohort confirmed 0 false-skips of real CG ids under this rule.
    if token_id != token_id.lower():
        return False
    return all(c.isalnum() or c in "-_" for c in token_id)


async def _get_held_token_ids(db: "Database") -> list[str]:
    """Return distinct token_ids for currently-open paper_trades."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cursor = await db._conn.execute(
        "SELECT DISTINCT token_id FROM paper_trades WHERE status = 'open'"
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows if r[0]]


async def _get_held_trade_metadata(
    db: "Database", coin_ids: list[str]
) -> dict[str, tuple[int, str | None]]:
    """Return token_id → (paper_trade_id, symbol) for open paper_trades.

    Picks ONE trade per token_id arbitrarily (a token may back multiple
    concurrent paper_trades; the WARN only needs one identifier to anchor
    operator navigation). Symbols can be NULL in paper_trades, propagated
    as None.

    Used by the BL-NEW-HELD-POSITION-REFRESH-RATE-GAP per-token persistent-
    stale WARN payload (R3 I2 fold).
    """
    if db._conn is None or not coin_ids:
        return {}
    placeholders = ",".join("?" * len(coin_ids))
    sql = (
        f"SELECT token_id, MIN(id), MIN(symbol) FROM paper_trades "
        f"WHERE status = 'open' AND token_id IN ({placeholders}) "
        f"GROUP BY token_id"
    )
    cur = await db._conn.execute(sql, coin_ids)
    rows = await cur.fetchall()
    return {r[0]: (r[1], r[2]) for r in rows if r[0] and r[1] is not None}


async def _get_cached_price_ages(
    db: "Database", coin_ids: list[str]
) -> dict[str, datetime]:
    """Direct query of `price_cache.updated_at` for the given coin_ids.

    Returns tz-aware datetimes keyed by coin_id. Coins absent from the cache
    are absent from the returned dict (caller treats missing as "needs refresh").

    Avoids touching scout/db.py — the existing `Database.get_cached_prices`
    helper does NOT return updated_at, so a direct SQL hop is the minimum
    shape. Used by the BL-NEW-HELD-POSITION-REFRESH-RATE-GAP gauge + per-token
    persistent-stale WARN paths.
    """
    if db._conn is None or not coin_ids:
        return {}
    placeholders = ",".join("?" * len(coin_ids))
    sql = f"SELECT coin_id, updated_at FROM price_cache WHERE coin_id IN ({placeholders})"
    cur = await db._conn.execute(sql, coin_ids)
    rows = await cur.fetchall()
    return {
        r[0]: datetime.fromisoformat(r[1])
        for r in rows
        if r[1] is not None
    }


async def _fetch_simple_price_batch(
    session: aiohttp.ClientSession,
    settings: "Settings",
    coin_ids: list[str],
) -> dict:
    """Single batched /simple/price call. Returns CG response dict or {} on failure."""
    if not coin_ids:
        return {}
    params = {
        "ids": ",".join(coin_ids),
        "vs_currencies": "usd",
        "include_market_cap": "true",
        "include_24hr_change": "true",
    }
    if settings.COINGECKO_API_KEY:
        params["x_cg_demo_api_key"] = settings.COINGECKO_API_KEY
    result = await _get_with_backoff(session, f"{CG_BASE}/simple/price", params=params)
    if not isinstance(result, dict):
        return {}
    return result


def _shape_for_cache_prices(simple_price_response: dict) -> list[dict]:
    """Convert /simple/price response to /coins/markets-shaped dicts.

    /simple/price returns {coin_id: {usd, usd_market_cap, usd_24h_change}}.
    cache_prices() expects [{id, current_price, price_change_percentage_24h,
    price_change_percentage_7d_in_currency, market_cap}, ...].

    /simple/price does NOT include 7d change. The enhanced cache_prices()
    writer uses COALESCE on conflict so any existing 7d value is preserved.
    """
    out: list[dict] = []
    for coin_id, fields in simple_price_response.items():
        if not isinstance(fields, dict):
            continue
        out.append(
            {
                "id": coin_id,
                "current_price": fields.get("usd"),
                "price_change_percentage_24h": fields.get("usd_24h_change"),
                # price_change_percentage_7d_in_currency intentionally omitted —
                # cache_prices() COALESCE preserves existing.
                "market_cap": fields.get("usd_market_cap"),
            }
        )
    return out


async def fetch_held_position_prices(
    session: aiohttp.ClientSession,
    settings: "Settings",
    db: "Database",
) -> list[dict]:
    """Refresh price_cache for currently-held tokens.

    Returns raw-coin-dict-shaped list compatible with Database.cache_prices()
    (the caller in scout/main.py merges this into `all_raw` before the write).

    No-op (returns []) if disabled or off-cadence. The module-level cycle
    counter increments on every call regardless, so cadence throttling stays
    deterministic.
    """
    global _cycle_counter
    _cycle_counter += 1

    if not settings.HELD_POSITION_PRICE_REFRESH_ENABLED:
        return []

    interval = max(1, settings.HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES)
    if _cycle_counter % interval != 0:
        return []

    held_ids = await _get_held_token_ids(db)
    if not held_ids:
        logger.info(
            "held_position_refresh_summary",
            refreshed_count=0,
            skipped_contract_addr_count=0,
            reason="no_open_trades",
        )
        return []

    cg_ids: list[str] = []
    skipped_contract_addr = 0
    for tid in held_ids:
        if _is_cg_coin_id(tid):
            cg_ids.append(tid)
        else:
            skipped_contract_addr += 1

    if not cg_ids:
        logger.info(
            "held_position_refresh_summary",
            refreshed_count=0,
            skipped_contract_addr_count=skipped_contract_addr,
            reason="no_cg_format_ids",
        )
        return []

    # Batch /simple/price (max 250 ids per call).
    aggregated: dict = {}
    for i in range(0, len(cg_ids), SIMPLE_PRICE_BATCH_SIZE):
        batch = cg_ids[i : i + SIMPLE_PRICE_BATCH_SIZE]
        resp = await _fetch_simple_price_batch(session, settings, batch)
        aggregated.update(resp)

    raw_coins = _shape_for_cache_prices(aggregated)

    # Compute drift telemetry against existing cache rows (best-effort —
    # silent if get_cached_prices fails; this lane never blocks on telemetry).
    material_drift_count = 0
    largest_drift_pct: float | None = None
    try:
        existing = await db.get_cached_prices([c["id"] for c in raw_coins])
        for coin in raw_coins:
            new_price = coin.get("current_price")
            old_entry = existing.get(coin["id"])
            if new_price is None or not old_entry:
                continue
            old_price = old_entry.get("usd")
            if old_price is None or old_price == 0:
                continue
            drift_pct = ((new_price - old_price) / old_price) * 100.0
            if abs(drift_pct) > 10.0:
                material_drift_count += 1
            if largest_drift_pct is None or abs(drift_pct) > abs(largest_drift_pct):
                largest_drift_pct = drift_pct
    except Exception:
        logger.exception("held_position_refresh_drift_telemetry_failed")

    # BL-NEW-HELD-POSITION-REFRESH-RATE-GAP (cycle 13): visibility block —
    # stale-open gauge + per-token persistent-stale WARN. Refactored per
    # PR-#158 R3-NIT fold: pre-fetch helpers ONCE with explicit `{}` defaults
    # on failure (eliminates the brittle `locals()` re-fetch); both
    # downstream sub-blocks become pure consumers wrapped in own try/except.
    ages_for_held: dict[str, datetime] = {}
    held_metadata: dict[str, tuple[int, str | None]] = {}
    ages_query_succeeded = False
    try:
        ages_for_held = await _get_cached_price_ages(db, held_ids)
        ages_query_succeeded = True
    except Exception:
        logger.exception("held_position_stale_visibility_ages_query_failed")
    try:
        held_metadata = await _get_held_trade_metadata(db, held_ids)
    except Exception:
        logger.exception("held_position_stale_visibility_metadata_query_failed")

    # PR-#158 R1-C1 fold: surface the specific token_ids `/simple/price`
    # didn't return so post-deploy data discriminates "stale source" vs
    # "rate-limit batch truncation" vs "ID-mismatch in CG." Capped at 25
    # to bound log line size.
    raw_coin_ids = {c["id"] for c in raw_coins}
    simple_price_missing_ids: list[str] = [
        cid for cid in cg_ids if cid not in raw_coin_ids
    ][:25]

    stale_open_count: int | None = None
    stale_open_pct: float | None = None
    # Only emit gauge when we actually have the data — a failed ages query
    # would otherwise be indistinguishable from "everything stale" (every
    # held_id missing from the empty fallback dict). False-alarm-safe.
    if ages_query_succeeded:
        try:
            now_utc = datetime.now(timezone.utc)
            stale_threshold_hours = 24
            stale_count = 0
            for tid in held_ids:
                age = ages_for_held.get(tid)
                if age is None:
                    stale_count += 1
                    continue
                if (now_utc - age).total_seconds() / 3600 > stale_threshold_hours:
                    stale_count += 1
            stale_open_count = stale_count
            if held_ids:
                stale_open_pct = round(100.0 * stale_count / len(held_ids), 1)
        except Exception:
            logger.exception("held_position_stale_count_failed")

    # Per-token WARN with 24h dedup. R3 I2 fold: include paper_trade_id +
    # symbol + the load-bearing consequence so an operator reading the
    # alert at 3am knows what's actually broken downstream (trailing-stop
    # evaluator can't fire price exits on these positions). R2 IMPORTANT 1
    # fold: prune dict entries older than 7d to bound memory growth.
    #
    # Gated on ages_query_succeeded same as the gauge: if the query failed,
    # the empty fallback dict would make every token look stale and trigger
    # one WARN per held position (alert-noise hazard).
    if ages_query_succeeded:
        try:
            now_utc = datetime.now(timezone.utc)
            threshold_hours = settings.HELD_POSITION_STALE_WARN_HOURS
            dedup_cutoff = now_utc - timedelta(hours=24)
            prune_cutoff = now_utc - _WARNED_PRUNE_AGE
            for stale_tid in list(_warned_today.keys()):
                if _warned_today[stale_tid] < prune_cutoff:
                    del _warned_today[stale_tid]
            for tid in held_ids:
                age = ages_for_held.get(tid)
                if age is None:
                    cache_age_hours: float | None = None
                    is_stale = True
                else:
                    cache_age_hours = (now_utc - age).total_seconds() / 3600
                    is_stale = cache_age_hours >= threshold_hours
                if not is_stale:
                    continue
                last_warn = _warned_today.get(tid)
                if last_warn is not None and last_warn > dedup_cutoff:
                    continue
                meta = held_metadata.get(tid, (None, None))
                logger.warning(
                    "held_position_token_persistently_stale",
                    token_id=tid,
                    paper_trade_id=meta[0],
                    symbol=meta[1],
                    cache_age_hours=(
                        round(cache_age_hours, 1) if cache_age_hours is not None else None
                    ),
                    cache_last=age.isoformat() if age is not None else None,
                    warn_threshold_hours=threshold_hours,
                    consequence="trailing_stop_evaluator_cannot_fire_price_exits",
                )
                _warned_today[tid] = now_utc
        except Exception:
            logger.exception("held_position_persistent_stale_warn_failed")

    logger.info(
        "held_position_refresh_summary",
        refreshed_count=len(raw_coins),
        skipped_contract_addr_count=skipped_contract_addr,
        not_found_count=len(cg_ids) - len(raw_coins),
        simple_price_missing_ids=simple_price_missing_ids,
        material_drift_count=material_drift_count,
        largest_drift_pct=(
            round(largest_drift_pct, 2) if largest_drift_pct is not None else None
        ),
        held_total=len(held_ids),
        stale_open_count=stale_open_count,
        stale_open_pct=stale_open_pct,
    )
    return raw_coins


def _reset_cycle_counter_for_tests() -> None:
    """Test-only helper. Production code never calls this."""
    global _cycle_counter
    _cycle_counter = 0


def _reset_warned_today_for_tests() -> None:
    """Test-only helper. Mirrors _reset_cycle_counter_for_tests pattern."""
    global _warned_today
    _warned_today = {}
