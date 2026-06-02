"""Gainer-acceleration detector (gap-fill 2026-06-02).

Closes part of the Top-Gainers-Tracker recall gap: tokens that pump +20%/24h
without any pipeline surface seeing them early. This detector reads the
``volume_history_cg`` rows we already store every cycle (zero extra CoinGecko
calls) and flags ``$500K-$200M`` tokens that are *accelerating* -- rising over
BOTH a ~1h and a ~4h window with volume expansion -- BEFORE the 24h move
completes.

Scope (per the Codex xhigh design review):
- Price acceleration (1h AND 4h) is the strong leg; volume expansion is a noisy
  secondary filter because ``volume_history_cg.volume_24h`` is a CG 24h
  *snapshot*, not interval volume. Both are gated, but the detector is
  RESEARCH-ONLY: it persists to ``gainer_acceleration`` and feeds the Top
  Gainers Tracker surface -- it never sends a Telegram alert or opens a
  paper-trade. Precision is measured during soak before any promotion.
- Reachable cohort is only the minority of misses that already have pre-pump
  history (~11-16 of 77). The dominant coverage gap (~61/77 with zero pre-pump
  history) is Increment 2's proactive-scan job, not this detector's.

The structured ``acceleration_scan_complete`` log line is emitted every cycle the
detector RUNS (i.e. CoinGecko markets ingestion was non-empty -- the call is
gated on ``_raw_markets_combined`` in main.py, like every sibling detector) and
is the execution heartbeat (zero detections can be healthy) the watchdog reads --
per global CLAUDE.md Section 12a, watchdog = execution heartbeat, NOT row-rate.
The three journald states stay distinguishable: healthy ->
``acceleration_scan_complete``; crashed -> ``gainer_acceleration_error`` (the
main.py try/except); ingestion dry -> no line (the watchdog's stale-heartbeat
alert text names that cause explicitly).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

import structlog

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.db import Database

logger = structlog.get_logger(__name__)

# (age_hours, price, volume) of one history sample relative to the latest sample.
_AgedSample = tuple[float, Optional[float], Optional[float]]


def _nearest(
    aged: list[_AgedSample], target_h: float, lo_h: float, hi_h: float
) -> Optional[_AgedSample]:
    """Return the sample whose age is closest to ``target_h`` within ``[lo,hi]``.

    Returns ``None`` when no sample falls inside the window, which means the coin
    lacks a usable reference point for that lookback and cannot qualify.
    """
    best: Optional[_AgedSample] = None
    best_dist: Optional[float] = None
    for sample in aged:
        age = sample[0]
        if lo_h <= age <= hi_h:
            dist = abs(age - target_h)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best = sample
    return best


async def detect_acceleration(db: "Database", settings: "Settings") -> list[dict]:
    """Detect $500K-$200M tokens accelerating over stored ``volume_history_cg``.

    Persists qualifying detections to ``gainer_acceleration`` (deduplicated by a
    per-coin cooldown) and returns them as dicts. Zero CoinGecko calls -- reads
    only history already in the DB. Research-only: no alerts, no paper-trades.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    if not settings.ACCELERATION_ENABLED:
        logger.info("acceleration_scan_skipped", enabled=False)
        return []

    conn = db._conn
    now = datetime.now(timezone.utc)
    lookback_cutoff = (
        now - timedelta(hours=settings.ACCELERATION_LOOKBACK_HOURS)
    ).isoformat()
    cooldown_cutoff = (
        now - timedelta(hours=settings.ACCELERATION_DEDUP_HOURS)
    ).isoformat()

    cur = await conn.execute(
        """SELECT coin_id, symbol, name, volume_24h, market_cap, price, recorded_at
           FROM volume_history_cg
           WHERE recorded_at >= ?
           ORDER BY coin_id, recorded_at ASC""",
        (lookback_cutoff,),
    )
    rows = await cur.fetchall()

    # Group consecutive rows (already ordered by coin_id, recorded_at ASC).
    by_coin: dict[str, list] = {}
    for coin_id, symbol, name, volume, mcap, price, recorded_at in rows:
        by_coin.setdefault(coin_id, []).append(
            (recorded_at, price, volume, mcap, symbol, name)
        )

    min_mcap = settings.ACCELERATION_MIN_MCAP
    max_mcap = settings.ACCELERATION_MAX_MCAP
    min_1h = settings.ACCELERATION_MIN_1H_PCT
    min_4h = settings.ACCELERATION_MIN_4H_PCT
    min_vol = settings.ACCELERATION_MIN_VOL_EXPANSION
    min_samples = settings.ACCELERATION_MIN_SAMPLES

    qualified: list[dict] = []
    null_mcap_skipped = 0
    insufficient_samples = 0
    insufficient_window = 0
    out_of_band = 0
    volume_filtered = 0
    cooldown_skipped = 0

    for coin_id, samples in by_coin.items():
        if len(samples) < min_samples:
            insufficient_samples += 1
            continue

        latest = samples[-1]
        recorded_at, price_now, vol_now, mcap_now, symbol, name = latest
        if price_now is None or price_now <= 0:
            continue
        if mcap_now is None:
            null_mcap_skipped += 1
            continue
        if not (min_mcap <= mcap_now <= max_mcap):
            out_of_band += 1
            continue

        t_now = datetime.fromisoformat(recorded_at)
        aged: list[_AgedSample] = []
        for rec, price, volume, _mcap, _sym, _nm in samples:
            age_h = (t_now - datetime.fromisoformat(rec)).total_seconds() / 3600.0
            aged.append((age_h, price, volume))

        ref_1h = _nearest(aged, 1.0, 0.5, 2.0)
        ref_4h = _nearest(aged, 4.0, 2.5, 5.5)
        if ref_1h is None or ref_4h is None:
            insufficient_window += 1
            continue

        price_1h, price_4h, vol_4h = ref_1h[1], ref_4h[1], ref_4h[2]
        if not price_1h or price_1h <= 0 or not price_4h or price_4h <= 0:
            continue

        change_1h = (price_now - price_1h) / price_1h * 100.0
        change_4h = (price_now - price_4h) / price_4h * 100.0
        if change_1h < min_1h or change_4h < min_4h:
            continue

        if not vol_4h or vol_4h <= 0:
            continue  # can't confirm volume expansion -> skip (research-only)
        vol_expansion = (vol_now / vol_4h) if vol_now else 0.0
        if vol_expansion < min_vol:
            volume_filtered += 1
            continue

        dedup = await conn.execute(
            "SELECT 1 FROM gainer_acceleration "
            "WHERE coin_id = ? AND detected_at >= ? LIMIT 1",
            (coin_id, cooldown_cutoff),
        )
        if await dedup.fetchone():
            cooldown_skipped += 1
            continue

        qualified.append(
            {
                "coin_id": coin_id,
                "symbol": symbol,
                "name": name,
                "change_1h": round(change_1h, 4),
                "change_4h": round(change_4h, 4),
                "vol_expansion": round(vol_expansion, 4),
                "market_cap": mcap_now,
                "current_price": price_now,
            }
        )

    # Strongest 1h acceleration first; cap per cycle.
    qualified.sort(key=lambda d: d["change_1h"], reverse=True)
    qualified = qualified[: settings.ACCELERATION_TOP_N]

    now_iso = now.isoformat()
    for d in qualified:
        await conn.execute(
            """INSERT INTO gainer_acceleration
               (coin_id, symbol, name, change_1h, change_4h, vol_expansion,
                market_cap, current_price, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                d["coin_id"],
                d["symbol"],
                d["name"],
                d["change_1h"],
                d["change_4h"],
                d["vol_expansion"],
                d["market_cap"],
                d["current_price"],
                now_iso,
            ),
        )
        d["detected_at"] = now_iso
    if qualified:
        await conn.commit()

    logger.info(
        "acceleration_scan_complete",
        enabled=True,
        coins_evaluated=len(by_coin),
        qualified=len(qualified),
        null_mcap_skipped=null_mcap_skipped,
        insufficient_samples=insufficient_samples,
        insufficient_window=insufficient_window,
        out_of_band=out_of_band,
        volume_filtered=volume_filtered,
        cooldown_skipped=cooldown_skipped,
        lookback_hours=settings.ACCELERATION_LOOKBACK_HOURS,
    )
    return qualified
