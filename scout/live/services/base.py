"""VenueService ABC + concurrency contract.

Per design v2.1: each per-venue service implements `run_once(adapter,
db, venue)` and exposes `cadence_seconds`. The service-runner harness
schedules `run_once` at the cadence; serializes runs per (adapter,
service-class) pair via asyncio.Lock so a slow run doesn't overlap
with the next tick.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from scout.db import Database


class VenueService(ABC):
    """Abstract per-venue service. Concrete subclasses implement
    run_once + cadence_seconds; the runner harness handles scheduling.
    """

    cadence_seconds: float = 60.0
    """Seconds between successive run_once calls per (adapter, service)
    pair. Default 60; subclasses override."""

    name: str = "venue_service"
    """Stable lowercase identifier used in log keys + lock keys."""

    @abstractmethod
    async def run_once(
        self, *, adapter: Any, db: Database, venue: str
    ) -> None:
        """Single tick. Errors must be caught + logged; do not raise."""
        ...
