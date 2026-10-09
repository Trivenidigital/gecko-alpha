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
DAY_SEC = 86400
THEN_SLACK = timedelta(days=2)  # a "week ago" value may be up to 9 days old
NOW_MAX_AGE = timedelta(days=3)  # a "now" value for growth must be this fresh


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


def _first_obs(obs: list[Observation], key: str):
    """Newest non-None value of ``key`` in newest-first ``obs``, with its row."""
    for o in obs:
        v = o.features.get(key)
        if v is not None:
            return v, o
    return None, None


def _first(obs: list[Observation], key: str):
    return _first_obs(obs, key)[0]


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _num(v) -> float | None:
    """Finite float from a number or numeric string; anything else is None."""
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _int(v) -> int | None:
    x = _num(v)
    return int(x) if x is not None else None


def _valid_candles(candles) -> list:
    """Candles that are 6-element lists with finite timestamp and volume."""
    if not isinstance(candles, list):
        return []
    return [
        c
        for c in candles
        if isinstance(c, list) and len(c) == 6 and _finite(c[0]) and _finite(c[5])
    ]


def vol_ratio_from_candles(candles: list, as_of: datetime) -> float | None:
    """Last-7-day over prior-7-day volume from daily candles.

    This is the registered F4 definition (prereg ``f7d38fa2``):
    ``last = sum(vol, [as_of - 7d, as_of))`` over
    ``prior = sum(vol, [as_of - 14d, as_of - 7d))``. There is no minimum
    candle count; any stricter minimum requires a new prereg.

    ``candles`` is ``[[ts_sec, o, h, l, c, vol_usd], ...]``. A candle is usable
    only if it is a list of exactly 6 elements whose timestamp and volume are
    finite numbers. Duplicate timestamps collapse to the last one. Returns None
    when the prior-week volume is zero or no usable candle lies in the last
    week.
    """
    t = as_of.timestamp()
    lo = t - 14 * DAY_SEC
    mid = t - 7 * DAY_SEC
    by_ts = {c[0]: c for c in _valid_candles(candles) if lo <= c[0] < t}
    recent = [c for c in by_ts.values() if c[0] >= mid]
    prior = sum(c[5] for c in by_ts.values() if c[0] < mid)
    if prior <= 0 or not recent:
        return None
    return sum(c[5] for c in recent) / prior


def _liq_key(o: Observation) -> float:
    liq = _num(o.features.get("liquidity_usd"))
    return liq if liq is not None else -math.inf


async def _market_obs(store: Store, token_id: str, as_of: str) -> list[Observation]:
    """Market rows newest first; rows sharing a timestamp go deepest pool first."""
    obs: list[Observation] = []
    for src in MARKET_SOURCES:
        obs.extend(
            await store.observations(token_id, as_of=as_of, source=src, limit=60)
        )
    obs.sort(key=lambda o: (o.observed_at, _liq_key(o)), reverse=True)
    return obs


async def _latest_candles(store: Store, token_id: str, as_of: str) -> list:
    """Valid candles of the newest OHLCV observation that has any.

    Status markers (``candles: []``) and observations whose candles are all
    malformed are skipped.
    """
    for o in await store.observations(
        token_id, as_of=as_of, source=OHLCV_SOURCE, limit=10
    ):
        c = _valid_candles(o.features.get("candles"))
        if c:
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
    week_ago_dt = as_of_dt - timedelta(days=7)
    week_ago = _iso(week_ago_dt)
    then_floor = _iso(week_ago_dt - THEN_SLACK)

    market = await _market_obs(store, token_id, as_of)
    if market:
        f.observed_at = market[0].observed_at
    for key in ("price_usd", "mcap_usd", "liquidity_usd", "vol_24h_usd"):
        setattr(f, key, _num(_first(market, key)))

    birth = min(d for d in (first_seen, pool_created) if d)
    f.age_days = max(0.0, (as_of_dt - parse_utc(birth)).total_seconds() / DAY_SEC)

    f.vol_7d_ratio = vol_ratio_from_candles(
        await _latest_candles(store, token_id, as_of), as_of_dt
    )

    # Separate query at week_ago: dense recent observations must not push the
    # week-old price out of a row-capped window.
    # The week-old value must lie in [week_ago - 2d, week_ago].
    p_then, p_obs = _first_obs(
        await _market_obs(store, token_id, week_ago), "price_usd"
    )
    p_then = _num(p_then) if p_obs and p_obs.observed_at >= then_floor else None
    if f.price_usd and p_then:
        f.momentum_ctx = f.price_usd / p_then - 1.0

    enrich = await store.observations(
        token_id, as_of=as_of, source=ENRICH_SOURCE, limit=30
    )
    if enrich:
        h, h_obs = _first_obs(enrich, "holders")
        f.holders = _int(h)
        f.top10_pct = _num(_first(enrich, "top10_pct"))
        f.smart_money_wallets = _int(_first(enrich, "smart_money_wallets"))
        sec = _first(enrich, "security_ok")
        f.security_ok = sec if isinstance(sec, bool) else None
        dd = _num(_first(enrich, "dd_score"))
        old = await store.observations(
            token_id, as_of=week_ago, source=ENRICH_SOURCE, limit=30
        )
        h_then, h_then_obs = _first_obs(old, "holders")
        if h_then_obs is None or h_then_obs.observed_at < then_floor:
            h_then = None
        h_then = _int(h_then)
        now_floor = _iso(as_of_dt - NOW_MAX_AGE)
        now_fresh = h_obs is not None and h_obs.observed_at >= now_floor
        if f.holders is not None and h_then and now_fresh:
            f.holder_growth_7d = (f.holders - h_then) / h_then
        f.bundle_flag = bool(
            (dd is not None and dd < BUNDLE_DD_BELOW)
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
                f"AND sc.token_id!=? AND sc.posted_at<=? "
                f"AND fr.measured_at IS NOT NULL AND fr.measured_at<=?",
                (*tg_callers, token_id, month_ago, as_of),
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
        since = as_of_dt - parse_utc(launch)
        f.young_chain = timedelta(0) <= since <= timedelta(days=YOUNG_CHAIN_DAYS)
    return f
