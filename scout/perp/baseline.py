"""Per-(exchange,symbol) EWMA baseline store with LRU + idle eviction."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime


@dataclass
class _Entry:
    oi_ewma: float | None
    funding_ewma: float | None
    sample_count: int
    last_seen: datetime


class BaselineStore:
    """In-memory EWMA baseline state keyed by (exchange, symbol).

    Bounded: ``max_keys`` LRU cap on insert, plus an opt-in ``evict_idle``
    pass that drops keys untouched for ``idle_evict_seconds``. Both
    paths are intentionally lightweight -- no background threads.

    Not thread-safe. Designed for single-asyncio-task use only.
    """

    def __init__(
        self,
        *,
        alpha: float = 0.1,
        max_keys: int = 1000,
        idle_evict_seconds: int = 3600,
    ):
        if not 0 < alpha <= 1:
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        self._alpha = alpha
        self._max_keys = max_keys
        if idle_evict_seconds < 0:
            raise ValueError(f"idle_evict_seconds must be >= 0, got {idle_evict_seconds}")
        self._idle = idle_evict_seconds
        self._entries: "OrderedDict[tuple[str, str], _Entry]" = OrderedDict()

    def update(
        self,
        key: tuple[str, str],
        *,
        oi: float | None,
        funding: float | None,
        now: datetime,
    ) -> None:
        if oi is None and funding is None:
            return
        entry = self._entries.get(key)
        if entry is None:
            if len(self._entries) >= self._max_keys:
                self._entries.popitem(last=False)
            entry = _Entry(oi, funding, 1, now)
            self._entries[key] = entry
            return
        if oi is not None:
            entry.oi_ewma = (
                oi if entry.oi_ewma is None
                else self._alpha * oi + (1 - self._alpha) * entry.oi_ewma
            )
        if funding is not None:
            entry.funding_ewma = (
                funding if entry.funding_ewma is None
                else self._alpha * funding + (1 - self._alpha) * entry.funding_ewma
            )
        entry.sample_count += 1
        entry.last_seen = now
        self._entries.move_to_end(key)

    def oi_baseline(self, key: tuple[str, str]) -> float | None:
        entry = self._entries.get(key)
        return None if entry is None else entry.oi_ewma

    def funding_baseline(self, key: tuple[str, str]) -> float | None:
        entry = self._entries.get(key)
        return None if entry is None else entry.funding_ewma

    def sample_count(self, key: tuple[str, str]) -> int:
        entry = self._entries.get(key)
        return 0 if entry is None else entry.sample_count

    def evict_idle(self, *, now: datetime) -> int:
        if self._idle == 0:
            return 0  # idle_evict_seconds=0 disables this pass entirely
        cutoff = now.timestamp() - self._idle
        victims = [
            k for k, e in self._entries.items()
            if e.last_seen.timestamp() < cutoff
        ]
        for k in victims:
            self._entries.pop(k)
        return len(victims)

    def __len__(self) -> int:
        return len(self._entries)
