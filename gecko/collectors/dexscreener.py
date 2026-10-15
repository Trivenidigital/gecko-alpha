from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
import structlog

from gecko.calls import call_seeds
from gecko.http import Pacer, UpstreamError, get_json
from gecko.identity import is_valid_address, norm_address, token_id
from gecko.jobs import register
from gecko.numeric import num
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, parse_utc, utcnow
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


def _iso_ms(ms) -> str | None:
    n = num(ms)
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
        price = num(p.get("priceUsd"))
        liq = num((p.get("liquidity") or {}).get("usd"))
        if price is None or liq is None:
            continue
        if best is None or liq > best_liq:
            best, best_liq = p, liq
    return best


def pair_features(p: dict) -> dict:
    tx = (p.get("txns") or {}).get("h24") or {}
    return {
        "price_usd": num(p.get("priceUsd")),
        "liquidity_usd": num((p.get("liquidity") or {}).get("usd")),
        "fdv_usd": num(p.get("fdv")),
        "mcap_usd": num(p.get("marketCap")),
        "vol_24h_usd": num((p.get("volume") or {}).get("h24")),
        "buys_24h": tx.get("buys"),
        "sells_24h": tx.get("sells"),
        "pair_created_at": _iso_ms(p.get("pairCreatedAt")),
        "pair_address": p.get("pairAddress"),
        "dex": p.get("dexId"),
    }


async def _discover(
    session: aiohttp.ClientSession,
    settings: GeckoSettings,
    pacer: Pacer,
    calls: dict[str, int],
) -> tuple[dict[str, set[str]], int]:
    """Feed addresses per configured chain, and how many were malformed.

    A failed feed is logged and counted in ``calls``; the other feed and the
    refresh pass still run.
    """
    found: dict[str, set[str]] = {}
    invalid = 0
    for url in (BOOSTS_URL, PROFILES_URL):
        await pacer.wait()
        calls["total"] += 1
        try:
            items = await get_json(session, url, settings=settings)
        except UpstreamError as exc:
            calls["failed"] += 1
            log.warning("dexscreener_feed_failed", url=url, status=exc.status)
            continue
        for it in items or []:
            chain = CHAIN_MAP.get(str(it.get("chainId", "")).lower())
            addr = str(it.get("tokenAddress") or "").strip()
            if chain not in settings.chains or not addr:
                continue
            if not is_valid_address(chain, addr):
                invalid += 1  # never reaches a URL or the store
                continue
            found.setdefault(chain, set()).add(norm_address(chain, addr))
    return found, invalid


async def _pairs(
    session: aiohttp.ClientSession,
    settings: GeckoSettings,
    pacer: Pacer,
    batches: dict[str, int],
    chain: str,
    addrs,
):
    """(address, best pair or None) for ``addrs``, one paced tokens/v1 call per 30.

    A failed batch is logged and counted in ``batches`` and yields nothing, so
    one bad batch cannot stop the rest of the run.
    """
    ordered = sorted(addrs)
    for i in range(0, len(ordered), BATCH):
        batch = ordered[i : i + BATCH]
        await pacer.wait()
        batches["total"] += 1
        try:
            pairs = await get_json(
                session,
                TOKENS_URL.format(chain=chain, addresses=",".join(batch)),
                settings=settings,
            )
        except UpstreamError as exc:
            batches["failed"] += 1
            log.warning(
                "dexscreener_batch_failed",
                chain=chain,
                size=len(batch),
                status=exc.status,
            )
            continue
        for addr in batch:
            yield addr, best_pair(pairs or [], addr, chain)


async def _age_out_candidates(store: Store, settings: GeckoSettings, now: str) -> int:
    """Candidates never admitted within ``candidate_max_age_days`` stop being
    refreshed or considered for admission."""
    cutoff = canon_ts(
        (parse_utc(now) - timedelta(days=settings.candidate_max_age_days)).isoformat()
    )
    cur = await store.conn.execute(
        "UPDATE tokens SET status='disqualified', disqualify_reason='stale_candidate', "
        "updated_at=? WHERE status='candidate' AND first_seen_at<?",
        (utcnow(), cutoff),
    )
    await store.conn.commit()
    return cur.rowcount


