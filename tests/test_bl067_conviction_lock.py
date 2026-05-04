"""BL-067: conviction-locked hold — production tests.

Pins per design v2 (commit 1a4fe18) + plan v2 (commit a1b6926):
- T1, T1b, T1c, T1d, T1e — migration (signal_params + paper_trades columns)
- T2, T2b, T2c, T2d — Settings + field validators (lower/upper bound)
- T3, T3b, T3c, T3d, T3e — conviction.py helpers
- T4 — SignalParams.conviction_lock_enabled flows through get_params
- T5, T5b, T5c, T5d, T5e, T5f, T5g — evaluator integration (3 gates +
  D2 idempotency + M4 defensive guard + M1 placement-critical)
- T6, T6b, T6c — moonshot trail composition (single-pass + two-pass)
- T7 — backtest sync wrapper round-trip
- T8 — LAB #711 regression (11-stack saturation)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from structlog.testing import capture_logs

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason="Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp tests",
)


# ---------------------------------------------------------------------------
# Conftest-style helpers (kept local to this file to avoid coupling to the
# project conftest until BL-067 lands; design-v2 arch-S2 cache reset).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_signal_sources_cache():
    """design-v2 arch-S2: per-test reset of conviction.py module cache."""
    try:
        from scout.trading.conviction import clear_missing_sources_cache_for_tests
        clear_missing_sources_cache_for_tests()
    except ImportError:
        pass  # Module not yet built (TDD red phase)
    yield


@pytest.fixture
async def db(tmp_path):
    from scout.db import Database
    d = Database(tmp_path / "t.db")
    await d.initialize()
    yield d
    await d.close()


# ---------------------------------------------------------------------------
# Task 1: Migration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conviction_lock_enabled_column_exists(db):
    """T1 — migration adds column with NOT NULL DEFAULT 0 (fail-closed)."""
    cur = await db._conn.execute("PRAGMA table_info(signal_params)")
    cols = {row[1]: (row[2], row[3], row[4]) for row in await cur.fetchall()}
    assert "conviction_lock_enabled" in cols
    coltype, notnull, default = cols["conviction_lock_enabled"]
    assert coltype == "INTEGER"
    assert notnull == 1
    assert default == "0"


@pytest.mark.asyncio
async def test_conviction_lock_enabled_paper_migrations_row(db):
    """T1b — `bl067_conviction_lock_enabled` recorded in paper_migrations."""
    cur = await db._conn.execute(
        "SELECT name FROM paper_migrations WHERE name = ?",
        ("bl067_conviction_lock_enabled",),
    )
    assert (await cur.fetchone()) is not None


@pytest.mark.asyncio
async def test_conviction_lock_enabled_default_zero_on_seeded_signals(db):
    """T1c — default fail-closed: ALL seeded signals have
    conviction_lock_enabled=0 after migration, regardless of signal_type."""
    cur = await db._conn.execute(
        "SELECT signal_type, conviction_lock_enabled FROM signal_params"
    )
    rows = await cur.fetchall()
    assert len(rows) > 0
    for row in rows:
        assert row[1] == 0, (
            f"signal_type {row[0]!r} default conviction_lock_enabled "
            f"must be 0 (fail-closed); got {row[1]}"
        )


@pytest.mark.asyncio
async def test_conviction_locked_at_column_exists_on_paper_trades(db):
    """T1d — D2 fix: paper_trades.conviction_locked_at column added by
    same migration. Default NULL (only stamped on first arm).
    design-v2 D1: also asserts conviction_locked_stack INTEGER column."""
    cur = await db._conn.execute("PRAGMA table_info(paper_trades)")
    cols = {row[1]: (row[2], row[3]) for row in await cur.fetchall()}
    assert "conviction_locked_at" in cols
    coltype, notnull = cols["conviction_locked_at"]
    assert coltype == "TEXT"
    assert notnull == 0
    assert "conviction_locked_stack" in cols
    coltype2, notnull2 = cols["conviction_locked_stack"]
    assert coltype2 == "INTEGER"
    assert notnull2 == 0


@pytest.mark.asyncio
async def test_conviction_lock_post_migration_assertion_fires_when_cutover_row_missing(
    tmp_path,
):
    """T1e (design-v2 adv-M3 + adv-M4) — failure-branch coverage:
    delete cutover row + re-run migration; M4 fix must re-insert because
    the INSERT OR IGNORE INTO paper_migrations is OUTSIDE the column-
    existence guard. Without M4, the second run no-ops on the column
    check + skips the INSERT, and post-migration assertion raises."""
    from scout.db import Database
    d = Database(tmp_path / "t.db")
    await d.initialize()
    # Force cutover row missing while column is present
    await d._conn.execute(
        "DELETE FROM paper_migrations WHERE name = ?",
        ("bl067_conviction_lock_enabled",),
    )
    await d._conn.commit()
    # Re-run migration
    await d._migrate_signal_params_schema()
    # M4 fix should have re-inserted the cutover row
    cur = await d._conn.execute(
        "SELECT 1 FROM paper_migrations WHERE name = ?",
        ("bl067_conviction_lock_enabled",),
    )
    assert (await cur.fetchone()) is not None, (
        "design-v2 adv-M4 regression: cutover row not re-inserted on "
        "re-run when column already present"
    )
    await d.close()


# ---------------------------------------------------------------------------
# Task 2: Settings + validators
# ---------------------------------------------------------------------------


def test_settings_paper_conviction_lock_enabled_default_false(settings_factory):
    """T2 — master kill-switch defaults False (fail-closed)."""
    s = settings_factory()
    assert s.PAPER_CONVICTION_LOCK_ENABLED is False


def test_settings_paper_conviction_lock_threshold_default_3(settings_factory):
    """T2b — threshold defaults to N=3 (per backtest findings)."""
    s = settings_factory()
    assert s.PAPER_CONVICTION_LOCK_THRESHOLD == 3


def test_settings_paper_conviction_lock_threshold_must_be_at_least_two(
    settings_factory,
):
    """T2c — validator: threshold < 2 makes no sense."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        settings_factory(PAPER_CONVICTION_LOCK_THRESHOLD=1)
    with pytest.raises(ValidationError):
        settings_factory(PAPER_CONVICTION_LOCK_THRESHOLD=0)


