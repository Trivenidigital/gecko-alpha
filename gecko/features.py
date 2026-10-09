"""Point-in-time token features.

Every value is computed only from rows whose timestamp is at or before
``as_of``: observations by ``observed_at``, source calls by ``posted_at``,
forward returns by ``measured_at``. Nothing here may read a row written after
``as_of``, so the same function serves live ranking and backtests.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from gecko.settings import GeckoSettings
from gecko.store import Observation, Store, canon_ts, parse_utc

MARKET_SOURCES = ("dexscreener", "geckoterminal", "coingecko")
OHLCV_SOURCE = "geckoterminal_ohlcv"
ENRICH_SOURCE = "enrich"
YOUNG_CHAIN_DAYS = 180
BUNDLE_DD_BELOW = 60
BUNDLE_TOP10_ABOVE = 40
MIN_CANDLES = 14
DAY_SEC = 86400


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Features:
    token_id: str
    as_of: str
    observed_at: str | None = None
    price_usd: float | None = None
    mcap_usd: float | None = None
    liquidity_usd: float | None = None
    vol_24h_usd: float | None = None
    age_days: float | None = None
    vol_7d_ratio: float | None = None
    holders: int | None = None
    holder_growth_7d: float | None = None
    top10_pct: float | None = None
    bundle_flag: bool = False
    smart_money_wallets: int | None = None
    callers_7d: int = 0
    caller_quality: float | None = None
    narrative_mentions_7d: int = 0
    security_ok: bool | None = None
    young_chain: bool = False
    momentum_ctx: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _first(obs: list[Observation], key: str):
    """Newest non-None value of ``key`` in newest-first ``obs``."""
    for o in obs:
        v = o.features.get(key)
        if v is not None:
            return v
    return None


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def vol_ratio_from_candles(candles: list, as_of: datetime) -> float | None:
    """Last-7-day over prior-7-day volume from daily candles.

    ``candles`` is ``[[ts_sec, o, h, l, c, vol_usd], ...]``. A candle is usable
    only if it is a list of exactly 6 elements whose timestamp and volume are
    finite numbers and whose timestamp is before ``as_of``. Returns None when
    fewer than 14 usable candles exist or the prior-week volume is zero.
    """
    t = as_of.timestamp()
    usable = [
        c
        for c in candles
        if isinstance(c, list)
        and len(c) == 6
        and _finite(c[0])
        and _finite(c[5])
        and c[0] < t
    ]
    if len(usable) < MIN_CANDLES:
        return None
    last = sum(c[5] for c in usable if t - 7 * DAY_SEC <= c[0] < t)
    prior = sum(c[5] for c in usable if t - 14 * DAY_SEC <= c[0] < t - 7 * DAY_SEC)
    if prior <= 0:
        return None
    return last / prior


async def _market_obs(store: Store, token_id: str, as_of: str) -> list[Observation]:
    obs: list[Observation] = []
    for src in MARKET_SOURCES:
        obs.extend(
            await store.observations(token_id, as_of=as_of, source=src, limit=60)
        )
    obs.sort(key=lambda o: o.observed_at, reverse=True)
    return obs


async def _latest_candles(store: Store, token_id: str, as_of: str) -> list:
    """Candles of the newest OHLCV observation that has any; markers are skipped."""
    for o in await store.observations(
        token_id, as_of=as_of, source=OHLCV_SOURCE, limit=10
    ):
        c = o.features.get("candles")
        if isinstance(c, list) and c:
            return c
    return []


async def compute_features(
    store: Store, settings: GeckoSettings, token_id: str, as_of: str
) -> Features:
    as_of = canon_ts(as_of)
    f = Features(token_id=token_id, as_of=as_of)
    as_of_dt = parse_utc(as_of)
    trow = await (
        await store.conn.execute(
            "SELECT chain, first_seen_at, pool_created_at FROM tokens WHERE token_id=?",
            (token_id,),
        )
    ).fetchone()
    if trow is None:
        return f
    chain, first_seen, pool_created = trow
    week_ago = _iso(as_of_dt - timedelta(days=7))

    market = await _market_obs(store, token_id, as_of)
    if market:
        f.observed_at = market[0].observed_at
    for key in ("price_usd", "mcap_usd", "liquidity_usd", "vol_24h_usd"):
        v = _first(market, key)
        setattr(f, key, float(v) if v is not None else None)

    birth = min(d for d in (first_seen, pool_created) if d)
    f.age_days = (as_of_dt - parse_utc(birth)).total_seconds() / DAY_SEC

    f.vol_7d_ratio = vol_ratio_from_candles(
        await _latest_candles(store, token_id, as_of), as_of_dt
    )

    # Separate query at week_ago: dense recent observations must not push the
    # week-old price out of a row-capped window.
    p_then = _first(await _market_obs(store, token_id, week_ago), "price_usd")
    if f.price_usd and p_then:
        f.momentum_ctx = f.price_usd / float(p_then) - 1.0

    enrich = await store.observations(
        token_id, as_of=as_of, source=ENRICH_SOURCE, limit=30
    )
    if enrich:
        h = _first(enrich, "holders")
        f.holders = int(h) if h is not None else None
        t10 = _first(enrich, "top10_pct")
        f.top10_pct = float(t10) if t10 is not None else None
        sm = _first(enrich, "smart_money_wallets")
        f.smart_money_wallets = int(sm) if sm is not None else None
        sec = _first(enrich, "security_ok")
        f.security_ok = bool(sec) if sec is not None else None
        dd = _first(enrich, "dd_score")
        old = await store.observations(
            token_id, as_of=week_ago, source=ENRICH_SOURCE, limit=30
        )
        h_then = _first(old, "holders")
        if f.holders is not None and h_then:
            f.holder_growth_7d = (f.holders - int(h_then)) / int(h_then)
        f.bundle_flag = bool(
            (dd is not None and float(dd) < BUNDLE_DD_BELOW)
            or (f.top10_pct is not None and f.top10_pct > BUNDLE_TOP10_ABOVE)
        )

    rows = await (
        await store.conn.execute(
            "SELECT source_type, source_id FROM source_calls "
            "WHERE token_id=? AND posted_at>? AND posted_at<=?",
            (token_id, week_ago, as_of),
        )
    ).fetchall()
    tg_callers = sorted({sid for st, sid in rows if st == "telegram"})
    f.callers_7d = len(tg_callers)
    f.narrative_mentions_7d = sum(1 for st, _ in rows if st == "x")
    if tg_callers:
        month_ago = _iso(as_of_dt - timedelta(days=30))
        q = ",".join("?" * len(tg_callers))
        # DISTINCT: one vote per (caller, token), however often it was called.
        hist = await (
            await store.conn.execute(
                f"SELECT DISTINCT sc.source_id, sc.token_id, fr.ret_vs_btc "
                f"FROM source_calls sc "
                f"JOIN forward_returns fr ON fr.token_id=sc.token_id "
                f"AND fr.horizon_days=30 "
                f"WHERE sc.source_type='telegram' AND sc.source_id IN ({q}) "
                f"AND sc.posted_at<=? "
                f"AND fr.measured_at IS NOT NULL AND fr.measured_at<=?",
                (*tg_callers, month_ago, as_of),
            )
        ).fetchall()
        per: dict[str, list[float]] = {}
        for sid, _tid, rvb in hist:
            if rvb is not None:
                per.setdefault(sid, []).append(1.0 if rvb > 0 else 0.0)
        rates = [sum(v) / len(v) for v in per.values() if v]
        f.caller_quality = sum(rates) / len(rates) if rates else None

    launch = settings.young_chain_launch_dates.get(chain)
    if launch:
        f.young_chain = (as_of_dt - parse_utc(launch)).days <= YOUNG_CHAIN_DAYS
    return f
