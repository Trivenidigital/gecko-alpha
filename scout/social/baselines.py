"""In-process EWMA baseline cache + DB checkpoint.

See design spec §5.4 (symmetric spike-exclusion rule) and §6 (persistence,
graceful shutdown flow). The cache is vendor-agnostic -- the LunarCrush
loop owns the schedule for hydrate/checkpoint calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import structlog

from scout.social.models import BaselineState

if TYPE_CHECKING:
    from scout.db import Database

logger = structlog.get_logger(__name__)

INTERACTIONS_RING_SIZE = 6


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def push_interactions(ring: list[float], value: float) -> list[float]:
    """Append ``value`` to the ring, truncating the oldest entry on overflow.

    Returns a NEW list; the caller replaces the stored ring via
    ``state._replace(interactions_ring=...)``.
    """
    next_ring = list(ring)
    next_ring.append(value)
    if len(next_ring) > INTERACTIONS_RING_SIZE:
        next_ring = next_ring[-INTERACTIONS_RING_SIZE:]
    return next_ring


def update_state(
    state: BaselineState,
    new_value: Optional[float],
    *,
    min_samples: int,
    spike_ratio: float,
) -> BaselineState:
    """Return a new ``BaselineState`` with EWMA + symmetric spike-exclusion.

    Rules (design spec §5.4):

    * ``None`` / zero / negative value -> return state unchanged (progress
      invariant NOT incremented; this preserves cold-start warmup).
    * After warmup (``sample_count >= min_samples``) skip the EWMA when
      the ratio is outside the ``[1/spike_ratio, spike_ratio]`` window,
      but still increment ``sample_count`` so warmup progresses uniformly.
    * Otherwise apply EWMA with ``alpha = 1 / min_samples``.
    """
    if new_value is None or new_value <= 0:
        return state

    # After warmup: exclude extreme samples in EITHER direction.
    if state.sample_count >= min_samples:
        ratio = new_value / max(state.avg_social_volume_24h, 1e-9)
        spike_hi = spike_ratio
        spike_lo = 1.0 / spike_hi if spike_hi > 0 else 0.0
        if ratio >= spike_hi or ratio <= spike_lo:
            return state._replace(
                sample_count=state.sample_count + 1,
                last_updated=_utcnow(),
            )

    alpha = 1.0 / max(min_samples, 1)
    new_avg = alpha * new_value + (1.0 - alpha) * state.avg_social_volume_24h
    return state._replace(
        avg_social_volume_24h=new_avg,
        sample_count=state.sample_count + 1,
        last_updated=_utcnow(),
    )


class BaselineCache:
    """Thin wrapper around ``dict[str, BaselineState]`` with dirty tracking."""

    def __init__(self) -> None:
        self._states: dict[str, BaselineState] = {}
        self._dirty: set[str] = set()

    def __contains__(self, coin_id: str) -> bool:
        return coin_id in self._states

    def __len__(self) -> int:
        return len(self._states)

    def get(self, coin_id: str) -> Optional[BaselineState]:
        return self._states.get(coin_id)

    def set(self, coin_id: str, state: BaselineState) -> None:
        self._states[coin_id] = state

    def mark_dirty(self, coin_id: str) -> None:
        self._dirty.add(coin_id)

    def pop_dirty(self) -> set[str]:
        out = set(self._dirty)
        self._dirty.clear()
        return out

    def items(self):
        return self._states.items()

    def bootstrap(self, coin_id: str, symbol: str) -> BaselineState:
        """Create or return an existing state for ``coin_id``."""
        existing = self._states.get(coin_id)
        if existing is not None:
            return existing
        state = BaselineState(
            coin_id=coin_id,
            symbol=symbol,
            avg_social_volume_24h=0.0,
            avg_galaxy_score=0.0,
            last_galaxy_score=None,
            interactions_ring=[],
            sample_count=0,
            last_poll_at=None,
            last_updated=_utcnow(),
        )
        self._states[coin_id] = state
        return state


async def hydrate_baselines(db: "Database", cache: BaselineCache) -> int:
    """Load ``social_baselines`` rows into ``cache``. Returns row count."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cursor = await db._conn.execute(
        """SELECT coin_id, symbol, avg_social_volume_24h, avg_galaxy_score,
                  last_galaxy_score, interactions_ring, sample_count,
                  last_poll_at, last_updated
           FROM social_baselines"""
    )
    rows = await cursor.fetchall()
    loaded = 0
    for r in rows:
        try:
            ring = json.loads(r[5] or "[]")
            if not isinstance(ring, list):
                ring = []
        except (json.JSONDecodeError, TypeError):
            ring = []
        last_poll = None
        if r[7]:
            try:
                last_poll = datetime.fromisoformat(r[7])
                if last_poll.tzinfo is None:
                    last_poll = last_poll.replace(tzinfo=timezone.utc)
            except ValueError:
                last_poll = None
        try:
            last_updated = datetime.fromisoformat(r[8])
            if last_updated.tzinfo is None:
                last_updated = last_updated.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            last_updated = _utcnow()
        state = BaselineState(
            coin_id=r[0],
            symbol=r[1],
            avg_social_volume_24h=float(r[2]),
            avg_galaxy_score=float(r[3]),
            last_galaxy_score=float(r[4]) if r[4] is not None else None,
            interactions_ring=[float(x) for x in ring],
            sample_count=int(r[6]),
            last_poll_at=last_poll,
            last_updated=last_updated,
        )
        cache.set(r[0], state)
        loaded += 1
    logger.info("social_baselines_hydrated", count=loaded)
    return loaded


async def flush_baselines(db: "Database", cache: BaselineCache) -> int:
    """Flush dirty baseline rows in a single transaction.

    On first write for a coin the row is INSERTed; subsequent writes hit the
    UPSERT branch via ``INSERT OR REPLACE``. Returns the number of rows
    written.
    """
    if db._conn is None:
        return 0
    dirty = cache.pop_dirty()
    if not dirty:
        return 0
    now = _utcnow().isoformat()
    written = 0
    for coin_id in dirty:
        state = cache.get(coin_id)
        if state is None:
            continue
        await db._conn.execute(
            """INSERT OR REPLACE INTO social_baselines
               (coin_id, symbol, avg_social_volume_24h, avg_galaxy_score,
                last_galaxy_score, interactions_ring, sample_count,
                last_poll_at, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                state.coin_id,
                state.symbol,
                state.avg_social_volume_24h,
                state.avg_galaxy_score,
                state.last_galaxy_score,
                json.dumps(list(state.interactions_ring)),
                state.sample_count,
                state.last_poll_at.isoformat() if state.last_poll_at else None,
                now,
            ),
        )
        written += 1
    await db._conn.commit()
    logger.info("social_baselines_flushed", count=written)
    return written
