"""VenueResolver + OverrideStore (spec §7). Two classes, one file per §2.1."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable

import structlog

from scout.db import Database
from scout.live.adapter_base import ExchangeAdapter
from scout.live.types import ResolvedVenue

log = structlog.get_logger(__name__)

# Optional dependency on scout.live.metrics.inc — shipped in Task 11. Until
# then, this stays None and the resolver no-ops metric increments. Once the
# module exists, operators get hit/miss counters without touching this file.
_METRIC_INC: Callable[[Database, str], Awaitable[None]] | None
try:  # pragma: no cover — import guard
    from scout.live.metrics import inc as _METRIC_INC  # type: ignore[assignment]
except ImportError:  # pragma: no cover
    _METRIC_INC = None


class OverrideStore:
    """Read-only view of venue_overrides. Write path is direct SQL via ops CLI."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def lookup(self, symbol: str) -> tuple[str | None, bool] | None:
        """Return (pair, disabled_bool) for symbol, or None if no row."""
        assert self._db._conn is not None
        cur = await self._db._conn.execute(
            "SELECT pair, disabled FROM venue_overrides WHERE symbol = ?",
            (symbol.upper(),),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return (row[0], bool(row[1]))


class VenueResolver:
    def __init__(
        self,
        *,
        binance_adapter: ExchangeAdapter,
        override_store: OverrideStore,
        positive_ttl: timedelta,
        negative_ttl: timedelta,
        db: Database,
    ) -> None:
        self._adapter = binance_adapter
        self._overrides = override_store
        self._positive_ttl = positive_ttl
        self._negative_ttl = negative_ttl
        self._db = db
        # defaultdict(asyncio.Lock): safe in CPython because dict.setdefault
        # (used internally by defaultdict.__getitem__) is atomic under the GIL.
        # Trio / other runtimes would need an explicit guard.
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def resolve(self, symbol: str) -> ResolvedVenue | None:
        sym = symbol.upper()
        # 1. Cache
        cached = await self._cache_get(sym)
        if cached is not False:
            # Positive hit OR cached-negative. Either way the cache served it.
            if _METRIC_INC is not None:
                await _METRIC_INC(self._db, "resolver_cache_hits")
            return cached if cached is not None else None

        # Cache miss — count it once, before single-flight.
        if _METRIC_INC is not None:
            await _METRIC_INC(self._db, "resolver_cache_misses")

        # 2. Single-flight per-symbol
        async with self._locks[sym]:
            cached = await self._cache_get(sym)
            if cached is not False:
                # Another waiter populated cache — treat this as a hit too.
                if _METRIC_INC is not None:
                    await _METRIC_INC(self._db, "resolver_cache_hits")
                return cached if cached is not None else None

            # 3. Override
            ov = await self._overrides.lookup(sym)
            if ov is not None:
                pair, disabled = ov
                if disabled:
                    # Spec §5 gate 4: disabled override SHORT-CIRCUITS.
                    # Do NOT fall through to exchangeInfo.
                    return None
                resolved = ResolvedVenue(
                    symbol=sym, venue="binance", pair=pair, source="override_table"
                )
                await self._cache_put_positive(sym, resolved)
                return resolved

            # 4. Binance exchangeInfo
            pair = await self._adapter.resolve_pair_for_symbol(sym)
            if pair is None:
                await self._cache_put_negative(sym)
                return None
            resolved = ResolvedVenue(
                symbol=sym, venue="binance", pair=pair,
                source="binance_exchangeinfo",
            )
            await self._cache_put_positive(sym, resolved)
            return resolved

    # --- cache helpers -----------------------------------------------------

    async def _cache_get(self, sym: str) -> ResolvedVenue | None | bool:
        """Return ResolvedVenue for positive hit, None for negative hit,
        False for cache miss. (Three-valued to distinguish 'not-cached' from
        'cached-as-negative'.)"""
        assert self._db._conn is not None
        now = datetime.now(timezone.utc)
        cur = await self._db._conn.execute(
            "SELECT outcome, venue, pair, expires_at FROM resolver_cache "
            "WHERE symbol = ?",
            (sym,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        expires_at = datetime.fromisoformat(row[3].replace("Z", "+00:00"))
        if expires_at <= now:
            return False
        if row[0] == "positive":
            return ResolvedVenue(
                symbol=sym, venue=row[1], pair=row[2], source="cache",
            )
        return None  # cached-negative

    async def _cache_put_positive(self, sym: str, rv: ResolvedVenue) -> None:
        assert self._db._conn is not None
        now = datetime.now(timezone.utc)
        expires_at = now + self._positive_ttl
        await self._db._conn.execute(
            "INSERT INTO resolver_cache "
            "(symbol, outcome, venue, pair, resolved_at, expires_at) "
            "VALUES (?, 'positive', ?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "  outcome=excluded.outcome, venue=excluded.venue, pair=excluded.pair, "
            "  resolved_at=excluded.resolved_at, expires_at=excluded.expires_at",
            (sym, rv.venue, rv.pair, now.isoformat(), expires_at.isoformat()),
        )
        await self._db._conn.commit()

    async def _cache_put_negative(self, sym: str) -> None:
        assert self._db._conn is not None
        now = datetime.now(timezone.utc)
        expires_at = now + self._negative_ttl
        await self._db._conn.execute(
            "INSERT INTO resolver_cache "
            "(symbol, outcome, venue, pair, resolved_at, expires_at) "
            "VALUES (?, 'negative', NULL, NULL, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "  outcome=excluded.outcome, venue=NULL, pair=NULL, "
            "  resolved_at=excluded.resolved_at, expires_at=excluded.expires_at",
            (sym, now.isoformat(), expires_at.isoformat()),
        )
        await self._db._conn.commit()
