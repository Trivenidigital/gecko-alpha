from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import timedelta

import structlog

from gecko.features import Features, compute_features
from gecko.jobs import register
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, parse_utc, utcnow

log = structlog.get_logger("gecko.rank")
RULE_ID = "F4"
SCORE_KEYS = (
    "vol_7d_ratio",
    "holder_growth_7d",
    "smart_money_wallets",
    "caller_signal",
)
WEIGHTS = {
    "vol_7d_ratio": 1.0,
    "holder_growth_7d": 1.0,
    "smart_money_wallets": 0.5,
    "caller_signal": 0.5,
}


def disqualify(f: Features, settings: GeckoSettings) -> str | None:
    """First disqualifier that fires, checked in a fixed order, or None."""
    if f.security_ok is False:
        return "security_fail"
    if f.top10_pct is not None and f.top10_pct > settings.top10_disqualify_pct:
        return "concentration"
    if f.liquidity_usd is None or f.liquidity_usd < settings.min_liquidity_usd:
        return "liquidity"
    if f.observed_at is None or parse_utc(f.as_of) - parse_utc(
        f.observed_at
    ) > timedelta(days=settings.dead_after_days):
        return "dead"
    return None


def rule_f4(f: Features, settings: GeckoSettings) -> bool:
    if f.age_days is None or f.age_days > settings.f4_max_age_days:
        return False
    if f.vol_7d_ratio is None or f.vol_7d_ratio < settings.f4_min_vol_ratio:
        return False
    if f.security_ok is False or (
        f.security_ok is None and settings.require_security_known
    ):
        return False
    return not f.bundle_flag


def _raw(f: Features) -> dict[str, float | None]:
    return {
        "vol_7d_ratio": f.vol_7d_ratio,
        "holder_growth_7d": f.holder_growth_7d,
        "smart_money_wallets": f.smart_money_wallets,
        "caller_signal": (
            None if f.callers_7d is None else f.callers_7d * (f.caller_quality or 0.0)
        ),
    }


def stats_for(feats: list[Features]) -> dict[str, tuple[float, float]]:
    """Population (mean, std) per score key over the candidate set."""
    raws = [_raw(f) for f in feats]
    out: dict[str, tuple[float, float]] = {}
    for k in SCORE_KEYS:
        vals = [r[k] for r in raws if r[k] is not None]
        mean = statistics.fmean(vals) if vals else 0.0
        std = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        out[k] = (mean, std)
    return out


def score(f: Features, stats: dict[str, tuple[float, float]]) -> float:
    s = 0.0
    raw = _raw(f)
    for k in SCORE_KEYS:
        v = raw[k]
        mean, std = stats[k]
        if v is None or std == 0:
            continue
        s += WEIGHTS[k] * (v - mean) / std
    if f.bundle_flag:
        s -= 1.0
    return s


@dataclass
class RankReport:
    entered: list[str]
    exited: list[str]
    ranked: int


async def _column(store: Store, sql: str) -> list[str]:
    return [r[0] for r in await (await store.conn.execute(sql)).fetchall()]


async def _exit_reason(store: Store, tid: str, reasons: dict[str, str]) -> str:
    """This run's disqualifier, else the token's stored one, else a default."""
    if tid in reasons:
        return reasons[tid]
    row = await (
        await store.conn.execute(
            "SELECT disqualify_reason FROM tokens WHERE token_id=?", (tid,)
        )
    ).fetchone()
    return (row[0] if row else None) or "disqualified"


async def rank_once(store: Store, settings: GeckoSettings, as_of: str) -> RankReport:
    as_of = canon_ts(as_of)
    # Watchlist state is not point-in-time: ranking at an earlier as_of than
    # state already written would mix futures into the past. Refuse, before
    # any write.
    latest = await (
        await store.conn.execute(
            "SELECT MAX(last_ranked_at), MAX(exited_at) FROM watchlist"
        )
    ).fetchone()
    if any(ts is not None and as_of < ts for ts in latest):
        raise ValueError("as_of rewinds ranking state")
    feats: dict[str, Features] = {}
    reasons: dict[str, str] = {}
    for tid in await _column(
        store, "SELECT token_id FROM tokens WHERE status='active'"
    ):
        f = await compute_features(store, settings, tid, as_of)
        why = disqualify(f, settings)
        if why:
            status = "dead" if why == "dead" else "disqualified"
            await store.set_token_status(tid, status, why)
            reasons[tid] = why
            continue
        feats[tid] = f

    current = {w.token_id: w for w in await store.active_watchlist()}
    exited: list[str] = []
    for tid, w in current.items():
        if tid not in feats:
            # Not active this run: this run's disqualifier, or left `active`
            # some other way (the ledger's dead/no_price).
            await store.watchlist_exit(
                tid, as_of, await _exit_reason(store, tid, reasons)
            )
            exited.append(tid)
        elif parse_utc(as_of) - parse_utc(w.entered_at) > timedelta(
            days=settings.watchlist_max_age_days
        ):
            await store.watchlist_exit(tid, as_of, "max_age")
            exited.append(tid)
    for tid in exited:
        del current[tid]

    # v1 has no re-entry: a token that ever exited stays out.
    ever_exited = set(
        await _column(
            store, "SELECT token_id FROM watchlist WHERE exited_at IS NOT NULL"
        )
    )
    candidates = {
        tid
        for tid, f in feats.items()
        if tid not in current and tid not in ever_exited and rule_f4(f, settings)
    }
    stats = stats_for([feats[t] for t in set(current) | candidates])
    scores = {t: score(feats[t], stats) for t in set(current) | candidates}

    def order(ids) -> list[str]:
        return sorted(ids, key=lambda t: (-scores[t], t))

    slots = max(0, settings.watchlist_cap - len(current))
    entering = order(candidates)[:slots]
    final = order(set(current) | set(entering))
    for rank, tid in enumerate(final, start=1):
        await store.watchlist_upsert(
            tid, as_of, RULE_ID, rank, scores[tid], feats[tid].as_dict()
        )
    for tid in entering:
        await store.schedule_returns(tid, as_of)
    log.info(
        "rank_done",
        as_of=as_of,
        ranked=len(final),
        entered=len(entering),
        exited=len(exited),
    )
    return RankReport(entering, exited, len(final))


@register("rank")
async def rank_job(settings: GeckoSettings, ns) -> int:
    as_of = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store, session):
        r = await rank_once(store, settings, as_of)
        return JobResult(rows_written=r.ranked)

    return await run_job("rank", settings, body)
