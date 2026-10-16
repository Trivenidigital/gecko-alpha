from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import structlog

from gecko.benchmark import BTC_ID, upsert_btc
from gecko.http import Pacer, UpstreamError, _retryable, get_json
from gecko.identity import is_valid_address, norm_address, token_id
from gecko.jobs import register
from gecko.runs import JobResult, PartialFailure, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, iso_plus, utcnow
from gecko.universe import in_band

log = structlog.get_logger("gecko.coingecko")
BASE = "https://api.coingecko.com/api/v3"
# CoinGecko platform slug -> gecko chain
PLATFORM_MAP = {
    "solana": "solana",
    "base": "base",
    "ethereum": "ethereum",
    "binance-smart-chain": "bsc",
}
RETRY_BACKOFF = (2.0, 4.0, 8.0)
COIN_PARAMS = {
    "localization": "false",
    "tickers": "false",
    "market_data": "false",
    "community_data": "false",
    "developer_data": "false",
    "sparkline": "false",
}


QUOTA_EXHAUSTED_CODE = 10006


def _quota_exhausted(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    status = body.get("status")
    code = status.get("error_code") if isinstance(status, dict) else None
    return QUOTA_EXHAUSTED_CODE in (code, body.get("error_code"))


class CGClient:
    """Demo-tier client: adds the API key header, paces requests, counts calls."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        settings: GeckoSettings,
        sleep=asyncio.sleep,
    ):
        self.session, self.settings, self.sleep, self.calls = (
            session,
            settings,
            sleep,
            0,
        )
        self.invalid_addresses = 0
        self.pacer = Pacer(settings.cg_min_interval_sec, sleep)

    def _headers(self) -> dict:
        key = self.settings.coingecko_demo_api_key
        return {"x-cg-demo-api-key": key.get_secret_value()} if key else {}

    async def get(self, path: str, params: dict | None = None) -> Any:
        # Retries live here, not in get_json, so every HTTP attempt (a 429 or 5xx
        # included) is paced and counted against the monthly budget.
        for attempt in range(len(RETRY_BACKOFF) + 1):
            await self.pacer.wait()
            self.calls += 1
            try:
                return await get_json(
                    self.session,
                    BASE + path,
                    settings=self.settings,
                    headers=self._headers(),
                    params=params,
                    retries=0,
                    sleep=self.sleep,
                )
            except UpstreamError as exc:
                if exc.status == 429 and _quota_exhausted(exc.body):
                    # Monthly credits are gone; retrying only burns more attempts.
                    log.warning("cg_quota_exhausted", path=path, calls=self.calls)
                    raise
                retryable = exc.status == 0 or _retryable(exc.status)
                if not retryable or attempt == len(RETRY_BACKOFF):
                    raise
                delay = RETRY_BACKOFF[attempt]
                log.warning("cg_retry", path=path, status=exc.status, delay=delay)
                await self.sleep(delay)
        raise AssertionError("unreachable")


async def calls_this_month(store: Store, now: str) -> int:
    year, month = int(canon_ts(now)[:4]), int(canon_ts(now)[5:7])
    nxt_year, nxt_month = (year + 1, 1) if month == 12 else (year, month + 1)
    month_start = f"{year:04d}-{month:02d}-01T00:00:00Z"
    month_end = f"{nxt_year:04d}-{nxt_month:02d}-01T00:00:00Z"
    row = await (
        await store.conn.execute(
            "SELECT COALESCE(SUM(credits_used),0) FROM runs "
            "WHERE job LIKE 'coingecko%' AND started_at>=? AND started_at<?",
            (month_start, month_end),
        )
    ).fetchone()
    return int(row[0])


async def _budget_left(
    store: Store,
    settings: GeckoSettings,
    planned: int,
    *,
    reserve: int = 0,
    budget_now: str | None = None,
) -> int | None:
    """Calls still available this month, or None if ``planned`` does not fit.

    The month is always the real one (``budget_now`` is a test seam): an
    ``--as-of`` replay spends real credits in the current month.
    """
    used = await calls_this_month(store, canon_ts(budget_now) or utcnow())
    cap = settings.coingecko_monthly_cap - reserve
    if used + planned > cap:
        log.warning(
            "cg_budget_exhausted",
            used=used,
            planned=planned,
            cap=settings.coingecko_monthly_cap,
            reserve=reserve,
        )
        return None
    return cap - used


async def _known_platform(
    store: Store, settings: GeckoSettings, cid: str, now: str
) -> tuple[str, str] | None | bool:
    """(chain, address) if mapped, False if known-unmapped, None if never fetched
    (a marker older than ``cg_unmapped_ttl_days`` counts as never fetched)."""
    row = await (
        await store.conn.execute(
            "SELECT chain, address FROM tokens WHERE cg_id=? AND chain!='cg'", (cid,)
        )
    ).fetchone()
    if row:
        return row[0], row[1]
    seen = await store.observations(
        f"cg:{cid}", as_of=now, source="cg_unmapped", limit=1
    )
    fresh = iso_plus(now, -settings.cg_unmapped_ttl_days)
    return False if seen and seen[0].observed_at > fresh else None


async def _fetch_platform(
    store: Store, settings: GeckoSettings, cg: CGClient, cid: str, now: str
) -> tuple[str, str] | None:
    """First platform on a configured chain; otherwise mark the coin unmapped."""
    body = await cg.get(f"/coins/{cid}", COIN_PARAMS)
    platforms = (body if isinstance(body, dict) else {}).get("platforms") or {}
    if not isinstance(platforms, dict) or not any(str(k).strip() for k in platforms):
        return None  # no answer (empty, or the {"": ""} placeholder): no marker
    for platform, addr in platforms.items():
        chain = PLATFORM_MAP.get(platform)
        if chain not in settings.chains or not addr or not str(addr).strip():
            continue
        addr = str(addr).strip()
        if is_valid_address(chain, addr):
            return chain, addr
        cg.invalid_addresses += 1
        log.warning("cg_platform_address_invalid", cg_id=cid, chain=chain)
    pseudo = f"cg:{cid}"
    await store.upsert_token(pseudo, "cg", cid, cid, cid, now, "coingecko", cg_id=cid)
    await store.set_token_status(pseudo, "disqualified", "no_supported_platform")
    await store.add_observation(
        pseudo, "cg_unmapped", now, {"platforms": list(platforms)}
    )
    return None


def _market_features(r: dict) -> dict:
    return {
        "price_usd": r.get("current_price"),
        "mcap_usd": r.get("market_cap"),
        "vol_24h_usd": r.get("total_volume"),
        "mcap_rank": r.get("market_cap_rank"),
        "price_change_24h_pct": r.get("price_change_percentage_24h_in_currency"),
        "price_change_7d_pct": r.get("price_change_percentage_7d_in_currency"),
    }


async def markets_sweep(
    store: Store,
    settings: GeckoSettings,
    session: aiohttp.ClientSession,
    now: str,
    sleep=asyncio.sleep,
    *,
    budget_now: str | None = None,
) -> JobResult:
    now = canon_ts(now)
    planned = settings.cg_max_pages - settings.cg_first_page + 1
    # The hourly BTC/trending job keeps cg_hourly_reserve calls of the month.
    left = await _budget_left(
        store,
        settings,
        planned,
        reserve=settings.cg_hourly_reserve,
        budget_now=budget_now,
    )
    if left is None:
        return JobResult()
    cg = CGClient(session, settings, sleep)
    written = 0
    try:
        budget_skipped = lookups = 0
        for page in range(settings.cg_first_page, settings.cg_max_pages + 1):
            rows = await cg.get(
                "/coins/markets",
                {
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": page,
                    "price_change_percentage": "24h,7d",
                },
            )
            if not rows:
                break
            # Platform lookups may only spend what the remaining planned pages leave.
            reserved = settings.cg_max_pages - page
            for r in rows:
                mcap = r.get("market_cap")
                if mcap is None:
                    continue
                # Liquidity floor passed through: the mcap band alone decides admission.
                if not in_band(
                    settings,
                    mcap_usd=mcap,
                    liquidity_usd=settings.min_liquidity_usd,
                    chain=settings.chains[0],
                ):
                    continue
                cid = r["id"]
                mapped = await _known_platform(store, settings, cid, now)
                if mapped is None:
                    if (
                        cg.calls + 1 + reserved > left
                        or lookups >= settings.cg_max_lookups_per_run
                    ):
                        budget_skipped += 1  # deferred: retried next sweep
                        continue
                    lookups += 1
                    mapped = await _fetch_platform(store, settings, cg, cid, now)
                if not mapped:
                    continue
                chain, addr = mapped
                if chain not in settings.chains:
                    continue
                tid = token_id(chain, addr)
                await store.upsert_token(
                    tid,
                    chain,
                    norm_address(chain, addr),
                    (r.get("symbol") or "?").upper(),
                    r.get("name") or "?",
                    now,
                    "coingecko",
                    cg_id=cid,
                )
                await store.add_observation(tid, "coingecko", now, _market_features(r))
                written += 1
            if (rows[-1].get("market_cap") or 0) < settings.min_mcap_usd:
                break
        if budget_skipped:
            log.warning("cg_platform_lookups_deferred", count=budget_skipped, left=left)
        log.info(
            "coingecko_markets_done",
            written=written,
            calls=cg.calls,
            lookups=lookups,
            invalid_addresses=cg.invalid_addresses,
        )
        return JobResult(rows_written=written, credits_used=cg.calls)
    except Exception as exc:
        raise PartialFailure(exc, credits_used=cg.calls, rows_written=written) from exc


async def hourly(
    store: Store,
    settings: GeckoSettings,
    session: aiohttp.ClientSession,
    now: str,
    sleep=asyncio.sleep,
    *,
    budget_now: str | None = None,
) -> JobResult:
    now = canon_ts(now)
    if await _budget_left(store, settings, 2, budget_now=budget_now) is None:
        # Unlike the sweep, a skipped hourly run starves the BTC benchmark the
        # ledger needs: fail the run so it is visible (ok=0, exit 1).
        raise RuntimeError("cg budget exhausted")
    cg = CGClient(session, settings, sleep)
    written = 0
    try:
        trending = await cg.get("/search/trending")
        for rank, entry in enumerate(((trending or {}).get("coins") or []), start=1):
            item = (entry or {}).get("item") or {}
            row = await (
                await store.conn.execute(
                    "SELECT token_id FROM tokens WHERE cg_id=? AND chain!='cg'",
                    (item.get("id"),),
                )
            ).fetchone()
            if row:
                await store.add_observation(
                    row[0],
                    "coingecko",
                    now,
                    {"trending_rank": rank, "mcap_rank": item.get("market_cap_rank")},
                )
                written += 1
        price = await cg.get(
            "/simple/price", {"ids": "bitcoin", "vs_currencies": "usd"}
        )
        btc = ((price or {}).get("bitcoin") or {}).get("usd")
        if btc:
            await upsert_btc(store, now, "coingecko")
            await store.add_observation(
                BTC_ID, "coingecko", now, {"price_usd": float(btc)}
            )
            written += 1
        else:
            keys = sorted(map(str, price))[:8] if isinstance(price, dict) else None
            log.warning("cg_btc_price_missing", kind=type(price).__name__, keys=keys)
        return JobResult(rows_written=written, credits_used=cg.calls)
    except Exception as exc:
        raise PartialFailure(exc, credits_used=cg.calls, rows_written=written) from exc


@register("coingecko-markets")
async def markets_job(settings: GeckoSettings, ns) -> int:
    now = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store, session):
        return await markets_sweep(store, settings, session, now)

    return await run_job("coingecko-markets", settings, body)


@register("coingecko-hourly")
async def hourly_job(settings: GeckoSettings, ns) -> int:
    now = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store, session):
        return await hourly(store, settings, session, now)

    return await run_job("coingecko-hourly", settings, body)
