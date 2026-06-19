"""Prospective conviction watchlist builder (BL-NEW-CONVICTION-PROSPECTIVE-SCORE V1).

Snapshots CURRENT not-yet-pumped CoinGecko coins scored by SUSTAINED (>=24h)
cross-surface early confirmation. Identity = CG ``coin_id`` (exact match only — NO
symbol merge; see spec Fold 2). Excludes coins already on the gainers tracker via
BOTH ``gainers_snapshots`` and ``gainers_comparisons`` (Fold 1). Observe-only: writes
the snapshot table, no alerts/trades.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import structlog

from scout.conviction import TIER_ORDER
from scout.conviction.prospective_scorer import score_prospective

logger = structlog.get_logger()

# (surface, "SELECT <coin_id key> AS k, MIN(<time>) FROM ... WHERE <window> GROUP BY k")
# All CG-coin_id-keyed (Fold 2): pipeline = chain='coingecko' candidates only;
# chains = exact signal_events.token_id (no symbol/LIKE merge).
_SURFACE_SOURCES: tuple[tuple[str, str, str], ...] = (
    (
        "narrative",
        "predictions",
        "SELECT coin_id AS k, MIN(predicted_at) AS t FROM predictions WHERE coin_id IS NOT NULL AND datetime(predicted_at) >= datetime(?) GROUP BY coin_id",
    ),
    (
        "pipeline",
        "candidates",
        "SELECT contract_address AS k, MIN(first_seen_at) AS t FROM candidates WHERE chain='coingecko' AND datetime(first_seen_at) >= datetime(?) GROUP BY contract_address",
    ),
    (
        "chains",
        "signal_events",
        "SELECT token_id AS k, MIN(created_at) AS t FROM signal_events WHERE token_id IS NOT NULL AND datetime(created_at) >= datetime(?) GROUP BY token_id",
    ),
    (
        "spikes",
        "volume_spikes",
        "SELECT coin_id AS k, MIN(detected_at) AS t FROM volume_spikes WHERE datetime(detected_at) >= datetime(?) GROUP BY coin_id",
    ),
    (
        "acceleration",
        "gainer_acceleration",
        "SELECT coin_id AS k, MIN(detected_at) AS t FROM gainer_acceleration WHERE datetime(detected_at) >= datetime(?) GROUP BY coin_id",
    ),
    (
        "momentum",
        "momentum_7d",
        "SELECT coin_id AS k, MIN(detected_at) AS t FROM momentum_7d WHERE datetime(detected_at) >= datetime(?) GROUP BY coin_id",
    ),
    (
        "slow_burn",
        "slow_burn_candidates",
        "SELECT coin_id AS k, MIN(detected_at) AS t FROM slow_burn_candidates WHERE datetime(detected_at) >= datetime(?) GROUP BY coin_id",
    ),
    (
        "velocity",
        "velocity_alerts",
        "SELECT coin_id AS k, MIN(detected_at) AS t FROM velocity_alerts WHERE datetime(detected_at) >= datetime(?) GROUP BY coin_id",
    ),
)

_ROW_CAP = 2000


def _parse_dt(value: str) -> datetime:
    """Parse a stored timestamp (isoformat or space-format) → tz-aware UTC."""
    s = str(value).strip().replace("Z", "+00:00")
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def build_prospective_watchlist(
    db, settings, *, now: datetime | None = None
) -> dict:
    """Compute + persist one prospective-watchlist snapshot. Returns a run summary."""
    if not getattr(settings, "CONVICTION_PROSPECTIVE_ENABLED", True):
        return {"enabled": False, "rows_written": 0}
    if db._conn is None:
        raise RuntimeError("Database not initialized")
    conn = db._conn
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    cutoff_iso = (
        now - timedelta(days=settings.CONVICTION_PROSPECTIVE_LOOKBACK_DAYS)
    ).isoformat()

    # Fold 1: exclude coins already on the gainers tracker (snapshots OR comparisons).
    excluded: set[str] = set()
    exclusion_ok = True
    for tbl in ("gainers_snapshots", "gainers_comparisons"):
        try:
            cur = await conn.execute(f"SELECT DISTINCT coin_id FROM {tbl}")
            excluded.update(r[0] for r in await cur.fetchall() if r[0])
        except Exception:
            exclusion_ok = False
            logger.exception("conviction_prospective_exclusion_query_failed", table=tbl)

    # Fold B: FAIL CLOSED. Without a reliable exclusion set, an already-pumped coin
    # could leak into the FORWARD watchlist — so skip writing the snapshot entirely,
    # but STILL write a run heartbeat so the freshness watchdog sees the builder ran.
    if not exclusion_ok:
        run = {
            "run_at": now_iso,
            "status": "skipped_exclusion_failed",
            "rows_written": 0,
            "high_tier": 0,
            "sub30m_high_fresh": 0,
            "per_surface_contrib": {},
            "truncated": False,
        }
        await db.insert_conviction_watchlist_run(run)
        logger.warning("conviction_prospective_skipped_exclusion_failed", **run)
        return run

    # Per-surface MIN(detection) grouped by CG coin_id within the lookback window.
    ages: dict[str, dict[str, float]] = defaultdict(dict)
    per_surface_contrib: dict[str, int] = {}
    for surface, _tbl, query in _SURFACE_SOURCES:
        try:
            cur = await conn.execute(query, (cutoff_iso,))
            n = 0
            for key, min_t in await cur.fetchall():
                if not key or not min_t:
                    continue
                age = (now - _parse_dt(min_t)).total_seconds() / 60.0
                if age < 0:
                    continue
                ages[key][surface] = age
                n += 1
            per_surface_contrib[surface] = n
        except Exception:
            logger.exception(
                "conviction_prospective_surface_query_failed", surface=surface
            )
            per_surface_contrib[surface] = -1

    # Score the universe (excluding already-pumped); keep tier >= watch (full denominator).
    watch_idx = TIER_ORDER.index("watch")
    rows: list[dict] = []
    for coin_id, surface_ages in ages.items():
        if coin_id in excluded:
            continue
        res = score_prospective(surface_ages, settings)
        if TIER_ORDER.index(res.tier) < watch_idx:
            continue
        rows.append(
            {
                "coin_id": coin_id,
                "early_count": res.early_count,
                "fresh_count": res.fresh_count,
                "tier": res.tier,
                "contributing_surfaces": list(res.contributing),
                "first_detection_ages": {
                    s: round(a, 1) for s, a in surface_ages.items()
                },
            }
        )

    rows.sort(key=lambda r: (r["early_count"], r["coin_id"]), reverse=True)
    truncated = len(rows) > _ROW_CAP
    rows = rows[:_ROW_CAP]

    # Enrich mcap (+ staleness) and symbol/name.
    coin_ids = [r["coin_id"] for r in rows]
    mcap_map: dict[str, tuple] = {}
    if coin_ids:
        placeholders = ",".join("?" * len(coin_ids))
        cur = await conn.execute(
            f"SELECT coin_id, market_cap, updated_at FROM price_cache WHERE coin_id IN ({placeholders})",
            coin_ids,
        )
        for cid, mc, upd in await cur.fetchall():
            mcap_map[cid] = (mc, upd)
    for r in rows:
        mc, upd = mcap_map.get(r["coin_id"], (None, None))
        r["market_cap"] = mc
        r["mcap_age_minutes"] = (
            round((now - _parse_dt(upd)).total_seconds() / 60.0, 1) if upd else None
        )
        try:
            sym, name = await db.lookup_symbol_name_by_coin_id(r["coin_id"])
        except Exception:
            sym, name = None, None
        r["symbol"] = sym or r["coin_id"].upper()
        r["name"] = name or r["coin_id"]

    await db.insert_conviction_watchlist_snapshot(rows, now_iso)

    max_mcap = settings.CONVICTION_WATCHLIST_MAX_MCAP
    max_age = settings.CONVICTION_WATCHLIST_MCAP_MAX_AGE_MINUTES
    sub30m_high_fresh = sum(
        1
        for r in rows
        if r["tier"] == "high"
        and r["market_cap"] is not None
        and r["market_cap"] < max_mcap
        and r["mcap_age_minutes"] is not None
        and r["mcap_age_minutes"] <= max_age
    )
    # P1 (silent recall hole): a per-surface query failure (-1 sentinel) means
    # the cohort is under-counted — a genuine `high` coin can silently drop a
    # tier or fall below the watch denominator and vanish. The snapshot's present
    # rows are still true positives (they met tier on the surviving surfaces), so
    # we keep them; but the run is NOT healthy. Flag it non-`ok` so the watchdog's
    # existing `status != "ok"` branch turns it into an operator alert, and the
    # dashboard (which now reads run status) can mark the batch degraded.
    degraded = any(v == -1 for v in per_surface_contrib.values())
    summary = {
        "rows_written": len(rows),
        "high_tier": sum(1 for r in rows if r["tier"] == "high"),
        "sub30m_high_fresh": sub30m_high_fresh,
        "per_surface_contrib": per_surface_contrib,
        "truncated": truncated,
        "snapshot_at": now_iso,
        "status": "degraded_surface_failed" if degraded else "ok",
    }
    # Fold A: always record a run heartbeat (even a 0-row run) keyed off run_at.
    await db.insert_conviction_watchlist_run({**summary, "run_at": now_iso})
    logger.info("conviction_prospective_snapshot_written", **summary)
    return summary
