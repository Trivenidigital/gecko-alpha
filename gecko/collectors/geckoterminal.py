from __future__ import annotations

import asyncio

import aiohttp
import structlog

from gecko.http import Pacer, UpstreamError, get_json
from gecko.identity import is_valid_address, norm_address, token_id
from gecko.jobs import register
from gecko.numeric import num
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, utcnow
from gecko.universe import admit_survivors, in_band

log = structlog.get_logger("gecko.geckoterminal")
BASE = "https://api.geckoterminal.com/api/v2"
POOLS_URL = BASE + "/networks/{network}/{list}?page=1"
OHLCV_URL = BASE + "/networks/{network}/pools/{pool}/ohlcv/day?limit=30&currency=usd"
HEADERS = {"Accept": "application/json;version=20230302"}
# gecko chain -> GeckoTerminal network slug
NETWORK_MAP: dict[str, str] = {
    "solana": "solana",
    "base": "base",
    "ethereum": "eth",
    "bsc": "bsc",
}
LISTS = ("new_pools", "trending_pools")


def token_address_from(rel_id: str, network: str) -> str:
    """Raw base-token address from a ``<network>_<address>`` relationship id.

    Only the leading network prefix is stripped, so an address containing ``_``
    survives. Case is preserved; normalization belongs to gecko.identity.
    """
    prefix = network + "_"
    if rel_id.startswith(prefix):
        addr = rel_id[len(prefix) :]
    else:
        addr = rel_id.split("_", 1)[-1]
    return addr.strip()


def pool_features(attrs: dict, list_name: str) -> dict:
    tx = (attrs.get("transactions") or {}).get("h24") or {}
    return {
        "price_usd": num(attrs.get("base_token_price_usd")),
        "liquidity_usd": num(attrs.get("reserve_in_usd")),
        "fdv_usd": num(attrs.get("fdv_usd")),
        "mcap_usd": num(attrs.get("market_cap_usd")),
        "vol_24h_usd": num((attrs.get("volume_usd") or {}).get("h24")),
        "buys_24h": tx.get("buys"),
        "sells_24h": tx.get("sells"),
        "pool_created_at": attrs.get("pool_created_at"),
        "pool_address": attrs.get("address"),
        "source_list": list_name,
    }


def _base_token_rel(item: dict) -> str | None:
    rels = item.get("relationships") or {}
    return ((rels.get("base_token") or {}).get("data") or {}).get("id")


async def collect_pools(
    store: Store,
    settings: GeckoSettings,
    session: aiohttp.ClientSession,
    now: str,
    *,
    sleep=asyncio.sleep,
) -> JobResult:
    now = canon_ts(now)
    pacer = Pacer(settings.gt_min_interval_sec, sleep)
    written = skipped = invalid = lists = failed = 0
    for chain in settings.chains:
        network = NETWORK_MAP[chain]
        for list_name in LISTS:
            await pacer.wait()
            lists += 1
            try:
                body = await get_json(
                    session,
                    POOLS_URL.format(network=network, list=list_name),
                    settings=settings,
                    headers=HEADERS,
                )
            except UpstreamError as exc:
                # One network's outage must not cost the others their reads.
                failed += 1
                log.warning(
                    "geckoterminal_list_failed",
                    network=network,
                    list=list_name,
                    status=exc.status,
                )
                continue
            for item in (body or {}).get("data") or []:
                attrs = item.get("attributes") or {}
                f = pool_features(attrs, list_name)
                if not in_band(
                    settings,
                    mcap_usd=(f["fdv_usd"] if f["mcap_usd"] is None else f["mcap_usd"]),
                    liquidity_usd=f["liquidity_usd"],
                    chain=chain,
                ):
                    skipped += 1
                    continue
                rel = _base_token_rel(item)
                addr = token_address_from(rel, network) if rel else ""
                if not addr or not is_valid_address(chain, addr):
                    invalid += bool(addr)
                    skipped += 1
                    continue
                try:
                    created = canon_ts(f["pool_created_at"])
                except (AttributeError, TypeError, ValueError):
                    log.warning(
                        "gt_pool_created_at_invalid",
                        pool=f["pool_address"],
                        value=repr(f["pool_created_at"])[:64],
                    )
                    skipped += 1
                    continue
                tid = token_id(chain, addr)
                symbol = (attrs.get("name") or "?").split("/")[0].strip() or "?"
                await store.upsert_token(
                    tid,
                    chain,
                    norm_address(chain, addr),
                    symbol,
                    symbol,
                    now,
                    "geckoterminal",
                    pool_created_at=created,
                )
                await store.add_observation(tid, "geckoterminal", now, f)
                written += 1
    promoted = await admit_survivors(store, settings, now)
    log.info(
        "geckoterminal_pools_done",
        written=written,
        skipped=skipped,
        invalid=invalid,
        promoted=promoted,
        lists=lists,
        lists_failed=failed,
    )
    if lists and failed == lists:
        raise RuntimeError(f"all {lists} geckoterminal pool lists failed")
    return JobResult(rows_written=written)


