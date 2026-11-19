from __future__ import annotations

import json
from datetime import timedelta

from gecko.numeric import num
from gecko.settings import GeckoSettings
from gecko.store import MARKET_SOURCES, Store, canon_ts, parse_utc

SCAN_DAYS = 7  # admission looks back this far; older rows cannot matter


def in_band(
    settings: GeckoSettings,
    *,
    mcap_usd: float | None,
    liquidity_usd: float | None,
    chain: str,
) -> bool:
    if chain not in settings.chains:
        return False
    if liquidity_usd is None or liquidity_usd < settings.min_liquidity_usd:
        return False
    if mcap_usd is not None and not (
        settings.min_mcap_usd <= mcap_usd <= settings.max_mcap_usd
    ):
        return False
    return True


async def admit_survivors(store: Store, settings: GeckoSettings, now: str) -> int:
    now = canon_ts(now)
    q = ",".join("?" * len(MARKET_SOURCES))
    since = canon_ts((parse_utc(now) - timedelta(hours=48)).isoformat())
    scan_from = canon_ts((parse_utc(now) - timedelta(days=SCAN_DAYS)).isoformat())
    rows = await (
        await store.conn.execute(
            f"SELECT o.token_id, o.observed_at, o.features_json FROM observations o "
            f"JOIN tokens t ON t.token_id=o.token_id WHERE t.status='candidate' "
            # v1 has no re-entry: a demoted (tax) watchlist exit stays a candidate
            f"AND t.token_id NOT IN (SELECT token_id FROM watchlist "
            f"WHERE exited_at IS NOT NULL) "
            f"AND o.source IN ({q}) AND o.observed_at>=? AND o.observed_at<=? "
            f"ORDER BY o.token_id, o.observed_at, o.id",
            (*MARKET_SOURCES, scan_from, now),
        )
    ).fetchall()
    by_token: dict[str, list[tuple[str, float]]] = {}
    for tid, obs_at, fj in rows:
        liq = num(json.loads(fj).get("liquidity_usd"))
        if liq is None:
            continue  # e.g. coingecko rows: no liquidity, cannot decide admission
        obs = by_token.setdefault(tid, [])
        if obs and obs[-1][0] == obs_at:
            # Several pools read at one instant: the deepest represents the token.
            obs[-1] = (obs_at, max(obs[-1][1], liq))
        else:
            obs.append((obs_at, liq))
    floor = settings.min_liquidity_usd
    promoted = 0
    for tid, obs in by_token.items():
        latest_at, latest_liq = obs[-1]
        if latest_at < since or latest_liq < floor:
            continue
        # The survival window must be contiguous: anchors only after the last dip.
        start = 0
        for i, (_, liq) in enumerate(obs):
            if liq < floor:
                start = i + 1
        cutoff = parse_utc(latest_at) - timedelta(hours=settings.survivor_hours)
        if any(parse_utc(at) <= cutoff for at, _ in obs[start:-1]):
            await store.set_token_status(tid, "active")
            promoted += 1
    return promoted
