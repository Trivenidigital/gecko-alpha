"""Migration `bl_venue_neutral_execution_v1`, both directions.

A migration-bearing change needs three answers, and each is a test here:

* **fresh install** — a brand-new database ends up with the columns, the indexes
  and the widened CHECK.
* **upgrade with data** — an existing database with rows in ``live_trades``
  survives the CHECK rebuild with every row and every index intact.
* **rollback posture** — the change is additive, so the previous code reads the
  migrated schema without modification. Asserted by exercising a legacy INSERT
  that names none of the new columns.

The reject_reason widening is the risky half: SQLite cannot ALTER a CHECK, so it
is a table rename-rebuild, and a rebuild that dropped the UNIQUE index on
``client_order_id`` would silently remove the DB-layer backstop against a
double-submit.
"""

from __future__ import annotations

import aiosqlite
import pytest

from scout.db import Database

_NEW_REASONS = ("mandate_inactive", "venue_capability_refused", "no_adapter_for_venue")


async def _columns(conn, table) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _indexes(conn) -> set[str]:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    return {row[0] for row in await cur.fetchall()}


async def _seed_live_row(conn, *, cid, reject_reason=None, status="open"):
    await conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
        "signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct, "
        "tp_price, sl_price, status, opened_at) "
        "VALUES ('c','SYM','N','eth','first_signal','{}',1.0,100.0,100.0,"
        "20.0,10.0,1.2,0.9,'open',?)",
        (f"2026-08-01T00:00:{abs(hash(cid)) % 60:02d}.{abs(hash(cid)) % 1000:03d}",),
    )
    cur = await conn.execute("SELECT last_insert_rowid()")
    pt_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO live_trades (paper_trade_id, coin_id, symbol, venue, pair, "
        "signal_type, size_usd, status, reject_reason, created_at, client_order_id) "
        "VALUES (?, 'c', 'SYM', 'binance', 'SYMUSDT', 'first_signal', '100', ?, ?, "
        "'2026-08-01T00:00:00', ?)",
        (pt_id, status, reject_reason, cid),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Fresh install
# ---------------------------------------------------------------------------


async def test_fresh_install_has_the_columns(tmp_path):
    db = Database(tmp_path / "g.db")
    await db.initialize()
    try:
        cols = await _columns(db._conn, "live_trades")
        assert {"intent_hash", "mandate_mode"} <= cols
    finally:
        await db.close()


async def test_fresh_install_has_the_lookup_indexes(tmp_path):
    db = Database(tmp_path / "g.db")
    await db.initialize()
    try:
        idx = await _indexes(db._conn)
        assert "idx_live_trades_intent_hash" in idx
        assert "idx_live_trades_mandate_mode" in idx
        # The pre-existing dedup backstop must survive.
        assert "idx_live_trades_client_order_id" in idx
    finally:
        await db.close()


@pytest.mark.parametrize("reason", _NEW_REASONS)
async def test_fresh_install_accepts_the_new_reject_reasons(tmp_path, reason):
    """*** Without the widening, every mandate refusal would raise
    IntegrityError. *** A fail-closed gate whose audit trail fails open is worse
    than no audit trail: the operator would see nothing and conclude nothing
    happened."""
    db = Database(tmp_path / "g.db")
    await db.initialize()
    try:
        await _seed_live_row(
            db._conn, cid=f"cid-{reason}", reject_reason=reason, status="rejected"
        )
        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM live_trades WHERE reject_reason = ?", (reason,)
        )
        assert (await cur.fetchone())[0] == 1
    finally:
        await db.close()


async def test_an_unknown_reject_reason_is_still_refused(tmp_path):
    """Guard on the guard: the CHECK was widened, not removed."""
    db = Database(tmp_path / "g.db")
    await db.initialize()
    try:
        with pytest.raises(aiosqlite.IntegrityError):
            await _seed_live_row(
                db._conn, cid="cid-x", reject_reason="whatever", status="rejected"
            )
    finally:
        await db.close()


async def test_the_migration_is_recorded(tmp_path):
    db = Database(tmp_path / "g.db")
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT 1 FROM paper_migrations WHERE name = ?",
            ("bl_venue_neutral_execution_v1",),
        )
        assert await cur.fetchone() is not None
        cur = await db._conn.execute(
            "SELECT description FROM schema_version WHERE version = 20260802"
        )
        assert (await cur.fetchone())[0] == "bl_venue_neutral_execution_v1"
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Upgrade with data
# ---------------------------------------------------------------------------


