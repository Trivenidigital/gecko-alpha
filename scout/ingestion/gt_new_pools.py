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
from scout.outcome_ledger import ledger_enabled, record_emission_with_status

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.db import Database

logger = structlog.get_logger()

# Cycle cadence gate (precedent: coingecko._midcap_scan_cycle_counter). The
# lane runs on cycle 1 of every DEX_DISCOVERY_POLL_EVERY_N_CYCLES window.
_poll_cycle_counter: int = 0

# PR-B reconciling counters for the most recent EXECUTED polling pass
# (test/ops introspection). Reset to {} when the last invocation was flag-off
# or cadence-skipped, so a stale nonzero snapshot can never survive a skipped
# pass. Contract (design review 2026-07-20):
#   candidates = attempted + budget_skipped
#   attempted  = succeeded + failed_none
#   succeeded  = enrolled + not_needed
# candidates counts NEW discoveries only — dedup re-sightings and dust pools
# are excluded by definition (they never reach the ledger stage). Operational
# ledger-write failures are contained by record_emission_with_status and returned as
# None; counted as failed_none, never described as enrolled. Intentional
# disablement (LEDGER_ENABLED=False) is NOT failure: the ledger stage is not
# attempted, candidates excludes the globally-disabled work by definition,
# and the pass log carries ledger_enabled=False.
last_pass_counters: dict = {}


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
    global _poll_cycle_counter, last_pass_counters

    if not settings.DEX_DISCOVERY_ENABLED:
        last_pass_counters = {}
        return 0

    _poll_cycle_counter += 1
    if (_poll_cycle_counter - 1) % settings.DEX_DISCOVERY_POLL_EVERY_N_CYCLES != 0:
        last_pass_counters = {}
        return 0

    recorded = 0
    poll_ok = False
    ledger_on = ledger_enabled(settings)
    counters = {
        "ledger_enabled": ledger_on,
        "poll_ok": False,
        "heartbeat_written": False,
        "candidates": 0,
        "attempted": 0,
        "succeeded": 0,
        "failed_none": 0,
        "budget_skipped": 0,
        "enrolled": 0,
        "not_needed": 0,
    }
    for network in settings.DEX_DISCOVERY_NETWORKS:
        url = f"{GECKO_BASE}/networks/{network}/new_pools"
        data = await _get_json(session, url, chain=network)
        raw_pools = data.get("data") if isinstance(data, dict) else None
        if not isinstance(raw_pools, list):
            # {} / error-object / non-list "data": a provider 200 carrying no
            # pool list is NOT a valid poll and must not advance the heartbeat.
            logger.warning("dex_discovery_malformed_payload", network=network)
            continue
        # Parse ONCE; the same parse results drive both the validity rule and
        # the discovery loop. Validity: an empty list is a healthy quiet
        # market; a NONEMPTY list where every record is structurally unusable
        # is schema drift, not a valid poll.
        parsed_pools = [
            _parse_pool(pool if isinstance(pool, dict) else {}) for pool in raw_pools
        ]
        if raw_pools and not any(pp is not None for pp in parsed_pools):
            logger.warning(
                "dex_discovery_schema_invalid_pool_set",
                network=network,
                raw=len(raw_pools),
            )
            continue
        poll_ok = True
        seen = 0
        for parsed in parsed_pools:
            if parsed is None:
                continue
            liq = parsed["liquidity_usd"]
            if (liq or 0.0) < settings.DEX_DISCOVERY_MIN_LIQUIDITY_USD:
                continue
            seen += 1
            is_new = await db.record_pool_discovery(network=network, **parsed)
            if is_new:
                recorded += 1
                # Ledger stage. Intentional disablement is not failure:
                # when the kill switch is off the stage is never attempted
                # and globally-disabled work is excluded from `candidates`
                # by definition (equation holds trivially at all-zero).
                if ledger_on:
                    counters["candidates"] += 1
                    if (
                        counters["attempted"]
                        < settings.DEX_DISCOVERY_LEDGER_ENROLL_PER_CYCLE
                    ):
                        counters["attempted"] += 1
                        result = await record_emission_with_status(
                            db,
                            settings,
                            kind="gated_out_sample",
                            token_id=f"dex:{network}:{parsed['base_token_address']}",
                            surface="dex_new_pool",
                            price=None,
                            liquidity=parsed["liquidity_usd"],
                            liquidity_source="gt_new_pools",
                            gate_verdicts={
                                "lane": "dex_discovery",
                                "pool": parsed["pool_address"],
                            },
                        )
                        if result is None:
                            counters["failed_none"] += 1
                        else:
                            _row_id, enrollment_status = result
                            counters["succeeded"] += 1
                            if enrollment_status == "enrolled":
                                counters["enrolled"] += 1
                            else:
                                counters["not_needed"] += 1
                    else:
                        counters["budget_skipped"] += 1
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
    counters["poll_ok"] = poll_ok
    if poll_ok:
        # Durable liveness heartbeat (PR-C seam): a SUCCESSFUL poll (>=1
        # network yielded valid data — independent of whether any NEW pool
        # appeared) upserts source='dex_discovery' with misses=0; its
        # updated_at is last_successful_poll_at. An unsuccessful pass writes
        # nothing, so watchdog staleness is measured from the last SUCCESS.
        # Poller health, not market activity: a quiet market stays healthy.
        try:
            await db.upsert_ingest_watchdog_state("dex_discovery", 0)
            counters["heartbeat_written"] = True
        except Exception:
            logger.exception("dex_discovery_heartbeat_write_failed")
    last_pass_counters = counters
    logger.info("dex_discovery_ledger_pass", **counters)
    return recorded
