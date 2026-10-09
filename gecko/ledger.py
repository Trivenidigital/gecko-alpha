from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import aiohttp
import structlog

from gecko.http import UpstreamError, get_json
from gecko.jobs import register
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.numeric import num
from gecko.store import (
    DEX_SOURCES,
    ReturnAlreadyMeasured,
    Store,
    canon_ts,
    parse_utc,
    utcnow,
)

log = structlog.get_logger("gecko.ledger")
MAX_ATTEMPTS = 3
BTC_WINDOW_HOURS = 6
BTC_FALLBACK_WINDOW_HOURS = 36
ENTRY_WINDOW_HOURS = 48  # no priced read this soon after entry: unmeasurable
BTC_TOKEN_ID = "benchmark:btc"
# UpstreamError statuses that spend no attempt (plus every 5xx); 0 = transport.
TRANSIENT_STATUSES = frozenset({0, 403, 429})
DEXSCREENER_TOKENS = "https://api.dexscreener.com/tokens/v1/{chain}/{address}"


def _price(value) -> float | None:
    """A usable positive finite price, else None."""
    p = num(value)
    return p if p is not None and p > 0 else None


async def _first_priced(
    store: Store, token_id: str, sources, entered_at: str, as_of: str | None
) -> float | None:
    """Price of the earliest priced row from ``sources`` at or after
    ``entered_at`` (bounded by ``as_of``); deepest pool among rows that share
    that earliest timestamp."""
    q = ",".join("?" * len(sources))
    sql = (
        f"SELECT observed_at, features_json FROM observations WHERE token_id=? "
        f"AND source IN ({q}) AND observed_at>=?"
    )
    args: list = [token_id, *sources, canon_ts(entered_at)]
    if as_of is not None:
        sql += " AND observed_at<=?"
        args.append(canon_ts(as_of))
    sql += " ORDER BY observed_at ASC, id ASC"
    best: tuple[float, float] | None = None
    best_at: str | None = None
    async with store.conn.execute(sql, args) as cur:
        async for obs_at, fj in cur:
            if best_at is not None and obs_at != best_at:
                break
            f = json.loads(fj)
            p = _price(f.get("price_usd"))
            if p is None:
                continue
            liq = num(f.get("liquidity_usd"))
            liq = -math.inf if liq is None else liq
            if best is None or liq > best[0]:
                best, best_at = (liq, p), obs_at
    return best[1] if best else None


async def entry_price(
    store: Store, token_id: str, entered_at: str, *, as_of: str | None = None
) -> float | None:
    """Price of the earliest priced DEX observation at or after ``entered_at``.

    DEX rows (the ledger's own price source) win; a CoinGecko row is used only
    when no priced DEX row exists in the window. Unpriced rows are passed
    over, and a later row is never preferred to an earlier priced one.
    ``as_of`` bounds the search so a run cannot read its own future.
    """
    p = await _first_priced(store, token_id, DEX_SOURCES, entered_at, as_of)
    if p is None:
        p = await _first_priced(store, token_id, ("coingecko",), entered_at, as_of)
    return p


async def _nearest_btc(
    store: Store, t: datetime, hours: int
) -> tuple[float, float] | None:
    """(distance_sec, price) of the priced BTC observation nearest ``t``."""
    window = timedelta(hours=hours)
    rows = await (
        await store.conn.execute(
            "SELECT observed_at, features_json FROM observations WHERE token_id=? "
            "AND observed_at BETWEEN ? AND ?",
            (
                BTC_TOKEN_ID,
                canon_ts((t - window).isoformat()),
                canon_ts((t + window).isoformat()),
            ),
        )
    ).fetchall()
    best: tuple[float, float] | None = None
    for obs_at, fj in rows:
        p = _price(json.loads(fj).get("price_usd"))
        if p is None:
            continue
        d = abs((parse_utc(obs_at) - t).total_seconds())
        if best is None or d < best[0]:
            best = (d, p)
    return best


async def btc_price_at(store: Store, ts: str) -> float | None:
    """``benchmark:btc`` price observed nearest ``ts``.

    Tries +/-6 h; if that window holds no priced observation, falls back to
    +/-36 h (logged as ``ledger_btc_fallback_window``); else None.
    """
    t = parse_utc(ts)
    best = await _nearest_btc(store, t, BTC_WINDOW_HOURS)
    if best is None:
        best = await _nearest_btc(store, t, BTC_FALLBACK_WINDOW_HOURS)
        if best is not None:
            log.warning(
                "ledger_btc_fallback_window",
                ts=canon_ts(ts),
                distance_hours=round(best[0] / 3600, 2),
            )
    return best[1] if best else None


async def fetch_price(
    session: aiohttp.ClientSession,
    settings: GeckoSettings,
    chain: str,
    address: str,
) -> float | None:
    """DexScreener price of the deepest-liquidity pair; None on 404/empty/unusable."""
    url = DEXSCREENER_TOKENS.format(chain=chain, address=address)
    try:
        data = await get_json(session, url, settings=settings, ok_statuses=(200, 404))
    except ValueError as exc:  # includes json.JSONDecodeError on a non-JSON body
        log.warning("ledger_price_body_invalid", url=url, error=str(exc))
        return None
    if not isinstance(data, list):
        return None
    best: tuple[float, float] | None = None
    for pair in data:
        if not isinstance(pair, dict):
            continue
        price = _price(pair.get("priceUsd"))
        if price is None:
            continue
        liq = _price((pair.get("liquidity") or {}).get("usd")) or 0.0
        if best is None or liq > best[0]:
            best = (liq, price)
    return best[1] if best else None


