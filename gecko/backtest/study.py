"""Reproduction of the 2026-09-18 long-hold study (prereg ``f7d38fa2``).

Reads the study's frozen inputs (``events.json``, ``btc.json``, ``charts/``,
``v2/coins/``) plus the flag-time prices stored in the historical ``scout.db``
and applies the study's rules exactly, so a rule's battery here can be checked
against the published anchor row before it is trusted for anything new.
"""

from __future__ import annotations

import bisect
import json
import os
import sqlite3
import statistics as st
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from gecko.features import vol_ratio_from_candles
from gecko.settings import GeckoSettings

STORED_QUERIES = [
    "SELECT coin_id, predicted_at, price_at_prediction FROM predictions",
    "SELECT token_id, opened_at, entry_price FROM paper_trades WHERE opened_at>'2020'",
    "SELECT coin_id, detected_at, current_price FROM velocity_alerts",
    "SELECT coin_id, detected_at, current_price FROM slow_burn_candidates",
    "SELECT coin_id, detected_at, current_price FROM gainer_acceleration",
    "SELECT coin_id, detected_at, current_price FROM momentum_7d",
    "SELECT coin_id, detected_at, price FROM volume_spikes",
]
CHART_TOLERANCE_MS = 36 * 3600 * 1000
STORED_BAND = (0.5, 2.0)
RULES = ("R0", "F1", "F3", "F4")
log = structlog.get_logger("gecko.backtest.study")
DAY_SEC = 86400


@dataclass
class Coin:
    cid: str
    t0: str
    dead: bool
    p0: float = 0.0
    m0: float = 0.0
    x: float = 0.0
    bx: float = 0.0
    surf: set = field(default_factory=set)
    age_days: float | None = None
    vol_ratio: float | None = None  # study definition (drives the rules)
    live_vol_ratio: float | None = None  # gecko.features definition (informational)
    chain: str = "none"


def _read_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _ms(iso: str) -> float:
    d = datetime.fromisoformat(iso.replace("Z", ""))
    return d.replace(tzinfo=timezone.utc).timestamp() * 1000


def _at(series: list, times: list, t: float):
    i = bisect.bisect_left(times, t)
    i = min(max(i, 0), len(series) - 1)
    if i > 0 and abs(times[i - 1] - t) < abs(times[i] - t):
        i -= 1
    return series[i]


def study_vol_ratio(ohlcv: list, t0: datetime) -> float | None:
    """The study's vol ratio: sum(v, [t0-7d, t0)) / sum(v, [t0-14d, t0-7d)).

    Same windows as ``gecko.features.vol_ratio_from_candles``, and neither
    has a candle minimum. They differ only in hardening and one edge: the
    live one collapses duplicate timestamps, skips malformed candles, and
    returns None when the last week holds no candle (this returns 0.0).
    Both return None when the prior week sums to zero.
    """
    t = t0.timestamp()
    a = sum(c[5] for c in ohlcv if t - 7 * DAY_SEC <= c[0] < t)
    b = sum(c[5] for c in ohlcv if t - 14 * DAY_SEC <= c[0] < t - 7 * DAY_SEC)
    return a / b if b > 0 else None


def _stored_prices(history_db: str | None) -> dict[str, tuple[str, float]]:
    """Earliest stored flag-time price per coin across the seven flag tables."""
    out: dict[str, tuple[str, float]] = {}
    if not history_db:
        return out
    if not os.path.exists(history_db):
        # A mistyped path would silently skip the chart/stored exclusion.
        raise FileNotFoundError(f"history db not found: {history_db}")
    with closing(
        sqlite3.connect(f"file:{history_db}?mode=ro&immutable=1", uri=True)
    ) as conn:
        for q in STORED_QUERIES:
            try:
                rows = conn.execute(q).fetchall()
            except sqlite3.OperationalError:
                continue  # table absent from this history snapshot
            for cid, ts, p in rows:
                if cid and p and p > 0:
                    ts = str(ts).replace(" ", "T")[:19]
                    if cid not in out or ts < out[cid][0]:
                        out[cid] = (ts, float(p))
    return out


