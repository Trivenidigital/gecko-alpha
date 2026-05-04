"""BL-067: Conviction-locked hold support.

Shared module for stack counting and locked-param composition. Used by:
- scout/trading/evaluator.py (production exit-logic overlay)
- scripts/backtest_conviction_lock.py (research backtest, via thin
  asyncio.run() sync wrapper)

Per backlog.md:374-380 spec table:
- stack=1: defaults (no lock)
- stack=2: +72h max, +5pp trail (cap 35), +5pp sl (cap 35)
- stack=3: +168h max, +10pp trail (cap 35), +10pp sl (cap 40)
- stack>=4: +336h max, +15pp trail (cap 35), +15pp sl (cap 40)

Validated by tasks/findings_bl067_backtest_conviction_lock.md (lift
+114% at N=3 threshold, both compound gates PASS).

Note (design-v2 arch-N1): paper_trades contributes to the stack count
via DISTINCT signal_type — multiple distinct signal_types on the same
token within the window each contribute ONE entry. This is intentional
("each distinct paper-trade signal_type IS an independent confirmation
event") and slightly violates the "each source ≤ 1" docstring shape;
acceptable design choice.

Note (design-v2 arch-D3): refactor `_SIGNAL_SOURCES` list to a
`MetadataSource` plugin pattern when ANY new source addition requires
editing two code locations.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import structlog

from scout.db import Database

log = structlog.get_logger()


_SIGNAL_SOURCES: list[tuple[str, str, str, str]] = [
    # (table, ts_col, label, token_col)
    ("gainers_snapshots", "snapshot_at", "gainers", "coin_id"),
    ("losers_snapshots", "snapshot_at", "losers", "coin_id"),
    ("trending_snapshots", "snapshot_at", "trending", "coin_id"),
    ("chain_matches", "completed_at", "chains", "token_id"),
    ("predictions", "predicted_at", "narrative", "coin_id"),
    ("velocity_alerts", "detected_at", "velocity", "coin_id"),
    ("volume_spikes", "detected_at", "volume_spike", "coin_id"),
    ("tg_social_signals", "created_at", "tg_social", "token_id"),
]


# Module-level cache of which signal sources are missing from the DB
# (e.g., partial-rollback snapshot). First miss is logged once;
# subsequent calls skip the source. Test isolation: see
# clear_missing_sources_cache_for_tests() per design-v2 arch-S2.
#
# PR-review H3-silent: TTL added to allow recovery without process restart.
# If a missing table is later re-added (migration completes, DB restored
# from backup), the cache entry expires and the next call re-probes.
_signal_sources_missing: set[str] = set()
_MISSING_CACHE_TTL_SEC = 3600  # 1 hour
_signal_sources_missing_at: dict[str, float] = {}


def clear_missing_sources_cache_for_tests() -> None:
    """design-v2 arch-S2: test-isolation helper paralleling
    `params.py:213-217 clear_cache_for_tests()`. Conftest registers as
    autouse fixture; per-test reset prevents tmp_path-DB cross-pollution
    of the missing-table set."""
    _signal_sources_missing.clear()
    _signal_sources_missing_at.clear()


def _is_in_missing_cache(table: str) -> bool:
    """PR-review H3-silent: TTL-aware membership test."""
    if table not in _signal_sources_missing:
        return False
    cached_at = _signal_sources_missing_at.get(table, 0.0)
    if (time.monotonic() - cached_at) > _MISSING_CACHE_TTL_SEC:
        # Expired — drop from cache, allow re-probe.
        _signal_sources_missing.discard(table)
        _signal_sources_missing_at.pop(table, None)
        return False
    return True


def _mark_table_missing(table: str) -> None:
    _signal_sources_missing.add(table)
    _signal_sources_missing_at[table] = time.monotonic()


async def _table_exists(db: Database, table: str) -> bool:
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return (await cur.fetchone()) is not None


async def _count_stacked_signals_in_window(
    db: Database,
    token_id: str,
    opened_at: str,
    end_at: str,
    exclude_trade_id: int | None = None,
) -> tuple[int, list[str]]:
    """Count DISTINCT signal-source firings on token_id within the window.

    `exclude_trade_id` (design-v2 adv-S2): when supplied, the paper_trades
    DISTINCT scan excludes the current trade so it does not count itself
    as a "confirmation" — preserves "independent confirmation" semantics.
    Backtest passes None (off-line replay; trade IDs don't matter);
    production passes the current trade_id.
    """
    sources: list[str] = []
    for table, ts_col, label, token_col in _SIGNAL_SOURCES:
        if _is_in_missing_cache(table):
            continue
        if not await _table_exists(db, table):
            _mark_table_missing(table)
            log.warning(
                "conviction_signal_source_missing",
                table=table,
                hint="stack count will not include this source",
            )
            continue
        try:
            cur = await db._conn.execute(
                f"""SELECT 1 FROM {table}
                    WHERE {token_col} = ?
                      AND datetime({ts_col}) >= datetime(?)
                      AND datetime({ts_col}) <= datetime(?)
                    LIMIT 1""",
                (token_id, opened_at, end_at),
            )
            if (await cur.fetchone()) is not None:
                sources.append(label)
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                f"OperationalError on {table}.{ts_col} "
                f"(column may have been renamed; surfaced rather than "
                f"silently continuing): {exc}"
            ) from exc

    # paper_trades distinct signal_types — design-v2 adv-S2 exclude self
    if not _is_in_missing_cache("paper_trades") and await _table_exists(
        db, "paper_trades"
    ):
        try:
            if exclude_trade_id is not None:
                cur = await db._conn.execute(
                    """SELECT DISTINCT signal_type FROM paper_trades
                       WHERE token_id = ?
                         AND id != ?
                         AND datetime(opened_at) >= datetime(?)
                         AND datetime(opened_at) <= datetime(?)""",
                    (token_id, exclude_trade_id, opened_at, end_at),
                )
            else:
                cur = await db._conn.execute(
                    """SELECT DISTINCT signal_type FROM paper_trades
                       WHERE token_id = ?
                         AND datetime(opened_at) >= datetime(?)
                         AND datetime(opened_at) <= datetime(?)""",
                    (token_id, opened_at, end_at),
                )
            for r in await cur.fetchall():
                sources.append(f"trade:{r[0]}")
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                f"OperationalError on paper_trades stack scan: {exc}"
            ) from exc
    return len(sources), sources


# Per backlog.md:374-380 spec table.
_CONVICTION_LOCK_DELTAS: dict[int, dict[str, float]] = {
    1: {
        "max_duration_hours": 0,
        "trail_pct": 0.0,
        "sl_pct": 0.0,
        "trail_cap": 35.0,
        "sl_cap": 25.0,
    },
    2: {
        "max_duration_hours": 72,
        "trail_pct": 5.0,
        "sl_pct": 5.0,
        "trail_cap": 35.0,
        "sl_cap": 35.0,
    },
    3: {
        "max_duration_hours": 168,
        "trail_pct": 10.0,
        "sl_pct": 10.0,
        "trail_cap": 35.0,
        "sl_cap": 40.0,
    },
    4: {
        "max_duration_hours": 336,
        "trail_pct": 15.0,
        "sl_pct": 15.0,
        "trail_cap": 35.0,
        "sl_cap": 40.0,
    },
}


def conviction_locked_params(stack: int, base: dict) -> dict:
    """Return base params with BL-067 conviction-lock deltas applied.

    Saturates at stack=4. Stack=1 returns base unchanged. Only widens
    `trail_pct`, `sl_pct`, `max_duration_hours` (per spec table —
    `trail_pct_low_peak` / leg targets / qty_frac NOT widened, design
    decision per S6/A3 plan-review notes).
    """
    bucket = min(max(stack, 1), 4)
    delta = _CONVICTION_LOCK_DELTAS[bucket]
    return {
        "max_duration_hours": int(
            base["max_duration_hours"] + delta["max_duration_hours"]
        ),
        "trail_pct": float(
            min(base["trail_pct"] + delta["trail_pct"], delta["trail_cap"])
        ),
        "sl_pct": float(
            min(base["sl_pct"] + delta["sl_pct"], delta["sl_cap"])
        ),
    }


# Real-time stack window: [opened_at, opened_at + 504h] capped at "now"
# (matches BL-067 backtest M1 fix). 504h = stack=4 max_duration ceiling.
_MAX_LOCKED_HOURS = 504


async def compute_stack(
    db: Database,
    token_id: str,
    opened_at: str,
    exclude_trade_id: int | None = None,
) -> int:
    """Real-time stack count for a paper trade.

    Window: [opened_at, min(opened_at + 504h, now)] — captures signals
    that would have fired in the extended-lock window, not just the
    actual closed-trade window.

    `exclude_trade_id` (design-v2 adv-S2): production callers pass the
    current trade_id so the paper_trades DISTINCT scan doesn't count
    the trade itself as a confirmation. Backtest passes None.

    Defensive: returns 0 for empty token_id, OR when db._conn is None
    (shutdown race per M4 fix). Caller treats stack=0 as no-lock-eligible.
    Failure mode = fail-closed.
    """
    if not token_id:
        return 0
    if db._conn is None:
        log.warning(
            "conviction_lock_db_closed",
            token_id=token_id,
            hint="db._conn is None — returning stack=0 fail-closed",
        )
        return 0
    open_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
    if open_dt.tzinfo is None:
        open_dt = open_dt.replace(tzinfo=timezone.utc)
    end_dt = min(
        open_dt + timedelta(hours=_MAX_LOCKED_HOURS),
        datetime.now(timezone.utc),
    )
    n, _ = await _count_stacked_signals_in_window(
        db,
        token_id,
        opened_at,
        end_dt.isoformat(),
        exclude_trade_id=exclude_trade_id,
    )
    return n


# PR-review N5-arch: public alias so cross-module imports (backtest
# script) don't violate the leading-underscore module-private convention.
# IMPORTANT — PR-review M3-silent: this helper MUST NOT introduce real
# `await` calls (e.g., `asyncio.sleep(0)`, pool acquires). The backtest
# sync wrapper drives the coroutine via `coro.send(None)` once and
# expects synchronous completion through the `_SyncDBShim`. T7 round-trip
# pin catches this in CI.
count_stacked_signals_in_window = _count_stacked_signals_in_window
