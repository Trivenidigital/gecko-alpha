"""Strategy manager for the narrative rotation agent.

Manages tunable strategy parameters stored in the agent_strategy table,
with bounds validation, locking, and JSON-typed values.
"""

import json
from datetime import datetime, timezone

from scout.db import Database

STRATEGY_DEFAULTS: dict[str, object] = {
    "category_accel_threshold": 5.0,
    "category_volume_growth_min": 10.0,
    "laggard_max_mcap": 200_000_000,
    "laggard_max_change": 10.0,
    "laggard_min_change": -20.0,
    "laggard_min_volume": 100_000,
    "max_picks_per_category": 5,
    "hit_threshold_pct": 15.0,
    "miss_threshold_pct": -10.0,
    "signal_cooldown_hours": 4,
    "max_heating_per_cycle": 5,
    "min_learn_sample": 100,
    "min_trigger_count": 1,
    "lessons_learned": "",
    "lessons_version": 0,
    "narrative_alert_enabled": True,
    "counter_suppress_threshold": 100,
    # Personalized narrative matching preferences
    "user_preferred_categories": [],
    "user_excluded_categories": [],
    "user_min_market_cap": 0,
    "user_max_market_cap": 0,
    "user_alert_mode": "all",
}

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
    "counter_suppress_threshold": (0, 100),
}


class Strategy:
    """Manages agent strategy parameters with bounds, locks, and persistence."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._cache: dict[str, object] = {}

    async def load_or_init(self) -> None:
        """Load existing values from agent_strategy table, seed missing defaults."""
        conn = self._db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        # Load existing rows into cache
        cursor = await conn.execute("SELECT key, value FROM agent_strategy")
        rows = await cursor.fetchall()
        for row in rows:
            self._cache[row[0]] = json.loads(row[1])

        # Seed any missing defaults
        now = datetime.now(timezone.utc).isoformat()
        for key, default in STRATEGY_DEFAULTS.items():
            if key not in self._cache:
                bounds = STRATEGY_BOUNDS.get(key)
                min_b = bounds[0] if bounds else None
                max_b = bounds[1] if bounds else None
                await conn.execute(
                    """INSERT INTO agent_strategy
                       (key, value, updated_at, updated_by, reason, min_bound, max_bound)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (key, json.dumps(default), now, "init", "seeded default", min_b, max_b),
                )
                self._cache[key] = default
        await conn.commit()

    def get(self, key: str) -> object:
        """Return the typed Python value for a strategy key."""
        if key not in self._cache:
            raise KeyError(f"Unknown strategy key: {key}")
        return self._cache[key]

    async def set(
        self, key: str, value: object, updated_by: str, reason: str | None = None
    ) -> None:
        """Set a strategy value, validating bounds and lock status."""
        conn = self._db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        # Check lock
        cursor = await conn.execute(
            "SELECT locked FROM agent_strategy WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        if row and row[0]:
            raise ValueError(f"Key '{key}' is locked")

        # Validate bounds
        if key in STRATEGY_BOUNDS:
            lo, hi = STRATEGY_BOUNDS[key]
            numeric = float(value)  # type: ignore[arg-type]
            if numeric < lo or numeric > hi:
                raise ValueError(
                    f"Value {value} for '{key}' is out of bounds [{lo}, {hi}]"
                )

        now = datetime.now(timezone.utc).isoformat()
        bounds = STRATEGY_BOUNDS.get(key)
        min_b = bounds[0] if bounds else None
        max_b = bounds[1] if bounds else None

        await conn.execute(
            """INSERT OR REPLACE INTO agent_strategy
               (key, value, updated_at, updated_by, reason, min_bound, max_bound)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (key, json.dumps(value), now, updated_by, reason, min_b, max_b),
        )
        await conn.commit()
        self._cache[key] = value

    async def lock(self, key: str) -> None:
        """Lock a key so it cannot be changed."""
        conn = self._db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")
        await conn.execute(
            "UPDATE agent_strategy SET locked = 1 WHERE key = ?", (key,)
        )
        await conn.commit()

    async def unlock(self, key: str) -> None:
        """Unlock a key so it can be changed again."""
        conn = self._db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")
        await conn.execute(
            "UPDATE agent_strategy SET locked = 0 WHERE key = ?", (key,)
        )
        await conn.commit()

    def get_timestamp(self, key: str, default: datetime | None = None) -> datetime:
        """Get a timestamp value, returning default (or datetime.min) if not set."""
        if default is None:
            default = datetime.min
        try:
            raw = self.get(key)
        except KeyError:
            return default
        if not raw:
            return default
        return datetime.fromisoformat(str(raw))

    async def set_timestamp(self, key: str, value: datetime) -> None:
        """Set a timestamp value."""
        await self.set(key, value.isoformat(), updated_by="system", reason="timestamp update")

    def get_all(self) -> dict[str, object]:
        """Return a dict of all strategy key-value pairs."""
        return dict(self._cache)
