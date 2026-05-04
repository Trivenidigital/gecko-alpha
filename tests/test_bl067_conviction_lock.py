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


def test_settings_paper_conviction_lock_threshold_upper_bound(
    settings_factory,
):
    """T2d — PR-review M2: upper bound relaxed to 50 (operator escape
    hatch) from previous design-v2 limit of 11. Values > 50 still rejected
    as likely typos."""
    from pydantic import ValidationError
    # 11 still accepted (previously the cap)
    s = settings_factory(PAPER_CONVICTION_LOCK_THRESHOLD=11)
    assert s.PAPER_CONVICTION_LOCK_THRESHOLD == 11
    # 50 boundary accepted (new escape-hatch ceiling)
    s = settings_factory(PAPER_CONVICTION_LOCK_THRESHOLD=50)
    assert s.PAPER_CONVICTION_LOCK_THRESHOLD == 50
    # 51 rejected
    with pytest.raises(ValidationError):
        settings_factory(PAPER_CONVICTION_LOCK_THRESHOLD=51)


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

    # SIGNAL_PARAMS_ENABLED=True forces the table read path (otherwise the
    # function returns Settings fallback with conviction_lock_enabled=False).
    settings = settings_factory(SIGNAL_PARAMS_ENABLED=True)
    sp = await get_params(db, "first_signal", settings)
    assert sp.conviction_lock_enabled is False
    assert sp.source == "table"

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
    """T7 — design-v2 arch-S1 + PR-review N2 tightening: pin sync wrapper
    round-trip + non-trivial assertion (N=1 from one seeded source row)
    against the actual `paper_trades` DISTINCT scan. Catches silent
    breakage on Database._conn refactor AND empty-DB-only T7 weakness."""
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
    conn.execute(
        "INSERT INTO paper_trades (token_id, signal_type, opened_at) "
        "VALUES (?, ?, ?)",
        ("test-coin", "first_signal", "2026-05-02T00:00:00+00:00"),
    )
    conn.commit()
    n, sources = backtest_helper(
        conn, "test-coin",
        "2026-05-01T00:00:00+00:00",
        "2026-05-04T00:00:00+00:00",
    )
    assert isinstance(n, int)
    assert n == 1
    assert "trade:first_signal" in sources

    base = {"max_duration_hours": 168, "trail_pct": 20.0, "sl_pct": 25.0}
    p = conviction_locked_params(stack=4, base=base)
    assert p["max_duration_hours"] == 504


# ---------------------------------------------------------------------------
# PR-review additions: T5/T5d/T5e/T5g/T6b + self-counting check (T3+)
# ---------------------------------------------------------------------------


