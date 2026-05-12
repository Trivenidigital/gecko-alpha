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

    logger.info(
        "held_position_refresh_summary",
        refreshed_count=len(raw_coins),
        skipped_contract_addr_count=skipped_contract_addr,
        not_found_count=len(cg_ids) - len(raw_coins),
        material_drift_count=material_drift_count,
        largest_drift_pct=(
            round(largest_drift_pct, 2) if largest_drift_pct is not None else None
        ),
        held_total=len(held_ids),
    )
    return raw_coins


def _reset_cycle_counter_for_tests() -> None:
    """Test-only helper. Production code never calls this."""
    global _cycle_counter
    _cycle_counter = 0