async def _pool_for(store: Store, tid: str, now: str) -> str | None:
    """Deepest pool among the latest geckoterminal snapshot; dexscreener fallback.

    A token can appear in several pools at one ``observed_at`` (new and trending
    lists, or several pairs); pick by highest liquidity, then pool address.
    """
    gt = await store.observations(tid, as_of=now, source="geckoterminal", limit=50)
    if gt:
        latest = [o for o in gt if o.observed_at == gt[0].observed_at]
        pools = [
            (o.features.get("liquidity_usd"), o.features["pool_address"])
            for o in latest
            if o.features.get("pool_address")
        ]
        if pools:
            pools.sort(key=lambda p: (-(p[0] if p[0] is not None else -1.0), p[1]))
            return pools[0][1]
        return None
    ds = await store.observations(tid, as_of=now, source="dexscreener", limit=1)
    if ds:
        return ds[0].features.get("pair_address") or None
    return None


def _clean_candles(tid: str, raw) -> list[list[float]]:
    out: list[list[float]] = []
    for c in raw if isinstance(raw, list) else []:
        try:
            if not isinstance(c, (list, tuple)) or not c:
                raise TypeError("candle is not a non-empty list")
            out.append([float(x) for x in c])
        except (TypeError, ValueError) as exc:
            log.warning(
                "ohlcv_candle_skipped", token_id=tid, candle=repr(c), error=str(exc)
            )
    return sorted(out, key=lambda c: c[0])


async def collect_ohlcv(
    store: Store,
    settings: GeckoSettings,
    session: aiohttp.ClientSession,
    now: str,
    *,
    sleep=asyncio.sleep,
) -> JobResult:
    now = canon_ts(now)
    pacer = Pacer(settings.gt_min_interval_sec, sleep)
    rows = await (
        await store.conn.execute(
            "SELECT t.token_id, t.chain, (SELECT MAX(observed_at) FROM observations o "
            "WHERE o.token_id=t.token_id AND o.source='geckoterminal_ohlcv' "
            "AND o.observed_at<=?) AS last_ohlcv "
            "FROM tokens t WHERE t.status IN ('candidate','active') "
            "ORDER BY last_ohlcv IS NOT NULL, last_ohlcv, t.token_id",
            (now,),
        )
    ).fetchall()
    written = fetched = 0
    for tid, chain, _ in rows:
        # The budget counts upstream fetches only: pool-less tokens never gain an
        # OHLCV row, so they would sort first forever and starve every later token.
        if fetched >= settings.ohlcv_max_per_run:
            break
        pool = await _pool_for(store, tid, now)
        if not pool:
            continue
        fetched += 1
        await pacer.wait()
        try:
            body = await get_json(
                session,
                OHLCV_URL.format(network=NETWORK_MAP[chain], pool=pool),
                settings=settings,
                headers=HEADERS,
            )
        except UpstreamError as exc:
            if exc.status != 404:
                raise
            body, status = None, "not_found"
        else:
            status = "ok"
        attrs = ((body or {}).get("data") or {}).get("attributes") or {}
        candles = _clean_candles(tid, attrs.get("ohlcv_list"))
        if status == "ok" and not candles:
            status = "empty"
        # Dead/empty pools still get a marker row so last_ohlcv advances and the
        # token rotates to the back of the oldest-first queue.
        await store.add_observation(
            tid,
            "geckoterminal_ohlcv",
            now,
            {"pool_address": pool, "candles": candles, "status": status},
        )
        written += 1
    log.info(
        "geckoterminal_ohlcv_done",
        written=written,
        fetched=fetched,
        considered=len(rows),
    )
    return JobResult(rows_written=written)


@register("geckoterminal")
async def pools_job(settings: GeckoSettings, ns) -> int:
    now = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        return await collect_pools(store, settings, session, now)

    return await run_job("geckoterminal", settings, body)


@register("geckoterminal-ohlcv")
async def ohlcv_job(settings: GeckoSettings, ns) -> int:
    now = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        return await collect_ohlcv(store, settings, session, now)

    return await run_job("geckoterminal-ohlcv", settings, body)
