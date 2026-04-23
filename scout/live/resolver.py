"""VenueResolver + OverrideStore (spec §7). Two classes, one file per §2.1."""
from __future__ import annotations

import asyncio
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
        # Per-symbol single-flight locks. Reviewer 2: using defaultdict here
        # leaked memory because entries were never evicted. Instead, each
        # resolve() creates-or-reuses the lock via dict.setdefault (atomic
        # under the CPython GIL), acquires it, and evicts the entry after
        # release — subsequent resolvers for the same symbol will hit the
        # positive/negative cache so no lock is needed. Concurrent tasks on
        # the same symbol all see the same Lock object because setdefault
        # returns the existing value if one is present.
        self._locks: dict[str, asyncio.Lock] = {}

    async def resolve(self, symbol: str) -> ResolvedVenue | None:
        sym = symbol.upper()
        # 1. Cache
        cached, ttl_remaining_sec = await self._cache_get_with_ttl(sym)
        if cached is not False:
            # Positive hit OR cached-negative. Either way the cache served it.
            outcome = "positive" if cached is not None else "negative"
            if _METRIC_INC is not None:
                await _METRIC_INC(self._db, "resolver_cache_hits")
            log.debug(
                "live_resolver_cache_hit",
                symbol=sym,
                outcome=outcome,
                ttl_remaining_sec=ttl_remaining_sec,
            )
            return cached if cached is not None else None

        # Cache miss — count it once, before single-flight.
        if _METRIC_INC is not None:
            await _METRIC_INC(self._db, "resolver_cache_misses")
        log.debug("live_resolver_cache_miss", symbol=sym, outcome="miss")

        # 2. Single-flight per-symbol. setdefault is atomic under the CPython
        # GIL so concurrent resolve() calls share one Lock per symbol. After
        # the lock releases, the result is cached, so subsequent resolvers
        # hit the positive/negative cache without needing the per-symbol
        # lock again — safe to evict.
        lock = self._locks.setdefault(sym, asyncio.Lock())
        try:
            async with lock:
                cached, ttl_remaining_sec = await self._cache_get_with_ttl(sym)
                if cached is not False:
                    # Another waiter populated cache — treat this as a hit too.
                    outcome = "positive" if cached is not None else "negative"
                    if _METRIC_INC is not None:
                        await _METRIC_INC(self._db, "resolver_cache_hits")
                    log.debug(
                        "live_resolver_cache_hit",
                        symbol=sym,
                        outcome=outcome,
                        ttl_remaining_sec=ttl_remaining_sec,
                    )
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
        finally:
            # Evict per-symbol lock once no one else is holding/waiting on it
            # so self._locks does not grow unboundedly. If another task is
            # still inside `async with lock:` (lock.locked() == True) or is
            # queued waiting, leave the lock in place — that task's finally
            # will handle eviction.
            if not lock.locked():
                self._locks.pop(sym, None)

    # --- cache helpers -----------------------------------------------------

    async def _cache_get(self, sym: str) -> ResolvedVenue | None | bool:
        """Return ResolvedVenue for positive hit, None for negative hit,
        False for cache miss. (Three-valued to distinguish 'not-cached' from
        'cached-as-negative'.)"""
        result, _ttl = await self._cache_get_with_ttl(sym)
        return result

    async def _cache_get_with_ttl(
        self, sym: str
    ) -> tuple[ResolvedVenue | None | bool, int | None]:
        """Variant of :meth:`_cache_get` that also returns the remaining TTL
        (in seconds, rounded down) so the resolve() log event can include it.
        Returns ``(False, None)`` on miss, ``(None, ttl)`` on cached-negative,
        ``(ResolvedVenue, ttl)`` on positive hit."""
        assert self._db._conn is not None
        now = datetime.now(timezone.utc)
        cur = await self._db._conn.execute(
            "SELECT outcome, venue, pair, expires_at FROM resolver_cache "
            "WHERE symbol = ?",
            (sym,),
        )
        row = await cur.fetchone()
        if row is None:
            return False, None
        expires_at = datetime.fromisoformat(row[3].replace("Z", "+00:00"))
        if expires_at <= now:
            return False, None
        ttl_remaining_sec = int((expires_at - now).total_seconds())
        if row[0] == "positive":
            return (
                ResolvedVenue(
                    symbol=sym, venue=row[1], pair=row[2], source="cache",
                ),
                ttl_remaining_sec,
            )
        return None, ttl_remaining_sec  # cached-negative

    async def _cache_put_positive(self, sym: str, rv: ResolvedVenue) -> None:
        assert self._db._conn is not None
        assert self._db._txn_lock is not None
        now = datetime.now(timezone.utc)
        expires_at = now + self._positive_ttl
        # Reviewer 2: commits on the shared connection must hold _txn_lock.
        # Callers of _cache_put_* (resolve() at L115/L121/L127) are already
        # inside the per-symbol single-flight lock but NOT inside _txn_lock,
        # so nesting here is safe.
        async with self._db._txn_lock:
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
        assert self._db._txn_lock is not None
        now = datetime.now(timezone.utc)
        expires_at = now + self._negative_ttl
        async with self._db._txn_lock:
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