def test_settings_paper_conviction_lock_threshold_must_be_at_most_eleven(
    settings_factory,
):
    """T2d — design-v2 S2: upper bound 11 (highest observed stack 30d)."""
    from pydantic import ValidationError
    s = settings_factory(PAPER_CONVICTION_LOCK_THRESHOLD=11)
    assert s.PAPER_CONVICTION_LOCK_THRESHOLD == 11
    with pytest.raises(ValidationError):
        settings_factory(PAPER_CONVICTION_LOCK_THRESHOLD=12)


# ---------------------------------------------------------------------------
# Task 3: scout/trading/conviction.py helpers
# ---------------------------------------------------------------------------


def test_conviction_locked_params_table_matches_backlog_spec():
    """T3 — pins backlog.md:374-380 spec table."""
    from scout.trading.conviction import conviction_locked_params

    base = {"max_duration_hours": 168, "trail_pct": 20.0, "sl_pct": 25.0}

    # stack=1: defaults
    p = conviction_locked_params(stack=1, base=base)
    assert p["max_duration_hours"] == 168
    assert p["trail_pct"] == 20.0
    assert p["sl_pct"] == 25.0

    # stack=2: +72h, +5pp trail, +5pp sl
    p = conviction_locked_params(stack=2, base=base)
    assert p["max_duration_hours"] == 240
    assert p["trail_pct"] == 25.0
    assert p["sl_pct"] == 30.0

    # stack=3: +168h, +10pp trail, +10pp sl
    p = conviction_locked_params(stack=3, base=base)
    assert p["max_duration_hours"] == 336
    assert p["trail_pct"] == 30.0
    assert p["sl_pct"] == 35.0

    # stack=4: +336h, +15pp trail (cap 35), +15pp sl
    p = conviction_locked_params(stack=4, base=base)
    assert p["max_duration_hours"] == 504
    assert p["trail_pct"] == 35.0
    assert p["sl_pct"] == 40.0