async def _seed_locked_eligible_trade(
    db,
    *,
    signal_type: str = "first_signal",
    opt_in_signal: bool = False,
    n_extra_sources: int = 3,
    opened_at: str | None = None,
    entry_price: float = 1.0,
    token_id: str = "lab-coin",
    current_price: float | None = None,
):
    """Shared fixture per design-v2 arch-S3.

    Seeds a paper_trade row + ≥`n_extra_sources` source-table rows on the
    same `token_id` after `opened_at`. Defaults: signal_type=first_signal,
    opened_at=now-2d, n_extra_sources=3 (enough for default threshold=3).

    Source-table seeding ORDER (additive — first N seeded):
      1. gainers_snapshots @ opened_at + 1h
      2. trending_snapshots @ opened_at + 2h
      3. volume_spikes @ opened_at + 3h (no chain_matches FK headache)
      ...

    For T8 (n_extra_sources=11) sources 9-11 are extra paper_trades rows
    with different signal_types.
    """
    from datetime import datetime, timedelta, timezone
    if opened_at is None:
        opened_at = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat()
    open_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))

    # Insert paper_trade
    cur = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity,
            tp_price, sl_price, opened_at, status, peak_pct)
           VALUES (?, 'LAB', 'Lab Coin', 'sol', ?, '{}',
                   ?, 100.0, 100.0,
                   ?, ?, ?, 'open', 10.0)""",
        (
            token_id, signal_type,
            entry_price, entry_price * 1.5, entry_price * 0.75,
            opened_at,
        ),
    )
    trade_id = cur.lastrowid

    # Operator opt-in if requested
    if opt_in_signal:
        await db._conn.execute(
            "UPDATE signal_params SET conviction_lock_enabled = 1 "
            "WHERE signal_type = ?",
            (signal_type,),
        )

    # Seed source-table rows (offsets in hours from opened_at)
    seed_specs = [
        # (table, sql, params builder)
        (
            1, "gainers_snapshots",
            "INSERT INTO gainers_snapshots "
            "(coin_id, symbol, name, price_change_24h, market_cap, "
            " volume_24h, price_at_snapshot, snapshot_at) "
            "VALUES (?, 'LAB', 'Lab', 12.0, 5000000, 1000, 1.0, ?)",
        ),
        (
            2, "trending_snapshots",
            # Schema may vary; use flexible inserts. Read schema dynamically.
            None,
        ),
        (
            3, "volume_spikes",
            None,
        ),
    ]
    sources_added = 0
    for hours, table, sql in seed_specs:
        if sources_added >= n_extra_sources:
            break
        ts = (open_dt + timedelta(hours=hours)).isoformat()
        if table == "gainers_snapshots" and sql:
            await db._conn.execute(sql, (token_id, ts))
            sources_added += 1
        else:
            # Probe schema and insert minimal NULLable defaults
            cur = await db._conn.execute(f"PRAGMA table_info({table})")
            cols = [(r[1], r[2], r[3]) for r in await cur.fetchall()]
            if not cols:
                continue
            non_null = [c for c in cols if c[2] == 1]
            colnames = []
            placeholders = []
            values = []
            for name, ctype, _notnull in cols:
                if name == "id":
                    continue
                colnames.append(name)
                placeholders.append("?")
                if name in ("coin_id", "token_id"):
                    values.append(token_id)
                elif name in ("snapshot_at", "detected_at", "created_at",
                              "completed_at", "predicted_at", "recorded_at"):
                    values.append(ts)
                elif ctype.upper() == "TEXT":
                    values.append("LAB")
                elif ctype.upper() in ("INTEGER", "REAL"):
                    values.append(1.0 if ctype.upper() == "REAL" else 1)
                else:
                    values.append(None)
            try:
                await db._conn.execute(
                    f"INSERT INTO {table} ({','.join(colnames)}) "
                    f"VALUES ({','.join(placeholders)})",
                    values,
                )
                sources_added += 1
            except Exception:
                # Table may not exist or schema doesn't match; skip
                continue

    # Extra paper_trades rows for sources 9-11 (different signal_types)
    extra_signals = [
        "gainers_early", "trending_catch", "losers_contrarian",
    ]
    for idx in range(min(max(n_extra_sources - sources_added, 0), 3)):
        ts = (open_dt + timedelta(hours=9 + idx)).isoformat()
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity,
                tp_price, sl_price, opened_at, status)
               VALUES (?, 'LAB', 'Lab Coin', 'sol', ?, '{}',
                       ?, 100.0, 100.0, 1.5, 0.75, ?, 'closed')""",
            (token_id, extra_signals[idx], entry_price, ts),
        )

    # price_cache row keeping the trade live (current_price defaults to
    # entry * 1.05 — peak_pct=10 stays valid; trade survives short-term
    # exit gates on this evaluator pass).
    if current_price is None:
        current_price = entry_price * 1.05
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, updated_at)
           VALUES (?, ?, ?)""",
        (token_id, current_price, now_iso),
    )
    await db._conn.commit()
    return trade_id


@pytest.mark.asyncio
async def test_evaluator_skips_conviction_lock_when_settings_kill_switch_off(
    db, settings_factory
):
    """T5 (PR-review H2 coverage gap) — fail-closed at master gate:
    settings.PAPER_CONVICTION_LOCK_ENABLED=False means NO overlay
    regardless of per-signal flag or stack count."""
    from scout.trading.evaluator import evaluate_paper_trades
    settings = settings_factory(SIGNAL_PARAMS_ENABLED=True)
    # Default: PAPER_CONVICTION_LOCK_ENABLED=False
    assert settings.PAPER_CONVICTION_LOCK_ENABLED is False
    await _seed_locked_eligible_trade(
        db, signal_type="first_signal", opt_in_signal=True,
        n_extra_sources=5,
    )
    with capture_logs() as logs:
        await evaluate_paper_trades(db, settings)
    events = [e.get("event") for e in logs]
    assert "conviction_lock_armed" not in events
    cur = await db._conn.execute(
        "SELECT conviction_locked_at FROM paper_trades WHERE token_id = ?",
        ("lab-coin",),
    )
    row = await cur.fetchone()
    assert row[0] is None  # not stamped


@pytest.mark.asyncio
async def test_evaluator_arms_conviction_lock_when_all_gates_pass(
    db, settings_factory
):
    """T5d (PR-review arch-D1 merge-blocker) — happy path: all 3 gates
    pass → locked params used. Asserts:
    - conviction_lock_armed event fires
    - paper_trades.conviction_locked_at + conviction_locked_stack stamped
    - locked max_duration_hours overlaid"""
    from scout.trading.evaluator import evaluate_paper_trades
    settings = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        PAPER_CONVICTION_LOCK_ENABLED=True,
    )
    await _seed_locked_eligible_trade(
        db, signal_type="first_signal", opt_in_signal=True,
        n_extra_sources=3,
    )
    with capture_logs() as logs:
        await evaluate_paper_trades(db, settings)
    armed = [e for e in logs if e.get("event") == "conviction_lock_armed"]
    assert armed, (
        f"expected conviction_lock_armed; got {[e.get('event') for e in logs]}"
    )
    a = armed[0]
    assert a["stack"] >= 3
    assert a["threshold"] == 3
    # Stack=3 bucket adds +168h. settings_factory()'s default
    # PAPER_MAX_DURATION_HOURS is 48 (from scout/config.py:220), so
    # locked_max_duration_hours = 48 + 168 = 216.
    assert a["locked_max_duration_hours"] == 48 + 168, (
        f"expected stack=3 bucket → base 48 + 168 = 216; "
        f"got {a['locked_max_duration_hours']}"
    )
    cur = await db._conn.execute(
        "SELECT conviction_locked_at, conviction_locked_stack "
        "FROM paper_trades WHERE token_id = ?",
        ("lab-coin",),
    )
    row = await cur.fetchone()
    assert row[0] is not None  # stamped
    assert row[1] is not None
    assert row[1] >= 3


@pytest.mark.asyncio
async def test_evaluator_logs_conviction_lock_armed_only_once(
    db, settings_factory
):
    """T5e (PR-review H1 — D2 idempotency, soak-signal-critical):
    second eval pass on the same armed trade does NOT re-emit the log
    (would corrupt the 14d operator soak monitor with ~672 spurious
    events per locked trade)."""
    from scout.trading.evaluator import evaluate_paper_trades
    settings = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        PAPER_CONVICTION_LOCK_ENABLED=True,
    )
    await _seed_locked_eligible_trade(
        db, signal_type="first_signal", opt_in_signal=True,
        n_extra_sources=3,
    )
    # Pass 1 — arms
    with capture_logs() as logs1:
        await evaluate_paper_trades(db, settings)
    armed1 = [e for e in logs1 if e.get("event") == "conviction_lock_armed"]
    assert len(armed1) == 1
    # Pass 2 — must NOT re-emit
    with capture_logs() as logs2:
        await evaluate_paper_trades(db, settings)
    armed2 = [e for e in logs2 if e.get("event") == "conviction_lock_armed"]
    assert armed2 == [], (
        f"D2 regression: re-emitted on pass 2; got {len(armed2)} events"
    )


@pytest.mark.asyncio
async def test_evaluator_overlay_placement_keeps_trade_alive_past_base_max(
    db, settings_factory
):
    """T5g (PR-review C1 + arch-D1 — STRUCTURAL placement pin):
    seed opened_at so elapsed > base PAPER_MAX_DURATION_HOURS but <
    locked max. Overlay-after-line-158 BUG would close the trade via
    expired (max_duration uses un-overlaid base); correct placement
    keeps status='open' (timedelta uses overlaid locked max).

    Default PAPER_MAX_DURATION_HOURS = 48. Stack=3 locked = 48 + 168 = 216.
    Seed opened_at = now - 100h (100 > 48, 100 < 216).

    Without this test, a future refactor moving the overlay AFTER
    `max_duration = timedelta(...)` makes the feature a silent no-op."""
    from datetime import datetime, timedelta, timezone
    from scout.trading.evaluator import evaluate_paper_trades
    settings = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        PAPER_CONVICTION_LOCK_ENABLED=True,
    )
    opened_at = (
        datetime.now(timezone.utc) - timedelta(hours=100)
    ).isoformat()
    await _seed_locked_eligible_trade(
        db, signal_type="first_signal", opt_in_signal=True,
        n_extra_sources=3, opened_at=opened_at,
    )
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE token_id = ?",
        ("lab-coin",),
    )
    row = await cur.fetchone()
    # Overlay-after-158 BUG would have closed via expired (100h > base 48h).
    # Overlay-before-158 (correct): trade still open at 100h since locked
    # max=216h.
    assert row[0] == "open", (
        f"placement bug: trade closed prematurely; status={row[0]!r}. "
        "Overlay must run BEFORE line 158 max_duration = timedelta(...)."
    )


@pytest.mark.asyncio
async def test_evaluator_moonshot_branch_uses_max_with_sp_trail_pct(
    db, settings_factory, monkeypatch
):
    """T6b (PR-review C2 — moonshot composition production-call-site pin):
    T6 only verified `max(30, 35) == 35` arithmetic; T6b verifies the
    PRODUCTION code path at evaluator.py reads `sp.trail_pct` (overlaid
    by conviction-lock) rather than the moonshot constant alone.

    Setup: stack=4 (saturated → locked trail=35); pre-stamp moonshot_armed_at
    to simulate prior-arming. Force peak_pct above moonshot threshold so the
    moonshot branch fires. Assert the trade survives a price drop within
    the locked 35% drawdown but outside the moonshot 30% drawdown.

    Pre-fix: effective_trail_pct = settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT
    (=30); a 33% drawdown closes the trade.
    Post-fix: effective_trail_pct = max(30, 35) = 35; same drawdown does
    NOT close the trade.
    """
    from datetime import datetime, timezone
    from scout.trading.evaluator import evaluate_paper_trades
    settings = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        PAPER_CONVICTION_LOCK_ENABLED=True,
        PAPER_MOONSHOT_ENABLED=True,
    )
    # Seed trade with stack=4 sources; pre-stamp moonshot_armed_at.
    trade_id = await _seed_locked_eligible_trade(
        db, signal_type="first_signal", opt_in_signal=True,
        n_extra_sources=4,
        entry_price=1.0,
        # Set current_price to simulate a peak retracement: we'll stamp
        # peak_pct via direct UPDATE below, then set current = entry * 0.67
        # which is 33% drawdown from peak (entry was the original anchor).
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    # Stamp moonshot_armed_at + peak_pct=50 + peak_price=1.50.
    # current_price = 1.005 → drawdown from peak (1.50→1.005) = 33%.
    # Locked 35% trail: not triggered (33% < 35%) → trade stays open.
    # Moonshot-only 30% trail: triggered (33% > 30%) → trade closes.
    await db._conn.execute(
        """UPDATE paper_trades
           SET moonshot_armed_at = ?, peak_pct = 50.0, peak_price = 1.50,
               floor_armed = 1
           WHERE id = ?""",
        (now_iso, trade_id),
    )
    await db._conn.execute(
        "UPDATE price_cache SET current_price = 1.005 WHERE coin_id = ?",
        ("lab-coin",),
    )
    await db._conn.commit()
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    row = await cur.fetchone()
    # Without A1 fix, trade closes (33% drawdown > moonshot 30%).
    # With A1 fix, trade survives (33% drawdown < locked 35%).
    assert row[0] == "open", (
        f"A1 regression: moonshot branch ignored locked sp.trail_pct=35; "
        f"trade closed at status={row[0]!r}. "
        "Production must use max(MOONSHOT, sp.trail_pct), not constant."
    )


@pytest.mark.asyncio
async def test_evaluator_emits_disarmed_log_when_master_off_post_arm(
    db, settings_factory
):
    """PR-review H1 silent-failure: emit `conviction_lock_disarmed_post_rollback`
    when a trade has conviction_locked_at set but master kill-switch is OFF.
    Operator-rollback visibility — without this, the fleet of armed trades
    silently retightens trail mid-flight on .env flip."""
    from datetime import datetime, timezone
    from scout.trading.evaluator import evaluate_paper_trades
    # First, arm the trade with master ON
    settings_on = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        PAPER_CONVICTION_LOCK_ENABLED=True,
    )
    await _seed_locked_eligible_trade(
        db, signal_type="first_signal", opt_in_signal=True,
        n_extra_sources=3,
    )
    await evaluate_paper_trades(db, settings_on)
    # Verify armed
    cur = await db._conn.execute(
        "SELECT conviction_locked_at FROM paper_trades "
        "WHERE token_id = ?",
        ("lab-coin",),
    )
    row = await cur.fetchone()
    assert row[0] is not None
    # Now flip master OFF and run another pass
    settings_off = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        PAPER_CONVICTION_LOCK_ENABLED=False,
    )
    with capture_logs() as logs:
        await evaluate_paper_trades(db, settings_off)
    events = [e.get("event") for e in logs]
    assert "conviction_lock_disarmed_post_rollback" in events, (
        f"H1: rollback regression invisible; events={events}"
    )


@pytest.mark.asyncio
async def test_compute_stack_excludes_self_trade_from_paper_trades_count(db):
    """PR-review C3 (test-coverage): paper_trades self-counting prevention
    pinned. compute_stack with exclude_trade_id should not count the
    excluded trade row as a confirmation."""
    from datetime import datetime, timedelta, timezone
    from scout.trading.conviction import compute_stack
    open_dt = datetime.now(timezone.utc) - timedelta(days=1)
    opened_at = open_dt.isoformat()
    # Insert paper_trade for token "x" with the same signal_type — this
    # would inflate stack by 1 if not excluded.
    cur = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            opened_at, status)
           VALUES ('x', 'X', 'X', 'sol', 'first_signal', '{}',
                   1.0, 100.0, 100.0, 1.5, 0.75, ?, 'open')""",
        (opened_at,),
    )
    self_id = cur.lastrowid
    await db._conn.commit()

    # Without exclude_trade_id: stack includes self-reference → at least 1
    n_with_self = await compute_stack(
        db, "x", opened_at, exclude_trade_id=None,
    )
    # With exclude_trade_id=self_id: stack excludes self → 0
    n_without_self = await compute_stack(
        db, "x", opened_at, exclude_trade_id=self_id,
    )
    assert n_with_self >= 1, (
        f"compute_stack should count paper_trades when not excluding "
        f"(got {n_with_self})"
    )
    assert n_without_self == 0, (
        f"compute_stack with exclude_trade_id should drop self-reference "
        f"(got {n_without_self})"
    )