async def _refresh_targets(
    store: Store, settings: GeckoSettings
) -> list[tuple[str, str]]:
    """Known tokens to re-observe whether or not a feed lists them: candidates
    and actives, plus every token with an open watchlist row. Ordered by the
    oldest DexScreener reading first (never read first)."""
    rows = await (
        await store.conn.execute(
            "SELECT t.chain, t.address, (SELECT MAX(o.observed_at) FROM observations o "
            "WHERE o.token_id=t.token_id AND o.source='dexscreener') AS last "
            "FROM tokens t WHERE t.chain!='cg' AND t.token_id!='benchmark:btc' "
            "AND (t.status IN ('candidate','active') OR t.token_id IN "
            "(SELECT token_id FROM watchlist WHERE exited_at IS NULL)) "
            "ORDER BY last IS NOT NULL, last, t.token_id"
        )
    ).fetchall()
    return [(c, a) for c, a, _ in rows if c in settings.chains]


def _refresh_row(p: dict) -> dict:
    # Written whatever the liquidity: a below-floor reading is what lets the
    # survivor window reset and the liquidity disqualifier fire.
    return {**pair_features(p), "refresh": True}


async def collect(
    store: Store,
    settings: GeckoSettings,
    session: aiohttp.ClientSession,
    now: str,
    *,
    sleep=asyncio.sleep,
) -> JobResult:
    pacer = Pacer(settings.ds_min_interval_sec, sleep)
    batches = {"total": 0, "failed": 0}
    feeds = {"total": 0, "failed": 0}
    aged = await _age_out_candidates(store, settings, now)
    targets = await _refresh_targets(store, settings)
    known = set(targets)
    found, invalid = await _discover(session, settings, pacer, feeds)
    seeded: set[tuple[str, str]] = set()
    for chain, addrs in (await call_seeds(store, settings, now)).items():
        have = found.setdefault(chain, set())
        seeded |= {(chain, a) for a in addrs - have}
        have |= addrs
    written = skipped = refreshed = missing = revived = 0
    for chain, addrs in found.items():
        async for addr, p in _pairs(session, settings, pacer, batches, chain, addrs):
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
                if (chain, addr) in known:  # fetched already: keep the reading
                    await store.add_observation(
                        token_id(chain, addr), "dexscreener", now, _refresh_row(p)
                    )
                    refreshed += 1
                else:
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
                "telegram" if (chain, addr) in seeded else "dexscreener",
                pool_created_at=f["pair_created_at"],
            )
            # An aged-out candidate back in band starts a fresh candidacy.
            revived += await store.revive_candidate(tid, now)
            await store.add_observation(tid, "dexscreener", now, f)
            written += 1
    eligible = [t for t in targets if t[1] not in found.get(t[0], set())]
    todo = eligible[: settings.ds_refresh_max_per_run]  # oldest reading first
    by_chain: dict[str, list[str]] = {}
    for chain, addr in todo:
        by_chain.setdefault(chain, []).append(addr)
    for chain, addrs in by_chain.items():
        async for addr, p in _pairs(session, settings, pacer, batches, chain, addrs):
            if p is None:
                missing += 1
                continue
            await store.add_observation(
                token_id(chain, addr), "dexscreener", now, _refresh_row(p)
            )
            refreshed += 1
    log.info(
        "dexscreener_refresh_done",
        refreshed=refreshed,
        missing=missing,
        planned=len(todo),
        deferred=len(eligible) - len(todo),
        aged_out=aged,
    )
    promoted = await admit_survivors(store, settings, now)
    log.info(
        "dexscreener_done",
        written=written,
        skipped=skipped,
        invalid=invalid,
        promoted=promoted,
        revived=revived,
        seeded=len(seeded),
        batches=batches["total"],
        batches_failed=batches["failed"],
        feeds_failed=feeds["failed"],
    )
    if feeds["failed"] == feeds["total"] and batches["failed"] == batches["total"]:
        raise RuntimeError("every dexscreener feed and refresh batch failed")
    return JobResult(rows_written=written + refreshed)


@register("dexscreener")
async def dexscreener_job(settings: GeckoSettings, ns) -> int:
    now = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        return await collect(store, settings, session, now)

    return await run_job("dexscreener", settings, body)