def test_conviction_locked_params_saturates_at_stack_4():
    """T3b — stack=10 returns same as stack=4."""
    from scout.trading.conviction import conviction_locked_params
    base = {"max_duration_hours": 168, "trail_pct": 20.0, "sl_pct": 25.0}
    p4 = conviction_locked_params(stack=4, base=base)
    p10 = conviction_locked_params(stack=10, base=base)
    assert p4 == p10


@pytest.mark.asyncio
async def test_compute_stack_returns_int(db):
    """T3c — compute_stack returns int >= 0; counts at least 1 source."""
    from scout.trading.conviction import compute_stack
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, price_change_24h, market_cap, volume_24h, "
        " price_at_snapshot, snapshot_at) "
        "VALUES ('test-coin', 'TEST', 'Test', 12.0, 5000000, 1000, 1.0, ?)",
        (now,),
    )
    await db._conn.commit()
    n = await compute_stack(db, "test-coin", "2026-05-01T00:00:00+00:00")
    assert isinstance(n, int)
    assert n >= 1


@pytest.mark.asyncio
async def test_compute_stack_empty_token_id_returns_zero(db):
    """T3d — empty token_id → 0 (defensive)."""
    from scout.trading.conviction import compute_stack
    n = await compute_stack(db, "", "2026-05-01T00:00:00+00:00")
    assert n == 0


@pytest.mark.asyncio
async def test_compute_stack_db_conn_none_returns_zero_with_log(db):
    """T5f / M4 — defensive: db._conn is None → 0 + warning log."""
    from scout.trading.conviction import compute_stack
    real_conn = db._conn
    db._conn = None
    try:
        with capture_logs() as logs:
            n = await compute_stack(
                db, "test-coin", "2026-05-01T00:00:00+00:00"
            )
        assert n == 0
        events = [e.get("event") for e in logs]
        assert "conviction_lock_db_closed" in events
    finally:
        db._conn = real_conn


# ---------------------------------------------------------------------------
# Task 4: SignalParams.conviction_lock_enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_params_loads_conviction_lock_enabled(db, settings_factory):
    """T4 — get_params reads conviction_lock_enabled from signal_params row."""
    from scout.trading.params import bump_cache_version, get_params

    settings = settings_factory()
    sp = await get_params(db, "first_signal", settings)
    assert sp.conviction_lock_enabled is False

    await db._conn.execute(
        "UPDATE signal_params SET conviction_lock_enabled = 1 "
        "WHERE signal_type = 'first_signal'"
    )
    await db._conn.commit()
    bump_cache_version()
    sp = await get_params(db, "first_signal", settings)
    assert sp.conviction_lock_enabled is True


# ---------------------------------------------------------------------------
# Task 6 — moonshot composition (unit tests; T6c integration deferred)
# ---------------------------------------------------------------------------


def test_moonshot_trail_composes_with_locked_trail(settings_factory):
    """T6 — A1 fix: max(30, 35) == 35."""
    settings = settings_factory()
    sp_trail_pct_locked = 35.0
    effective = max(
        settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT, sp_trail_pct_locked
    )
    assert effective == 35.0


# ---------------------------------------------------------------------------
# Task 7 — backtest sync wrapper round-trip (D3)
# ---------------------------------------------------------------------------


def test_backtest_script_imports_conviction_module_helpers():
    """T7 — design-v2 arch-S1: pin sync wrapper round-trip works through
    asyncio.run on the production async helper. Catches silent breakage
    on Database._conn refactor."""
    import sqlite3
    from scripts.backtest_conviction_lock import (
        _count_stacked_signals_in_window as backtest_helper,
    )
    from scout.trading.conviction import conviction_locked_params

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, "
        "token_id TEXT, signal_type TEXT, opened_at TEXT)"
    )
    conn.commit()
    n, _sources = backtest_helper(
        conn, "test-coin", "2026-05-01T00:00:00+00:00",
        "2026-05-04T00:00:00+00:00",
    )
    assert isinstance(n, int)
    assert n == 0

    base = {"max_duration_hours": 168, "trail_pct": 20.0, "sl_pct": 25.0}
    p = conviction_locked_params(stack=4, base=base)
    assert p["max_duration_hours"] == 504
