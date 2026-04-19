"""LunarCrush spike detector -- 3 pure checks + orchestrator.

See design spec §5 for rules. Each check is a pure ``(coin, state) -> metric
or None`` function. The orchestrator calls all three, collapses per-coin
hits into a single :class:`ResearchAlert`, applies DB dedup, sorts by
highest-triggered spike ratio, and returns the top-N.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import structlog

from scout.social.baselines import BaselineCache, push_interactions, update_state
from scout.social.models import BaselineState, ResearchAlert, SpikeKind

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.db import Database

logger = structlog.get_logger(__name__)


def _f(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Pure checks
# ---------------------------------------------------------------------------


def check_social_volume_24h_spike(
    coin: dict, state: BaselineState, *, ratio: float
) -> Optional[float]:
    """Return ``current / avg`` when it crosses ``ratio``, else None."""
    current = _f(coin.get("social_volume_24h"))
    if current is None or current <= 0:
        return None
    avg = state.avg_social_volume_24h
    if avg <= 0:
        return None
    hit = current / avg
    if hit >= ratio:
        return hit
    return None


def check_galaxy_jump(
    coin: dict, state: BaselineState, *, min_jump: float
) -> Optional[float]:
    """Return absolute jump when current - last_galaxy_score >= min_jump."""
    current = _f(coin.get("galaxy_score"))
    last = state.last_galaxy_score
    if current is None or last is None:
        return None
    jump = current - last
    if jump >= min_jump:
        return jump
    return None


def check_interactions_accel(
    coin: dict, state: BaselineState, *, ratio: float
) -> Optional[float]:
    """Return ``current / oldest_in_ring`` when >= ratio.

    Silently returns None when the 6-slot ring hasn't filled yet (design
    spec §5.1: "do not compare against a missing slot").
    """
    from scout.social.baselines import INTERACTIONS_RING_SIZE

    if len(state.interactions_ring) < INTERACTIONS_RING_SIZE:
        return None
    current = _f(coin.get("interactions_24h"))
    if current is None or current <= 0:
        return None
    oldest = state.interactions_ring[0]
    if oldest <= 0:
        return None
    hit = current / oldest
    if hit >= ratio:
        return hit
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _warmup_required(settings: "Settings", current_poll_interval: int) -> int:
    """Return the sample count needed before alerts fire (interval-aware)."""
    hours = int(getattr(settings, "LUNARCRUSH_BASELINE_MIN_HOURS", 24))
    interval = max(int(current_poll_interval), 1)
    return (hours * 3600) // interval


async def detect_spikes(
    db: "Database",
    settings: "Settings",
    cache: BaselineCache,
    coins: list[dict],
    *,
    current_poll_interval: Optional[int] = None,
) -> tuple[list[ResearchAlert], dict[str, BaselineState]]:
    """Run the three spike checks across ``coins`` and return (alerts, buffered_states).

    The orchestrator never commits baseline updates to the cache here -- it
    returns the buffered post-update states so the surrounding loop can apply
    them transactionally after the DB insert succeeds (buffered-commit
    pattern, design spec §8 step 10).

    Non-firing coins are safe to commit unconditionally: the loop caller
    distinguishes them via the returned mapping's key set vs alert coin_ids.
    """
    if current_poll_interval is None:
        current_poll_interval = int(getattr(settings, "LUNARCRUSH_POLL_INTERVAL", 300))

    ratio = float(settings.LUNARCRUSH_SOCIAL_SPIKE_RATIO)
    min_jump = float(settings.LUNARCRUSH_GALAXY_JUMP)
    accel = float(settings.LUNARCRUSH_INTERACTIONS_ACCEL)
    min_samples = int(settings.LUNARCRUSH_BASELINE_MIN_SAMPLES)
    required = _warmup_required(settings, current_poll_interval)
    dedup_hours = int(settings.LUNARCRUSH_DEDUP_HOURS)
    top_n = int(settings.LUNARCRUSH_TOP_N)

    # Buffered states for FIRING coins only -- the loop applies these
    # transactionally after the DB insert succeeds (design spec §8 step 10).
    # Non-firing coins get their buffered state committed inline to the cache
    # (step 11: baseline progresses independently of alert persistence).
    buffered_states: dict[str, BaselineState] = {}
    detections: list[tuple[ResearchAlert, float]] = []

    for coin in coins:
        coin_id = str(coin.get("id") or "").strip()
        if not coin_id:
            continue
        symbol = (coin.get("symbol") or coin_id).upper()
        name = coin.get("name") or symbol

        pre_state = cache.get(coin_id)
        if pre_state is None:
            pre_state = cache.bootstrap(coin_id, symbol)

        # Run the 3 checks against PRE-update state.
        kinds: list[SpikeKind] = []
        metrics: dict[str, float] = {}

        sv_hit = check_social_volume_24h_spike(coin, pre_state, ratio=ratio)
        if sv_hit is not None and pre_state.sample_count >= required:
            kinds.append(SpikeKind.SOCIAL_VOLUME_24H)
            metrics["social_spike_ratio"] = sv_hit

        gx_jump = check_galaxy_jump(coin, pre_state, min_jump=min_jump)
        if gx_jump is not None and pre_state.sample_count >= required:
            kinds.append(SpikeKind.GALAXY_JUMP)
            metrics["galaxy_jump"] = gx_jump

        ia_hit = check_interactions_accel(coin, pre_state, ratio=accel)
        if ia_hit is not None and pre_state.sample_count >= required:
            kinds.append(SpikeKind.INTERACTIONS_ACCEL)
            metrics["interactions_ratio"] = ia_hit

        # Compute the buffered post-update state regardless (progress
        # invariant — every coin's sample_count advances).
        new_sv = _f(coin.get("social_volume_24h"))
        new_gx = _f(coin.get("galaxy_score"))
        new_ia = _f(coin.get("interactions_24h"))

        post = update_state(
            pre_state, new_sv, min_samples=min_samples, spike_ratio=ratio
        )
        # Always record last_galaxy_score and push to the interactions ring.
        ring = post.interactions_ring
        if new_ia is not None and new_ia > 0:
            ring = push_interactions(ring, new_ia)
        post = post._replace(
            last_galaxy_score=new_gx if new_gx is not None else post.last_galaxy_score,
            interactions_ring=ring,
            last_poll_at=_utcnow(),
        )

        if not kinds:
            # Non-firing coin -- commit baseline update inline (§8 step 11).
            cache.set(coin_id, post)
            cache.mark_dirty(coin_id)
            continue

        # Firing coin -- buffer the post_state for the loop to commit.
        buffered_states[coin_id] = post

        alert = ResearchAlert(
            coin_id=coin_id,
            symbol=symbol,
            name=name,
            spike_kinds=kinds,
            social_spike_ratio=metrics.get("social_spike_ratio"),
            galaxy_score=new_gx,
            galaxy_jump=metrics.get("galaxy_jump"),
            social_volume_24h=new_sv,
            social_volume_baseline=pre_state.avg_social_volume_24h,
            interactions_24h=new_ia,
            interactions_ratio=metrics.get("interactions_ratio"),
            sentiment=_f(coin.get("sentiment")),
            social_dominance=_f(coin.get("social_dominance")),
            price_change_1h=_f(coin.get("price_change_1h")),
            price_change_24h=_f(coin.get("percent_change_24h"))
            or _f(coin.get("price_change_24h")),
            market_cap=_f(coin.get("market_cap")),
            current_price=_f(coin.get("price")) or _f(coin.get("current_price")),
            detected_at=_utcnow(),
        )
        # Rank by the largest hit ratio (social_spike_ratio as primary;
        # galaxy_jump divided by min_jump as a comparable "ratio"; same for
        # interactions). Fall back to 1.0 if none set (shouldn't happen).
        rank_key = 0.0
        if alert.social_spike_ratio is not None:
            rank_key = max(rank_key, alert.social_spike_ratio)
        if alert.galaxy_jump is not None and min_jump > 0:
            rank_key = max(rank_key, 1.0 + (alert.galaxy_jump / min_jump))
        if alert.interactions_ratio is not None:
            rank_key = max(rank_key, alert.interactions_ratio)
        detections.append((alert, rank_key))

    if not detections:
        return [], buffered_states

    # Dedup against recently-ALERTED social_signals rows only. Rows with
    # ``alerted_at IS NULL`` represent a stored detection whose Telegram
    # dispatch failed -- we WANT those to re-enter detection so the user
    # eventually gets the alert (spec §8).
    # SQLite's default SQLITE_LIMIT_VARIABLE_NUMBER is 999; chunk the coin
    # list into batches of 500 to stay well clear.
    if db._conn is not None and dedup_hours > 0:
        coin_ids = list({a.coin_id for a, _ in detections})
        recent: set[str] = set()
        CHUNK = 500
        for i in range(0, len(coin_ids), CHUNK):
            chunk = coin_ids[i : i + CHUNK]
            placeholders = ",".join("?" * len(chunk))
            cursor = await db._conn.execute(
                f"""SELECT DISTINCT coin_id FROM social_signals
                    WHERE coin_id IN ({placeholders})
                      AND alerted_at IS NOT NULL
                      AND datetime(detected_at) >= datetime('now', '-' || ? || ' hours')""",
                (*chunk, dedup_hours),
            )
            rows = await cursor.fetchall()
            recent.update(r[0] for r in rows)
        detections = [(a, r) for a, r in detections if a.coin_id not in recent]

    # Sort descending by rank_key + take top-N.
    detections.sort(key=lambda x: x[1], reverse=True)
    detections = detections[:top_n]

    return [a for a, _ in detections], buffered_states
