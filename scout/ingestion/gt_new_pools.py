"""GeckoTerminal new-pools discovery — DEX-first Phase 1 (research-only lane).

Design: tasks/design_dex_first_discovery_2026_07_20.md. Polls the public GT
``GET /networks/{network}/new_pools`` endpoint (keyless — NOT on the CoinGecko
credit budget) and records every first-seen pool to ``dex_pool_discoveries``,
stamping forward identity into ``contract_coin_map`` (source='gt_new_pools',
coin_id=NULL) so the DEX corpus is identifiable at the graduation moment
instead of retroactively at CG-listing time.

Observe-only guardrails (mirrors the I1/I2/I3 discipline):
- Emits NO CandidateToken — nothing reaches aggregate()/scorer/gate/alerts.
- Gated by DEX_DISCOVERY_ENABLED; when False this module makes no HTTP call
  and the pipeline is byte-identical.
- Never raises into run_cycle (caller wraps, and _get_json returns None on
  HTTP/network failure by design).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp
import structlog

from scout.ingestion.geckoterminal import GECKO_BASE, _get_json

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.db import Database

logger = structlog.get_logger()

# Cycle cadence gate (precedent: coingecko._midcap_scan_cycle_counter). The
# lane runs on cycle 1 of every DEX_DISCOVERY_POLL_EVERY_N_CYCLES window.
_poll_cycle_counter: int = 0


def _parse_pool(pool: dict) -> dict | None:
    """Extract the discovery record from one GT new_pools entry.

    Returns None for malformed entries (skipped, never fatal).
    """
    attrs = pool.get("attributes") or {}
    rels = pool.get("relationships") or {}
    pool_address = attrs.get("address")
    base_rel = ((rels.get("base_token") or {}).get("data") or {}).get("id") or ""
    # GT relationship ids are "<network>_<address>"
    base_token_address = base_rel.split("_", 1)[1] if "_" in base_rel else None
    if not pool_address or not base_token_address:
        return None

    name = attrs.get("name") or ""
    base_symbol, quote_symbol = None, None
    if " / " in name:
        base_symbol, _, quote_symbol = (p.strip() for p in name.partition(" / "))

    def _f(v) -> float | None:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "pool_address": pool_address,
        "base_token_address": base_token_address,
        "base_token_symbol": base_symbol,
        "quote_token_symbol": quote_symbol,
        "pool_created_at": attrs.get("pool_created_at"),
        "fdv_usd": _f(attrs.get("fdv_usd")),
        "liquidity_usd": _f(attrs.get("reserve_in_usd")),
        "volume_h1_usd": _f((attrs.get("volume_usd") or {}).get("h1")),
    }


async def discover_new_pools(
    session: aiohttp.ClientSession,
    db: "Database",
    settings: "Settings",
) -> int:
    """Poll GT new_pools for each configured network; record first-seens.

    Returns the number of NEW discoveries recorded this pass (0 when the
    flag is off, the cadence gate skips, or nothing new/eligible appeared).
    """
    global _poll_cycle_counter

    if not settings.DEX_DISCOVERY_ENABLED:
        return 0

    _poll_cycle_counter += 1
    if (_poll_cycle_counter - 1) % settings.DEX_DISCOVERY_POLL_EVERY_N_CYCLES != 0:
        return 0

    recorded = 0
    for network in settings.DEX_DISCOVERY_NETWORKS:
        url = f"{GECKO_BASE}/networks/{network}/new_pools"
        data = await _get_json(session, url, chain=network)
        if not isinstance(data, dict):
            continue
        raw_pools = data.get("data") or []
        seen = 0
        for pool in raw_pools:
            parsed = _parse_pool(pool if isinstance(pool, dict) else {})
            if parsed is None:
                continue
            liq = parsed["liquidity_usd"]
            if (liq or 0.0) < settings.DEX_DISCOVERY_MIN_LIQUIDITY_USD:
                continue
            seen += 1
            is_new = await db.record_pool_discovery(network=network, **parsed)
            if is_new:
                recorded += 1
                # Forward identity at the graduation moment: coin_id=NULL row
                # keyed by mint address; the I1 resolver upserts the CG
                # coin_id over it if/when the token ever lists.
                try:
                    await db.record_contract_coin_map(
                        contract_address=parsed["base_token_address"],
                        chain=network,
                        coin_id=None,
                        source="gt_new_pools",
                        confidence=None,
                    )
                except Exception:
                    logger.exception(
                        "dex_discovery_identity_write_failed",
                        network=network,
                        contract=parsed["base_token_address"],
                    )
        logger.info(
            "dex_discovery_pass",
            network=network,
            raw=len(raw_pools),
            eligible=seen,
            new=recorded,
        )
    return recorded
