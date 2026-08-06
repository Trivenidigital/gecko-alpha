"""THE authoritative bounds registry for tunable strategy parameters.

One definition, every consumer. Previously this table existed twice — here (as
part of ``scout/narrative/strategy.py``) and again as a literal inside
``dashboard/api.py`` — and the two drifted: the dashboard copy was missing
``counter_suppress_threshold`` entirely, so the operator-facing PUT endpoint
applied **no bounds validation** to that parameter while ``Strategy.set`` and the
deterministic learner both enforced ``(0, 100)``. A value written through the
dashboard could therefore sit outside the range the learner assumes when it
builds a candidate grid around it.

Consumers, all importing from here:

* ``scout.narrative.strategy`` — ``Strategy.set`` bounds enforcement, and the
  re-export that keeps ``from scout.narrative.strategy import STRATEGY_BOUNDS``
  working for existing callers.
* ``scout.narrative.deterministic_learner`` — candidate-grid clamping.
* ``dashboard.api`` — the operator PUT endpoint.
* ``tests/test_strategy_bounds_registry.py`` — asserts no second copy exists.

**This module must stay a leaf.** It deliberately imports nothing, so the
dashboard can consume it without pulling in ``scout.db`` and the whole scout
import chain. Adding an import here would recreate the pressure that produced the
duplicate copy in the first place.

To add a parameter: add it here, once. Do not paste a literal into a consumer.
"""

from __future__ import annotations

# key -> (inclusive_min, inclusive_max)
STRATEGY_BOUNDS: dict[str, tuple[float, float]] = {
    "category_accel_threshold": (2.0, 15.0),
    "category_volume_growth_min": (5.0, 50.0),
    "laggard_max_mcap": (50_000_000, 1_000_000_000),
    "laggard_max_change": (5.0, 30.0),
    "laggard_min_change": (-50.0, 0.0),
    "laggard_min_volume": (10_000, 1_000_000),
    "hit_threshold_pct": (5.0, 50.0),
    "miss_threshold_pct": (-30.0, -5.0),
    "max_picks_per_category": (3, 10),
    "max_heating_per_cycle": (1, 10),
    "signal_cooldown_hours": (1, 12),
    "min_learn_sample": (50, 500),
    "min_trigger_count": (1, 10),
    # The key the dashboard copy omitted. Present in `Strategy.set` and the
    # learner from the start; absent from the endpoint, which is why manual
    # updates to it were unvalidated.
    "counter_suppress_threshold": (0, 100),
}

__all__ = ["STRATEGY_BOUNDS"]