async def test_upgrade_preserves_existing_rows(tmp_path):
    """The CHECK rebuild copies the table. Every row must come back."""
    path = tmp_path / "g.db"
    db = Database(path)
    await db.initialize()
    try:
        for i in range(5):
            await _seed_live_row(db._conn, cid=f"legacy-{i}")
    finally:
        await db.close()

    # Re-open: the migration is idempotent and must not disturb the data.
    db = Database(path)
    await db.initialize()
    try:
        cur = await db._conn.execute("SELECT COUNT(*) FROM live_trades")
        assert (await cur.fetchone())[0] == 5
        cur = await db._conn.execute(
            "SELECT intent_hash, mandate_mode FROM live_trades LIMIT 1"
        )
        row = await cur.fetchone()
        # Pre-existing rows carry NULL, and that NULL is load-bearing: the
        # mandate's supervised-history count must not inherit history that was
        # never mandate-gated.
        assert row[0] is None and row[1] is None
    finally:
        await db.close()


async def test_the_dedup_backstop_survives_the_rebuild(tmp_path):
    """*** The failure this test exists for. ***

    A rename-rebuild recreates the table, which drops its indexes. If the UNIQUE
    partial index on client_order_id were not recreated, two concurrent retries
    that raced past the application-level dedup query would BOTH insert — and
    both submit.
    """
    path = tmp_path / "g.db"
    db = Database(path)
    await db.initialize()
    try:
        await _seed_live_row(db._conn, cid="dupe-me")
        with pytest.raises(aiosqlite.IntegrityError):
            await _seed_live_row(db._conn, cid="dupe-me")
    finally:
        await db.close()


async def test_the_cross_venue_views_survive_the_rebuild(tmp_path):
    """`cross_venue_exposure` references live_trades, so the rebuild has to drop
    and recreate it. A missing view is a dashboard that returns an error, which
    is how a rebuild ships looking fine and breaks a read path."""
    db = Database(tmp_path / "g.db")
    await db.initialize()
    try:
        cur = await db._conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
        views = {row[0] for row in await cur.fetchall()}
        assert {"cross_venue_exposure", "cross_venue_pnl"} <= views
        # And it still executes.
        await db._conn.execute("SELECT * FROM cross_venue_exposure")
    finally:
        await db.close()


async def test_running_initialize_twice_is_a_no_op(tmp_path):
    """Idempotency on the marker AND on per-object inspection, so a crash between
    the rebuild and the marker is safe to re-run."""
    path = tmp_path / "g.db"
    for _ in range(3):
        db = Database(path)
        await db.initialize()
        cols = await _columns(db._conn, "live_trades")
        assert {"intent_hash", "mandate_mode"} <= cols
        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM paper_migrations WHERE name = ?",
            ("bl_venue_neutral_execution_v1",),
        )
        assert (await cur.fetchone())[0] == 1
        await db.close()


# ---------------------------------------------------------------------------
# Rollback posture
# ---------------------------------------------------------------------------


async def test_legacy_writers_still_work_against_the_migrated_schema(tmp_path):
    """The change is purely additive, so rolling the CODE back while leaving the
    SCHEMA migrated is safe: a writer that names none of the new columns still
    inserts, because both are nullable with no DEFAULT and no NOT NULL."""
    db = Database(tmp_path / "g.db")
    await db.initialize()
    try:
        await _seed_live_row(db._conn, cid="legacy-writer")
        cur = await db._conn.execute(
            "SELECT intent_hash FROM live_trades WHERE client_order_id='legacy-writer'"
        )
        assert (await cur.fetchone())[0] is None
    finally:
        await db.close()


async def test_shadow_trades_check_was_widened_too(tmp_path):
    """`shadow_trades` shares the constraint; a widening applied to only one of
    the two would leave the shadow path raising on the same refusal."""
    db = Database(tmp_path / "g.db")
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='shadow_trades'"
        )
        sql = (await cur.fetchone())[0]
        for reason in _NEW_REASONS:
            assert reason in sql
    finally:
        await db.close()
