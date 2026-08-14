"""Production wiring for the TG actionability shadow — registration, activation,
and the catch-up scan loop.

This is deliberately NOT part of `shadow.py` or `caller_features.py`. Those two
module sources are hashed into `gate_version`, so an edit to either splits the
evidence cohort. Scheduling is operational: the scan interval, the log lines,
and the crash containment all have to be tunable without retiring a generation
and resetting the accumulation toward the feature freeze. Keeping the scheduler
here means the decision-producing surface stays hash-stable.

**Deploy-dark.** With `TG_SHADOW_ENABLED` absent or false this loop registers
no provider, creates no generation row, writes no health row, and evaluates
nothing — it returns before touching the database at all. That is the state
every deployed host is in today: Stage A + Stage B merge dark, and activation
is a later, separate operator action.
"""

from __future__ import annotations

import asyncio

import structlog

from scout.config import Settings
from scout.db import Database
from scout.social.telegram.caller_features import TgCallerFeatureProvider
from scout.social.telegram.shadow import (
    activate_shadow_generation,
    register_caller_feature_provider,
    scan_and_evaluate,
)

log = structlog.get_logger()

# Floor on the scan interval, so a misconfigured cadence cannot spin.
_MIN_SCAN_INTERVAL_SEC = 60


def scan_interval_sec(settings: Settings) -> int:
    """Half the watchdog's scan-cadence budget, always STRICTLY inside it.

    Derived from `TG_SHADOW_SCAN_CADENCE_MIN` rather than given its own setting
    so the two cannot drift apart: the watchdog pages when no scan has
    completed within the cadence, and a scanner running exactly AT the cadence
    would race that alarm on every cycle.

    The floor is clamped back under the budget rather than allowed to exceed
    it. At a 1-minute cadence a bare `max(60, ...)` returns exactly 60 — equal
    to the budget, not inside it — which breaks the very invariant this
    function exists to hold.
    """
    budget = settings.TG_SHADOW_SCAN_CADENCE_MIN * 60
    return max(1, min(budget - 1, max(_MIN_SCAN_INTERVAL_SEC, budget // 2)))


async def tg_shadow_loop(*, db: Database, settings: Settings) -> None:
    """Register the real provider, arm the generation, then scan forever.

    Returns immediately — before any read or write — when the flag is off.
    Activation refusal (flag on, provider somehow unregistered) is handled and
    logged inside `activate_shadow_generation`; this returns without starting a
    scan loop that could have nothing to attribute decisions to.
    """
    if not settings.TG_SHADOW_ENABLED:
        log.info("tg_shadow_runner_disabled")
        return

    register_caller_feature_provider(TgCallerFeatureProvider(db._db_path))
    gate_version = await activate_shadow_generation(db, settings)
    if gate_version is None:
        log.warning("tg_shadow_runner_not_armed")
        return

    interval = scan_interval_sec(settings)
    log.info(
        "tg_shadow_runner_started",
        gate_version=gate_version,
        scan_interval_sec=interval,
    )
    while True:
        try:
            await scan_and_evaluate(db, settings)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            # A scan failure is an evidence gap, not a pipeline fault — but it
            # must not be a SILENT gap. The §12a watchdog pages when no scan
            # completes within the cadence, which is exactly what a loop
            # failing here produces: no `tg_shadow_scan_complete`, no
            # heartbeat, and therefore a page.
            log.exception(
                "tg_shadow_scan_failed",
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
        await asyncio.sleep(interval)
