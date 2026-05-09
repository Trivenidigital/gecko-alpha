"""Service-runner harness — parallel across venues, serialized per pair.

Usage:
    runner = ServiceRunner(db=db, adapters={'binance': adapter}, services=[
        HealthProbe(), BalanceSnapshot(), RateLimitAccountantStub(),
    ])
    await runner.start()
    # ... let it run; cancel via runner.stop() ...

Per design v2.1: at most one run_once per (adapter, service) pair at a
time. asyncio.Lock per pair enforces this. Different services on the
same adapter run concurrently; same service on different adapters runs
concurrently; same service on same adapter runs serially with a sleep
of `cadence_seconds` between completions.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from scout.db import Database
from scout.live.services.base import VenueService

log = structlog.get_logger(__name__)


class ServiceRunner:
    def __init__(
        self,
        *,
        db: Database,
        adapters: dict[str, Any],
        services: list[VenueService],
    ) -> None:
        self._db = db
        self._adapters = adapters
        self._services = services
        self._tasks: list[asyncio.Task] = []
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _lock_for(self, venue: str, service: VenueService) -> asyncio.Lock:
        key = (venue, service.name)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def _run_loop(self, venue: str, adapter: Any, service: VenueService) -> None:
        lock = self._lock_for(venue, service)
        while True:
            async with lock:
                try:
                    await service.run_once(adapter=adapter, db=self._db, venue=venue)
                except Exception:
                    log.exception(
                        "venue_service_run_once_failed",
                        venue=venue,
                        service=service.name,
                    )
            await asyncio.sleep(service.cadence_seconds)

    async def start(self) -> None:
        for venue, adapter in self._adapters.items():
            for service in self._services:
                task = asyncio.create_task(
                    self._run_loop(venue, adapter, service),
                    name=f"venue_service:{venue}:{service.name}",
                )
                self._tasks.append(task)
        log.info(
            "service_runner_started",
            n_tasks=len(self._tasks),
            n_venues=len(self._adapters),
            n_services=len(self._services),
        )

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        log.info("service_runner_stopped")