@dataclass
class LedgerReport:
    measured: int = 0
    failed: int = 0
    total_loss: int = 0
    unmeasurable: int = 0
    skipped: int = 0


async def _resolve_entry(
    store: Store, tid: str, entered_at: str, stored: float | None, now: str
) -> tuple[float | None, bool]:
    """(entry price, whether its search window has closed at ``now``).

    The entry price is the first priced market observation in
    ``[entered_at, entered_at + 48h]``, read no later than ``now``. It is
    resolved once: when found it is frozen on the watchlist row and every
    horizon reuses it.
    """
    if stored is not None:
        return stored, True
    deadline = canon_ts(
        (parse_utc(entered_at) + timedelta(hours=ENTRY_WINDOW_HOURS)).isoformat()
    )
    ep = await entry_price(store, tid, entered_at, as_of=min(now, deadline))
    if ep is not None:
        await store.set_entry_price(tid, ep)
    return ep, now > deadline


async def _finalize_no_entry(
    store: Store, tid: str, btc_entry: float, btc_now: float, now: str
) -> int:
    """No entry price exists: every open horizon is unmeasurable (ret NULL).

    The token is not marked dead: a missing price at entry says nothing
    about whether it trades now.
    """
    rows = await (
        await store.conn.execute(
            "SELECT horizon_days FROM forward_returns "
            "WHERE token_id=? AND measured_at IS NULL",
            (tid,),
        )
    ).fetchall()
    for (h,) in rows:
        await store.set_return(tid, h, None, 0.0, btc_entry, btc_now, now)
    await store.watchlist_exit(tid, now, "no_entry_price")
    log.info("ledger_entry_unmeasurable", token_id=tid, horizons=len(rows))
    return len(rows)


async def measure_due(
    store: Store,
    settings: GeckoSettings,
    session: aiohttp.ClientSession,
    now: str,
) -> LedgerReport:
    now = canon_ts(now)
    rep = LedgerReport()
    closed: set[str] = set()
    for due in await store.due_returns(now):
        tid, h = due.token_id, due.horizon_days
        if tid in closed:
            continue  # finalized whole this run
        trow = await (
            await store.conn.execute(
                "SELECT chain, address FROM tokens WHERE token_id=?", (tid,)
            )
        ).fetchone()
        wrow = await (
            await store.conn.execute(
                "SELECT entered_at, entry_price FROM watchlist WHERE token_id=?",
                (tid,),
            )
        ).fetchone()
        if not trow or not wrow:
            log.warning("ledger_row_orphaned", token_id=tid, horizon_days=h)
            rep.skipped += 1
            continue
        chain, address = trow
        entered_at = wrow[0]
        # A BTC gap skips the row without spending an attempt. A gap at ``now``
        # heals on a later run; a gap at ``entered_at`` cannot (nothing
        # backfills benchmark history), so that row stays due and keeps logging.
        btc_entry = await btc_price_at(store, entered_at)
        btc_now = await btc_price_at(store, now)
        if btc_entry is None or btc_now is None:
            log.warning(
                "ledger_btc_benchmark_missing",
                token_id=tid,
                horizon_days=h,
                btc_entry_missing=btc_entry is None,
                btc_now_missing=btc_now is None,
            )
            rep.skipped += 1
            continue
        ep, window_closed = await _resolve_entry(store, tid, entered_at, wrow[1], now)
        if ep is None:
            if window_closed:
                rep.unmeasurable += await _finalize_no_entry(
                    store, tid, btc_entry, btc_now, now
                )
                closed.add(tid)
            else:
                log.info("ledger_entry_price_pending", token_id=tid, horizon_days=h)
                rep.skipped += 1
            continue
        try:
            price = await fetch_price(session, settings, chain, address)
        except UpstreamError as exc:
            if exc.status in TRANSIENT_STATUSES or exc.status >= 500:
                # Transient (incl. 403, which DexScreener can return for a
                # whole region or IP block): not evidence the token is gone.
                log.warning(
                    "ledger_fetch_transient",
                    token_id=tid,
                    horizon_days=h,
                    status=exc.status,
                )
                rep.skipped += 1
                continue
            # Any other 4xx is a definite refusal: count it as a missing price.
            log.warning(
                "ledger_fetch_rejected",
                token_id=tid,
                horizon_days=h,
                status=exc.status,
            )
            price = None
        try:
            if price is not None:
                await store.set_return(tid, h, ep, price, btc_entry, btc_now, now)
                rep.measured += 1
                continue
            attempts = await store.bump_return_attempt(tid, h)
            if attempts < MAX_ATTEMPTS:
                rep.failed += 1
                continue
            await store.set_return(tid, h, ep, 0.0, btc_entry, btc_now, now)
        except ReturnAlreadyMeasured as exc:
            log.warning(
                "ledger_row_already_measured",
                token_id=tid,
                horizon_days=h,
                error=str(exc),
            )
            rep.skipped += 1
            continue
        await store.set_token_status(tid, "dead", "no_price")
        rep.total_loss += 1
        log.info(
            "ledger_row_finalized_dead",
            token_id=tid,
            horizon_days=h,
            reason="no_price",
            attempts=attempts,
        )
    log.info("ledger_done", **asdict(rep))
    return rep


@register("ledger")
async def ledger_job(settings: GeckoSettings, ns) -> int:
    now = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store, session):
        r = await measure_due(store, settings, session, now)
        return JobResult(rows_written=r.measured + r.total_loss + r.unmeasurable)

    return await run_job("ledger", settings, body)
