#!/usr/bin/env python3
"""Phase 1a-ii: liquidity-enrichment cron writer.

Reads ``candidates`` rows that need enrichment (no
``liquidity_enriched_at``, or older than ``LIQUIDITY_ENRICHMENT_TTL_SEC``)
and populates the 4 Phase 1a-i columns by resolving via:

  1. If ``contract_address`` is ``dex:<chain>:<address>`` shape — parse
     and call DexScreener ``/tokens/v1/{chain}/{address}`` directly.
  2. Else (treated as CoinGecko slug) — call CG ``/coins/{id}`` with
     market_data/community_data/tickers disabled, extract the
     ``platforms`` mapping, then call DexScreener per resolved (chain,
     address) pair. Confidence ``multi_chain`` when more than one
     chain resolves; the highest-liquidity match wins.

**Deterministic resolution only.** Never calls the DexScreener symbol-
fuzzy lookup endpoint — symbol-fuzzy resolution is structurally banned
per the design (operator guardrail #3). The boundary is mechanically
enforced via the substring scanner in ``tests`` for the cron path.

**Fail-soft per token.** One bad token cannot abort the batch — each
row's resolution is wrapped in ``try/except``; on exception, the row is
skipped without clobbering its existing ``liquidity_enriched_at``.

**Killswitch.** ``LIQUIDITY_ENRICHMENT_ENABLED=False`` in ``.env`` halts
all work BEFORE the DB is opened. Heartbeat is NOT touched when
killswitch is off — the watchdog reads the operator-supplied
``--killswitch-disabled`` flag separately and suppresses staleness
alerts when the killswitch is intentional.

**Rate budget.** CG calls share the global ``coingecko_limiter``
(scout.ratelimit) — bounded by ``COINGECKO_RATE_LIMIT_PER_MIN`` minus
the existing ingest budget. Per-tick batch is capped by
``LIQUIDITY_BACKFILL_BATCH_MAX`` (default 50). DexScreener calls use the
documented 300 req/min public quota; exponential backoff on 429 / 5xx.

**Heartbeat.** Written to ``--heartbeat-file`` at the END of every
successful tick, INCLUDING no-work ticks (TTL filtered all rows). The
watchdog's writer-side branch consumes this file's mtime to detect
silent cron-stall (CLAUDE.md §12a).

Exit codes:
  0 — tick completed (work or no-work; heartbeat touched)
  1 — fatal error (DB open failure, heartbeat write failure)
  2 — killswitch off (no work performed; heartbeat NOT touched)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import aiosqlite
import structlog

from scout.config import Settings
from scout.ratelimit import coingecko_limiter, configure_from_settings

logger = structlog.get_logger()

CG_BASE = "https://api.coingecko.com/api/v3"
DEX_BASE = "https://api.dexscreener.com/tokens/v1"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
DEX_MAX_RETRIES = 3

# CoinGecko's ``platforms`` field uses CG-flavoured chain identifiers
# (``binance-smart-chain``, ``polygon-pos``, ``optimistic-ethereum`` etc.);
# DexScreener's ``/tokens/v1/{chain}/{address}`` expects its own chain
# slugs. This mapping is the source of truth for the cron resolution
# path. Chains absent from the mapping resolve to ``dex_no_match``
# (the row is marked unenrichable rather than guessed) — operator
# guardrail #3: deterministic resolution only.
#
# Extend this dict (and add a unit test) when adding new chains.
CG_PLATFORM_TO_DEX_CHAIN: dict[str, str] = {
    "ethereum": "ethereum",
    "solana": "solana",
    "base": "base",
    "binance-smart-chain": "bsc",
    "polygon-pos": "polygon",
    "arbitrum-one": "arbitrum",
    "optimistic-ethereum": "optimism",
    "avalanche": "avalanche",
    "fantom": "fantom",
    "the-open-network": "ton",
    "hyperliquid": "hyperevm",
}


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _parse_dex_prefix(contract_address: str) -> tuple[str, str] | None:
    """Return ``(chain, address)`` if ``contract_address`` matches the
    ``dex:<chain>:<address>`` shortcut shape, else ``None``.

    Some signal_types (e.g. ``tg_social``) carry on-chain addresses
    already; this shortcut bypasses the CG hop entirely.
    """
    if not contract_address.startswith("dex:"):
        return None
    parts = contract_address.split(":", 2)
    if len(parts) != 3:
        return None
    chain, address = parts[1], parts[2]
    if not chain or not address:
        return None
    return chain, address


async def resolve_cg_slug_to_platforms(
    session: aiohttp.ClientSession, slug: str
) -> dict[str, str] | None:
    """Call CG ``/coins/{id}`` and return its ``platforms`` mapping.

    Returns ``None`` on HTTP error (404 = slug not in CG, 429 = backoff,
    5xx, network error). Returns ``{}`` on successful response with
    empty/missing ``platforms`` (operator visibility: row is genuinely
    not on-chain per CG's knowledge).

    Disables every optional payload section (``market_data``,
    ``community_data``, ``developer_data``, ``tickers``, ``sparkline``,
    ``localization``) to minimize bandwidth — only the small platforms
    block is needed.
    """
    # Check backoff BEFORE acquire so a known-throttled state doesn't
    # waste a rate-budget slot. (Reviewer fold: previous order acquired
    # the slot then no-op'd; harmless but less considerate to the
    # shared CG budget.)
    if coingecko_limiter.is_backing_off():
        return None
    await coingecko_limiter.acquire()
    url = f"{CG_BASE}/coins/{slug}"
    params = {
        "localization": "false",
        "tickers": "false",
        "market_data": "false",
        "community_data": "false",
        "developer_data": "false",
        "sparkline": "false",
    }
    try:
        async with session.get(
            url, params=params, timeout=REQUEST_TIMEOUT
        ) as resp:
            if resp.status == 429:
                await coingecko_limiter.report_429()
                return None
            if resp.status == 404:
                # Slug not in CG anymore — row is genuinely unresolvable.
                return {}
            if resp.status >= 400:
                logger.warning(
                    "liquidity_enrichment_cg_http_error",
                    status=resp.status,
                    slug=slug,
                )
                return None
            data = await resp.json()
    except Exception as exc:
        logger.warning(
            "liquidity_enrichment_cg_request_error",
            error=str(exc),
            slug=slug,
        )
        return None

    platforms = data.get("platforms") if isinstance(data, dict) else None
    if not isinstance(platforms, dict):
        return {}
    # Keep only string-address values; CG sometimes returns empty strings
    # for chains a token is "listed on" but not tradeable.
    return {k: v for k, v in platforms.items() if isinstance(v, str) and v}


async def resolve_dex_pair_liquidity(
    session: aiohttp.ClientSession, chain: str, address: str
) -> float | None:
    """Call DexScreener ``/tokens/v1/{chain}/{address}`` and return the
    HIGHEST-liquidity pair's ``liquidity.usd`` value (a single token
    often has multiple pools — the deepest one is the most actionable
    for trader sizing).

    Returns ``None`` on HTTP error / no pairs / all pairs zero
    liquidity. Exponential backoff on 429 / 5xx (matches existing
    ``scout/ingestion/dexscreener.py`` shape).
    """
    url = f"{DEX_BASE}/{chain}/{address}"
    for attempt in range(DEX_MAX_RETRIES):
        try:
            async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 429 or resp.status >= 500:
                    await asyncio.sleep(2**attempt)
                    continue
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception as exc:
            logger.warning(
                "liquidity_enrichment_dex_request_error",
                error=str(exc),
                chain=chain,
                address=address,
            )
            return None
        if not isinstance(data, list) or not data:
            return None
        best = 0.0
        for pair in data:
            if not isinstance(pair, dict):
                continue
            liq_block = pair.get("liquidity")
            if not isinstance(liq_block, dict):
                continue
            try:
                val = float(liq_block.get("usd") or 0)
            except (TypeError, ValueError):
                continue
            if val > best:
                best = val
        return best if best > 0 else None
    return None


async def resolve_row(
    session: aiohttp.ClientSession, contract_address: str
) -> tuple[float | None, str | None, str]:
    """Top-level row resolution.

    Returns ``(liquidity_usd, source, confidence)``. ``confidence`` is
    one of ``definite`` / ``multi_chain`` / ``cg_slug_unresolvable`` /
    ``dex_no_match``. The cron writer stores the timestamp regardless of
    confidence so the watchdog row-rate SLO counts the row as visited.

    When CG returns ``None`` (rate-limit backoff, network error), the
    caller treats this as a transient skip — the row's existing
    ``liquidity_enriched_at`` is NOT clobbered so the watchdog still
    sees the prior good run (matches ``feedback_resilience_layered_
    failure_modes.md`` discipline).
    """
    # Shortcut: dex:<chain>:<address> prefix bypasses CG hop.
    dex_parts = _parse_dex_prefix(contract_address)
    if dex_parts is not None:
        chain, address = dex_parts
        liquidity = await resolve_dex_pair_liquidity(session, chain, address)
        if liquidity is not None:
            return liquidity, "dexscreener_v1", "definite"
        return None, "dexscreener_v1", "dex_no_match"

    # CG slug → platforms mapping (deterministic resolution only;
    # symbol-fuzzy endpoints are banned by the static scanner).
    platforms = await resolve_cg_slug_to_platforms(session, contract_address)
    if platforms is None:
        # Transient — let caller skip without clobbering prior write.
        return None, None, ""
    if not platforms:
        return None, "dexscreener_v1", "cg_slug_unresolvable"

    # Translate CG platform names → DexScreener chain identifiers.
    chain_addr_pairs: list[tuple[str, str]] = []
    for cg_platform, address in platforms.items():
        dex_chain = CG_PLATFORM_TO_DEX_CHAIN.get(cg_platform)
        if dex_chain is None:
            continue
        chain_addr_pairs.append((dex_chain, address))

    if not chain_addr_pairs:
        # CG has the slug but on chains DexScreener does not cover via
        # this cron's mapping table (e.g., L2s we have not added yet).
        return None, "dexscreener_v1", "dex_no_match"

    matches: list[tuple[str, float]] = []
    for chain, address in chain_addr_pairs:
        liquidity = await resolve_dex_pair_liquidity(session, chain, address)
        if liquidity is not None:
            matches.append((chain, liquidity))

    if not matches:
        return None, "dexscreener_v1", "dex_no_match"

    # Pick the highest-liquidity match across resolved chains.
    _best_chain, best_liquidity = max(matches, key=lambda x: x[1])
    confidence = "multi_chain" if len(matches) > 1 else "definite"
    return best_liquidity, "dexscreener_v1", confidence


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def fetch_batch(
    conn: aiosqlite.Connection, *, ttl_sec: int, batch_max: int
) -> list[str]:
    """Return up to ``batch_max`` contract_addresses needing enrichment.

    NULL ``liquidity_enriched_at`` rows are prioritized (never visited);
    then rows with the oldest ``liquidity_enriched_at`` (re-enrichment
    pass once TTL expires).
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=ttl_sec)
    ).isoformat()
    cur = await conn.execute(
        "SELECT contract_address FROM candidates "
        "WHERE liquidity_enriched_at IS NULL "
        "   OR liquidity_enriched_at < ? "
        "ORDER BY liquidity_enriched_at IS NOT NULL, liquidity_enriched_at "
        "LIMIT ?",
        (cutoff, batch_max),
    )
    rows = await cur.fetchall()
    return [row[0] for row in rows]