def load_study(
    study_dir: str, history_db: str | None, stats: dict | None = None
) -> list[Coin]:
    """Live (charted, priced) study coins after the chart/stored exclusion.

    With a ``history_db``, a coin with no stored flag price is excluded too:
    the chart/stored check cannot see a re-denominated chart without one.

    ``stats``, when given, receives counts: universe, stored, excluded,
    excluded_no_price, rejected (path-like ids), dead.
    """
    ev = _read_json(os.path.join(study_dir, "events.json"))
    btc = _read_json(os.path.join(study_dir, "btc.json"))["prices"]
    bt = [p[0] for p in btc]
    stored = _stored_prices(history_db)
    coins: list[Coin] = []
    excluded = excluded_no_price = rejected = 0
    for cid, e in ev.items():
        if os.path.basename(cid) != cid:
            rejected += 1  # a path-like id would read outside charts/ or v2/coins/
            continue
        f = os.path.join(study_dir, "charts", f"{cid}.json")
        if not os.path.exists(f):
            continue
        d = _read_json(f)
        pr = d.get("prices") or []
        mc = d.get("market_caps") or []
        t0s = min(x[0] for x in e)
        if len(pr) < 2:
            coins.append(Coin(cid, t0s, True))
            continue
        t0 = _ms(t0s)
        pt = [p[0] for p in pr]
        p0 = _at(pr, pt, t0)
        if abs(p0[0] - t0) > CHART_TOLERANCE_MS or p0[1] <= 0:
            coins.append(Coin(cid, t0s, True))
            continue
        if history_db and cid not in stored:
            excluded_no_price += 1  # unverifiable: may be a re-denominated chart
            continue
        if cid in stored and not (
            STORED_BAND[0] <= p0[1] / stored[cid][1] <= STORED_BAND[1]
        ):
            excluded += 1  # chart/stored disagreement > 2x: excluded (study rule)
            continue
        m0 = _at(mc, [m[0] for m in mc], t0)[1] if mc else 0.0
        b0 = _at(btc, bt, t0)[1]
        c = Coin(
            cid,
            t0s,
            False,
            p0=p0[1],
            m0=m0 or 0.0,
            x=pr[-1][1] / p0[1],
            bx=btc[-1][1] / b0,
            surf={x[1] for x in e},
        )
        v2 = os.path.join(study_dir, "v2", "coins", f"{cid}.json")
        if os.path.exists(v2):
            r = _read_json(v2)
            pf = r.get("platforms") or []
            c.chain = pf[0] if len(pf) == 1 else ("multi" if pf else "none")
            if r.get("pool_created_at"):
                c.age_days = (t0 - _ms(r["pool_created_at"])) / 86400000
            if r.get("ohlcv"):
                t0_dt = datetime.fromtimestamp(t0 / 1000, tz=timezone.utc)
                c.vol_ratio = study_vol_ratio(r["ohlcv"], t0_dt)
                c.live_vol_ratio = vol_ratio_from_candles(r["ohlcv"], t0_dt)
        coins.append(c)
    live = [c for c in coins if not c.dead]
    if history_db:
        log.info("study_excluded_no_price", n=excluded_no_price)
    if stats is not None:
        stats.update(
            universe=len(ev),
            stored=len(stored),
            excluded=excluded,
            excluded_no_price=excluded_no_price,
            rejected=rejected,
            dead=len(coins) - len(live),
        )
    return live


def _f1(c: Coin, s: GeckoSettings) -> bool:
    return c.age_days is not None and c.age_days <= s.f4_max_age_days


def _f3(vr: float | None, s: GeckoSettings) -> bool:
    return vr is not None and vr >= s.f4_min_vol_ratio


def apply_rule(coins: list[Coin], rule_id: str, settings: GeckoSettings) -> list[Coin]:
    if rule_id not in RULES:
        raise ValueError(f"unknown rule {rule_id!r}; expected one of {RULES}")
    rules = {
        "R0": lambda c: True,
        "F1": lambda c: _f1(c, settings),
        "F3": lambda c: _f3(c.vol_ratio, settings),
        "F4": lambda c: _f1(c, settings) and _f3(c.vol_ratio, settings),
    }
    return [c for c in coins if rules[rule_id](c)]


def f4_divergence(coins: list[Coin], settings: GeckoSettings) -> int:
    """Coins whose F4 verdict flips under the live vol-ratio definition."""
    return sum(
        _f3(c.vol_ratio, settings) != _f3(c.live_vol_ratio, settings)
        for c in coins
        if _f1(c, settings)
    )


@dataclass
class Battery:
    n: int
    mean: float
    median: float
    btc_mean: float
    ex_best: float
    ex_top3: float
    same_month_r0: float
    pct_7x: float
    pct_half: float
    halves: str
    passes: list[bool]
    passed: bool


def battery(
    selected: list[Coin], universe: list[Coin], btc_split: str = "2026-07-01"
) -> Battery:
    if len(selected) < 2:
        return Battery(len(selected), 0, 0, 0, 0, 0, 0, 0, 0, "-", [False] * 5, False)
    xs = sorted(c.x for c in selected)
    m = st.fmean(xs)
    btc = st.fmean(c.bx for c in selected)
    month_mean = {
        mo: st.fmean(c.x for c in universe if c.t0[:7] == mo)
        for mo in {c.t0[:7] for c in universe}
    }
    base = st.fmean(month_mean[c.t0[:7]] for c in selected)
    h1 = [c for c in selected if c.t0 < btc_split]
    h2 = [c for c in selected if c.t0 >= btc_split]
    label = btc_split
    if len(h1) < 20 or len(h2) < 20:
        med = sorted(c.t0 for c in selected)[len(selected) // 2]
        h1 = [c for c in selected if c.t0 < med]
        h2 = [c for c in selected if c.t0 >= med]
        label = "median " + med[:10]

    def hm(v: list[Coin]) -> float:
        return st.fmean(c.x for c in v) if v else 0.0

    def hb(v: list[Coin]) -> float:
        return st.fmean(c.bx for c in v) if v else 0.0

    passes = [
        m > 1.0 and m > btc,
        st.fmean(xs[:-1]) > btc,
        m > base,
        bool(h1 and h2 and hm(h1) > hb(h1) and hm(h2) > hb(h2)),
        20 <= len(selected) <= 200,
    ]
    halves = (
        f"[{label}] {hm(h1):.2f}/{hb(h1):.2f} n={len(h1)} | "
        f"{hm(h2):.2f}/{hb(h2):.2f} n={len(h2)}"
    )
    return Battery(
        len(selected),
        m,
        st.median(xs),
        btc,
        st.fmean(xs[:-1]),
        st.fmean(xs[:-3]) if len(xs) > 3 else st.fmean(xs),
        base,
        100 * sum(a >= 7 for a in xs) / len(xs),
        100 * sum(a <= 0.5 for a in xs) / len(xs),
        halves,
        passes,
        all(passes),
    )
