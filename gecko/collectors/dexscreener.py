from __future__ import annotations

from datetime import datetime, timezone

import aiohttp
import structlog

from gecko.http import get_json
from gecko.identity import norm_address, token_id
from gecko.jobs import register
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, utcnow
from gecko.universe import admit_survivors, in_band

log = structlog.get_logger("gecko.dexscreener")
BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{addresses}"
CHAIN_MAP: dict[str, str] = {
    "solana": "solana",
    "base": "base",
    "ethereum": "ethereum",
    "bsc": "bsc",
}
BATCH = 30


def _num(v) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _iso_ms(ms) -> str | None:
    n = _num(ms)
    if n is None:
        return None
    return datetime.fromtimestamp(n / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def best_pair(pairs: list[dict], address: str, chain: str) -> dict | None:
    want = norm_address(chain, address)
    best = None
    best_liq = 0.0
    for p in pairs:
        base_addr = str((p.get("baseToken") or {}).get("address", ""))
        if norm_address(chain, base_addr) != want:
            continue
        price = _num(p.get("priceUsd"))
        liq = _num((p.get("liquidity") or {}).get("usd"))
        if price is None or liq is None:
            continue
        if best is None or liq > best_liq:
            best, best_liq = p, liq
    return best


def pair_features(p: dict) -> dict:
    tx = (p.get("txns") or {}).get("h24") or {}
    return {
        "price_usd": _num(p.get("priceUsd")),
        "liquidity_usd": _num((p.get("liquidity") or {}).get("usd")),
        "fdv_usd": _num(p.get("fdv")),
        "mcap_usd": _num(p.get("marketCap")),
        "vol_24h_usd": _num((p.get("volume") or {}).get("h24")),
        "buys_24h": tx.get("buys"),
        "sells_24h": tx.get("sells"),
        "pair_created_at": _iso_ms(p.get("pairCreatedAt")),
        "pair_address": p.get("pairAddress"),
        "dex": p.get("dexId"),
    }


async def _discover(
    session: aiohttp.ClientSession, settings: GeckoSettings
) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for url in (BOOSTS_URL, PROFILES_URL):
        items = await get_json(session, url, settings=settings)
        for it in items or []:
            chain = CHAIN_MAP.get(str(it.get("chainId", "")).lower())
            addr = str(it.get("tokenAddress") or "")
            if chain in settings.chains and addr:
                found.setdefault(chain, set()).add(norm_address(chain, addr))
    return found


async def collect(
    store: Store,
    settings: GeckoSettings,
    session: aiohttp.ClientSession,
    now: str,
) -> JobResult:
    found = await _discover(session, settings)
    written = skipped = 0
    for chain, addrs in found.items():
        ordered = sorted(addrs)
        for i in range(0, len(ordered), BATCH):
            batch = ordered[i : i + BATCH]
            pairs = await get_json(
                session,
                TOKENS_URL.format(chain=chain, addresses=",".join(batch)),
                settings=settings,
            )
            for addr in batch:
                p = best_pair(pairs or [], addr, chain)
                if p is None:
                    skipped += 1
                    continue
                f = pair_features(p)
                if not in_band(
                    settings,
                    mcap_usd=(f["fdv_usd"] if f["mcap_usd"] is None else f["mcap_usd"]),
                    liquidity_usd=f["liquidity_usd"],
                    chain=chain,
                ):
                    skipped += 1
                    continue
                base = p.get("baseToken") or {}
                tid = token_id(chain, addr)
                await store.upsert_token(
                    tid,
                    chain,
                    addr,
                    base.get("symbol") or "?",
                    base.get("name") or "?",
                    now,
                    "dexscreener",
                    pool_created_at=f["pair_created_at"],
                )
                await store.add_observation(tid, "dexscreener", now, f)
                written += 1
    promoted = await admit_survivors(store, settings, now)
    log.info("dexscreener_done", written=written, skipped=skipped, promoted=promoted)
    return JobResult(rows_written=written)


@register("dexscreener")
async def dexscreener_job(settings: GeckoSettings, ns) -> int:
    now = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        return await collect(store, settings, session, now)

    return await run_job("dexscreener", settings, body)