async def write_enrichment(
    conn: aiosqlite.Connection,
    contract_address: str,
    liquidity_usd: float | None,
    source: str | None,
    confidence: str,
) -> None:
    """Write the 4 enrichment columns for one row. Commits per-row so
    fail-soft semantics don't lose successful writes from earlier in
    the batch on a later row's crash."""
    enriched_at = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "UPDATE candidates SET "
        "  liquidity_usd_enriched = ?, "
        "  liquidity_enriched_source = ?, "
        "  liquidity_enriched_at = ?, "
        "  liquidity_enriched_confidence = ? "
        "WHERE contract_address = ?",
        (liquidity_usd, source, enriched_at, confidence, contract_address),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def touch_heartbeat(heartbeat_path: Path) -> None:
    """Write the heartbeat file with the current UTC timestamp.

    Called at the END of every successful tick, including no-work
    ticks. The watchdog's writer-side branch reads this file's mtime
    to detect silent cron-stall.
    """
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.write_text(
        datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Main tick
# ---------------------------------------------------------------------------


async def run_tick(
    db_path: Path,
    heartbeat_path: Path,
    settings: Settings,
) -> tuple[int, int, int]:
    """Execute one cron tick.

    Returns ``(visited, enriched, errored)`` row counts for caller
    logging / metrics.
    """
    visited = 0
    enriched = 0
    errored = 0

    timeout = aiohttp.ClientTimeout(total=120, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        conn = await aiosqlite.connect(str(db_path))
        try:
            batch = await fetch_batch(
                conn,
                ttl_sec=settings.LIQUIDITY_ENRICHMENT_TTL_SEC,
                batch_max=settings.LIQUIDITY_BACKFILL_BATCH_MAX,
            )
            logger.info(
                "liquidity_enrichment_tick_started",
                batch_size=len(batch),
                batch_max=settings.LIQUIDITY_BACKFILL_BATCH_MAX,
            )
            for contract_address in batch:
                try:
                    liquidity, source, confidence = await resolve_row(
                        session, contract_address
                    )
                except Exception as exc:
                    # Fail-soft per token: one bad row does NOT abort
                    # the batch. Do NOT touch this row's enriched_at —
                    # it will be retried on the next tick.
                    errored += 1
                    logger.warning(
                        "liquidity_enrichment_row_error",
                        contract_address=contract_address,
                        error=str(exc),
                    )
                    continue
                visited += 1
                # Transient CG/DexScreener failure → confidence == "":
                # skip the write so the row retries next tick. The
                # operator's existing data is NOT clobbered (per
                # feedback_resilience_layered_failure_modes.md).
                if not confidence:
                    continue
                try:
                    await write_enrichment(
                        conn,
                        contract_address,
                        liquidity,
                        source,
                        confidence,
                    )
                    enriched += 1
                except Exception as exc:
                    errored += 1
                    logger.exception(
                        "liquidity_enrichment_write_error",
                        contract_address=contract_address,
                        error=str(exc),
                    )
                    continue
        finally:
            await conn.close()

    # Heartbeat touched on SUCCESSFUL tick completion, including
    # no-work ticks (batch empty). NOT touched if the killswitch is off
    # (handled in main()) or if an unhandled exception escapes run_tick.
    touch_heartbeat(heartbeat_path)
    logger.info(
        "liquidity_enrichment_tick_completed",
        visited=visited,
        enriched=enriched,
        errored=errored,
    )
    return visited, enriched, errored


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


async def amain(args: argparse.Namespace) -> int:
    settings = Settings()  # reads .env

    # Killswitch: halt before opening DB; do NOT touch heartbeat.
    # Watchdog reads --killswitch-disabled separately and suppresses
    # staleness alerts when the operator has intentionally disabled
    # the cron (design failure-mode table).
    if not settings.LIQUIDITY_ENRICHMENT_ENABLED:
        logger.info(
            "liquidity_enrichment_killswitch_off",
            note="LIQUIDITY_ENRICHMENT_ENABLED=False; tick skipped",
        )
        return 2

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        logger.error("liquidity_enrichment_db_not_found", db=str(db_path))
        return 1

    heartbeat_path = Path(args.heartbeat_file).expanduser()

    # Coordinate CG rate-limiter with the existing ingest budget. Idempotent.
    configure_from_settings(settings)

    started_at = time.monotonic()
    try:
        await run_tick(db_path, heartbeat_path, settings)
    except Exception as exc:
        logger.exception(
            "liquidity_enrichment_tick_failed", error=str(exc)
        )
        return 1
    elapsed = time.monotonic() - started_at
    logger.info("liquidity_enrichment_tick_elapsed_sec", elapsed=round(elapsed, 2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 1a-ii liquidity-enrichment cron writer."
    )
    parser.add_argument(
        "--db",
        default="scout.db",
        help="Path to scout.db. Default: scout.db (cwd).",
    )
    parser.add_argument(
        "--heartbeat-file",
        required=True,
        help="Path touched on every successful tick (incl. no-work). "
        "Consumed by check_liquidity_enrichment_lag.py.",
    )
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
