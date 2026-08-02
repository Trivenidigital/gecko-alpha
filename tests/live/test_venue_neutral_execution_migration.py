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


# ---------------------------------------------------------------------------
# The rebuild path — exercised for real
# ---------------------------------------------------------------------------
#
# *** EVERY TEST ABOVE STARTS FROM A FRESH DATABASE, AND ON A FRESH DATABASE THE
# REBUILD NEVER RUNS. ***
#
# The base DDL in `_create_tables` already carries the three new reject reasons and
# both new columns, so `_migrate_venue_neutral_execution_v1` takes its
# already-widened `continue` and the rename-rebuild is never executed. An
# adversarial review instrumented `aiosqlite.Connection.execute` and counted zero
# rebuild statements across two `initialize()` calls — meaning the upgrade
# assertions above (rows preserved, indexes survive, views survive) were passing
# against a rebuild that had not happened.
#
# These tests construct the PRE-widening table shape by hand — the DDL the parent
# commit ships — so the rebuild is forced to run. That is the only way to test an
# upgrade path from a tmp_path fixture.


_PARENT_LIVE_TRADES_DDL = """
CREATE TABLE "live_trades" (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_trade_id      INTEGER NOT NULL REFERENCES paper_trades(id) ON DELETE RESTRICT,
    coin_id             TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    venue               TEXT NOT NULL,
    pair                TEXT NOT NULL,
    signal_type         TEXT NOT NULL,
    size_usd            TEXT NOT NULL,
    entry_order_id      TEXT,
    entry_fill_price    TEXT,
    entry_fill_qty      TEXT,
    mid_at_entry        TEXT,
    entry_slippage_bps  INTEGER,
    status              TEXT NOT NULL CHECK (status IN (
        'open','closed_tp','closed_sl','closed_duration','closed_via_reconciliation',
        'rejected','needs_manual_review'
    )),
    reject_reason       TEXT CHECK (reject_reason IS NULL OR reject_reason IN (
        'no_venue','insufficient_depth','slippage_exceeds_cap','insufficient_balance',
        'daily_cap_hit','kill_switch','exposure_cap','override_disabled',
        'venue_unavailable',
        'notional_cap_exceeded','signal_disabled','token_aggregate',
        'dual_signal_aggregate','all_candidates_failed',
        'master_kill','mode_paper',
        'live_signed_disabled','api_key_lacks_trade_scope'
    )),
    exit_order_id       TEXT,
    exit_fill_price     TEXT,
    realized_pnl_usd    TEXT,
    realized_pnl_pct    TEXT,
    kill_event_id       INTEGER REFERENCES kill_events(id),
    created_at          TEXT NOT NULL,
    closed_at           TEXT,
    client_order_id     TEXT
)
"""


async def _parent_shaped_db(tmp_path, *, rows=3, with_solana_child=True):
    """A database carrying the PRE-widening live_trades, so the rebuild must run.

    Built by replacing the migrated table with the parent commit's DDL after
    `initialize()` has created everything else — which keeps the surrounding schema
    (paper_trades, solana_executions, the views) realistic while forcing the one
    table under test back to its old shape.
    """
    path = tmp_path / "upgrade.db"
    db = Database(path)
    await db.initialize()
    conn = db._conn

    await conn.execute("PRAGMA foreign_keys=OFF")
    await conn.execute("DROP VIEW IF EXISTS cross_venue_exposure")
    await conn.execute("DROP VIEW IF EXISTS cross_venue_pnl")
    await conn.execute("DROP TABLE live_trades")
    await conn.execute(_PARENT_LIVE_TRADES_DDL)
    await conn.execute(
        "CREATE UNIQUE INDEX idx_live_trades_client_order_id "
        "ON live_trades(client_order_id) WHERE client_order_id IS NOT NULL"
    )
    # An index this migration has never heard of, to prove capture-and-replay
    # rather than an enumerated recreate list.
    await conn.execute(
        "CREATE INDEX idx_live_trades_status_probe ON live_trades(status)"
    )
    await conn.execute("""CREATE VIEW cross_venue_exposure AS
           SELECT 'binance' AS venue,
                  COALESCE(SUM(CAST(size_usd AS REAL)), 0) AS open_exposure_usd,
                  COUNT(*) AS open_count
           FROM live_trades WHERE status = 'open'""")
    # Clear the marker so the migration runs again over the reverted table.
    await conn.execute(
        "DELETE FROM paper_migrations WHERE name = 'bl_venue_neutral_execution_v1'"
    )
    await conn.commit()

    for i in range(rows):
        await conn.execute(
            "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
            "signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct, "
            "tp_price, sl_price, status, opened_at) "
            "VALUES ('c','SYM','N','eth','first_signal','{}',1.0,100.0,100.0,"
            "20.0,10.0,1.2,0.9,'open',?)",
            (f"2026-07-01T00:00:{i:02d}",),
        )
        cur = await conn.execute("SELECT last_insert_rowid()")
        pt_id = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO live_trades (paper_trade_id, coin_id, symbol, venue, pair, "
            "signal_type, size_usd, status, created_at, client_order_id, "
            "entry_fill_qty) "
            "VALUES (?, 'c', 'SYM', 'binance', 'SYMUSDT', 'first_signal', '100', "
            "'open', '2026-07-01T00:00:00', ?, '1.0')",
            (pt_id, f"legacy-cid-{i}"),
        )
    # Push the AUTOINCREMENT high-water mark well past max(id). `sqlite_sequence`
    # has no unique constraint, so UPSERT is unavailable — UPDATE, then INSERT if
    # the row was not there.
    cur = await conn.execute(
        "UPDATE sqlite_sequence SET seq=9999 WHERE name='live_trades'"
    )
    if cur.rowcount == 0:
        await conn.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES ('live_trades', 9999)"
        )

    if with_solana_child:
        cur = await conn.execute("SELECT MIN(id) FROM live_trades")
        first_id = (await cur.fetchone())[0]
        now = "2026-07-01T00:00:00+00:00"
        await conn.execute(
            "INSERT INTO solana_executions "
            "(decision_id, state, mode, live_trade_id, created_at, updated_at) "
            "VALUES ('dec-1', 'transaction_built', 'SUPERVISED_LIVE', ?, ?, ?)",
            (first_id, now, now),
        )
    await conn.commit()
    await conn.execute("PRAGMA foreign_keys=ON")
    await db.close()
    return path


async def test_the_rebuild_actually_runs_on_a_pre_widening_database(tmp_path):
    """Guard on every test below: the fixture really is pre-widening."""
    path = await _parent_shaped_db(tmp_path)
    db = Database(path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='live_trades'"
        )
        sql = (await cur.fetchone())[0]
        for reason in _NEW_REASONS:
            assert reason in sql, "the rebuild did not widen the CHECK"
        cols = await _columns(db._conn, "live_trades")
        assert {"intent_hash", "mandate_mode"} <= cols
    finally:
        await db.close()


async def test_the_rebuild_does_not_sever_solana_execution_links(tmp_path):
    """*** DROP TABLE fires ON DELETE SET NULL on every child row. ***

    `solana_executions.live_trade_id REFERENCES live_trades(id) ON DELETE SET NULL`
    means a rebuild with foreign keys enforced permanently NULLs every execution's
    link to its ledger row — silently, because SET NULL is a legal outcome and
    `foreign_key_check` stays clean afterwards. The runtime cost is a stranded
    position: `_recover_interrupted_executions` retires the ledger row via
    `live_trade_id`, and `retire_row` returns early on None, so the row stays
    `open` forever and counts against the lane's exposure and concurrency caps.
    """
    path = await _parent_shaped_db(tmp_path)
    db = Database(path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT live_trade_id FROM solana_executions WHERE decision_id='dec-1'"
        )
        link = (await cur.fetchone())[0]
        assert link is not None, "the rebuild severed the Solana execution link"
        cur = await db._conn.execute("SELECT MIN(id) FROM live_trades")
        assert link == (await cur.fetchone())[0]
    finally:
        await db.close()


async def test_the_rebuild_leaves_no_foreign_key_violations(tmp_path):
    path = await _parent_shaped_db(tmp_path)
    db = Database(path)
    await db.initialize()
    try:
        cur = await db._conn.execute("PRAGMA foreign_key_check")
        assert list(await cur.fetchall()) == []
        cur = await db._conn.execute("PRAGMA integrity_check")
        assert (await cur.fetchone())[0] == "ok"
    finally:
        await db.close()


async def test_foreign_key_enforcement_is_restored_after_the_rebuild(tmp_path):
    """Leaving `PRAGMA foreign_keys=OFF` would disable enforcement for the whole
    process, not just the migration — every writer after this point."""
    path = await _parent_shaped_db(tmp_path)
    db = Database(path)
    await db.initialize()
    try:
        cur = await db._conn.execute("PRAGMA foreign_keys")
        assert (await cur.fetchone())[0] == 1
    finally:
        await db.close()


async def test_the_rebuild_preserves_every_row_and_every_index(tmp_path):
    path = await _parent_shaped_db(tmp_path, rows=4)
    db = Database(path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT client_order_id FROM live_trades ORDER BY id"
        )
        assert [r[0] for r in await cur.fetchall()] == [
            f"legacy-cid-{i}" for i in range(4)
        ]
        idx = await _indexes(db._conn)
        assert "idx_live_trades_status_probe" in idx
        assert "idx_live_trades_client_order_id" in idx
    finally:
        await db.close()


async def test_the_rebuild_preserves_the_autoincrement_high_water_mark(tmp_path):
    """AUTOINCREMENT promises an id is never reused. The rebuild resets
    `sqlite_sequence` to max(id), making every id above it allocatable again — and
    live_trade ids are quoted in evidence files, Telegram messages and Solana
    execution rows, where a reused id names a different trade."""
    path = await _parent_shaped_db(tmp_path)
    db = Database(path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='live_trades'"
        )
        assert (await cur.fetchone())[0] == 9999
    finally:
        await db.close()


async def test_a_rewrite_that_misses_refuses_to_record_itself_as_applied(tmp_path):
    """*** A marker is a claim that the migration worked. ***

    On a table shape the rewrite does not match, the migration logs a warning and
    moves on. Without the post-condition it would still write the marker, be
    permanently recorded as applied, never retry — and every mandate refusal would
    then raise IntegrityError forever, which is the exact failure the widening
    exists to prevent. The refusal must be loud AND un-marked.
    """
    path = tmp_path / "odd.db"
    db = Database(path)
    await db.initialize()
    conn = db._conn
    await conn.execute("PRAGMA foreign_keys=OFF")
    await conn.execute("DROP VIEW IF EXISTS cross_venue_exposure")
    await conn.execute("DROP VIEW IF EXISTS cross_venue_pnl")
    await conn.execute("DROP TABLE live_trades")
    # `NOT NULL` between TEXT and CHECK — a shape the rewrite pattern misses.
    await conn.execute(
        _PARENT_LIVE_TRADES_DDL.replace(
            "reject_reason       TEXT CHECK", "reject_reason       TEXT NOT NULL CHECK"
        ).replace("reject_reason IS NULL OR ", "")
    )
    await conn.execute(
        "DELETE FROM paper_migrations WHERE name = 'bl_venue_neutral_execution_v1'"
    )
    await conn.commit()
    await conn.execute("PRAGMA foreign_keys=ON")
    await db.close()

    db = Database(path)
    with pytest.raises(RuntimeError, match="still refuses"):
        await db.initialize()
    await db.close()

    # And the migration is NOT recorded, so the next boot retries it.
    probe = await aiosqlite.connect(str(path))
    try:
        cur = await probe.execute(
            "SELECT 1 FROM paper_migrations "
            "WHERE name='bl_venue_neutral_execution_v1'"
        )
        assert await cur.fetchone() is None
    finally:
        await probe.close()


# ---------------------------------------------------------------------------
# The post-conditions must not have a larger blast radius than what they guard,
# and the restore must actually restore.
# ---------------------------------------------------------------------------


async def _plant_dangling(path, table_sql):
    probe = await aiosqlite.connect(str(path))
    try:
        await probe.execute("PRAGMA foreign_keys=OFF")
        await probe.execute(table_sql)
        await probe.commit()
    finally:
        await probe.close()


async def test_a_dangling_fk_in_an_unrelated_table_does_not_block_the_migration(
    tmp_path,
):
    """*** A MIGRATION'S POST-CONDITION MUST BE ABOUT THE MIGRATION. ***

    `PRAGMA foreign_key_check` with no argument checks the ENTIRE database. One
    pre-existing dangling row in a table this migration never opens would raise,
    roll back, leave the marker unwritten — and therefore fail EVERY boot from
    then on, permanently, over data the migration had nothing to do with. There
    are eleven FK-bearing tables it never opens.

    Not hypothetical for this database: `dashboard/api.py::_build_alert_outcome`
    documents handling exactly that state in production ("FK set but the
    paper_trades row is gone — ON DELETE SET NULL race / manual delete").
    """
    path = await _parent_shaped_db(tmp_path)
    await _plant_dangling(
        path,
        "INSERT INTO tg_alert_log (paper_trade_id, signal_type, token_id, "
        " alerted_at, outcome) "
        "VALUES (999999,'first_signal','c','2026-07-01T00:00:00Z','sent')",
    )

    probe = await aiosqlite.connect(str(path))
    try:
        cur = await probe.execute("PRAGMA foreign_key_check")
        assert list(await cur.fetchall()), "the fixture did not create a dangling FK"
    finally:
        await probe.close()

    db = Database(path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT 1 FROM paper_migrations "
            "WHERE name='bl_venue_neutral_execution_v1'"
        )
        assert await cur.fetchone() is not None, "unrelated data blocked the migration"
        assert {"intent_hash", "mandate_mode"} <= await _columns(
            db._conn, "live_trades"
        )
    finally:
        await db.close()


async def test_a_dangling_fk_on_a_migrated_table_still_blocks(tmp_path):
    """Guard on the guard: scoping narrowed the check, it did not remove it."""
    path = await _parent_shaped_db(tmp_path)
    await _plant_dangling(
        path,
        "INSERT INTO live_trades (paper_trade_id, coin_id, symbol, venue, pair, "
        "signal_type, size_usd, status, created_at, client_order_id) "
        "VALUES (999999, 'c', 'SYM', 'binance', 'SYMUSDT', 'first_signal', "
        "'100', 'open', '2026-07-01T00:00:00', 'dangling-cid')",
    )
    db = Database(path)
    with pytest.raises(RuntimeError) as exc:
        await db.initialize()
    await db.close()
    # And the diagnostic names the table and prints the row, not an object address.
    assert "live_trades" in str(exc.value)
    assert "sqlite3.Row object" not in str(exc.value)


async def test_the_post_condition_reads_the_check_clause_not_the_whole_ddl():
    """The post-condition backstops the already-widened skip at the top of the
    loop. Both were the same substring test over the full CREATE TABLE text, so
    any occurrence of the three strings ANYWHERE — a comment, a column name,
    another constraint — satisfied both while the CHECK itself stayed narrow. A
    backstop that fails the same way as the thing it backstops is not a backstop.
    """
    import inspect

    src = inspect.getsource(Database._migrate_venue_neutral_execution_v1)
    assert "check_clause = pattern.search" in src, (
        "the post-condition no longer isolates the CHECK clause before testing "
        "membership"
    )
    # And the pattern must be bound OUTSIDE the rewrite loop, or the fresh-install
    # path (where every table `continue`s before the assignment) raises NameError.
    assert src.index("pattern = _re.compile") < src.index(
        'for table in ("shadow_trades", "live_trades")'
    )


async def test_the_scoped_check_covers_every_table_the_migration_rebuilds():
    """If the rebuild grows a table, the check must grow with it — otherwise the
    scoping that fixed the blast radius quietly stops covering the new one."""
    import inspect

    src = inspect.getsource(Database._migrate_venue_neutral_execution_v1)
    # The pragma must be called WITH an argument — the bare form checks the whole
    # database, which is the blast-radius defect.
    assert "PRAGMA foreign_key_check({table})" in src
    assert 'PRAGMA foreign_key_check"' not in src.replace(
        "PRAGMA foreign_key_check({table})", ""
    )
    assert '("live_trades", "shadow_trades", "solana_executions")' in src


async def test_enforcement_survives_a_failed_migration(tmp_path):
    """The `finally` runs an UNCONDITIONAL rollback before restoring enforcement.

    `PRAGMA foreign_keys` is a no-op inside a transaction — which is why OFF has
    to precede BEGIN, and the same rule applies to the ON in `finally`. The
    `except` block rolls back inside its own try/except so a failing rollback
    cannot mask the original error; swallowing it leaves the transaction OPEN,
    and then the restore silently does nothing.

    Driven through a real failure: a post-condition violation on a migrated table
    raises after the rebuild, so the transaction is live when `finally` runs.
    """
    path = await _parent_shaped_db(tmp_path)
    await _plant_dangling(
        path,
        "INSERT INTO live_trades (paper_trade_id, coin_id, symbol, venue, pair, "
        "signal_type, size_usd, status, created_at, client_order_id) "
        "VALUES (999999, 'c', 'SYM', 'binance', 'SYMUSDT', 'first_signal', "
        "'100', 'open', '2026-07-01T00:00:00', 'dangling-2')",
    )
    db = Database(path)
    with pytest.raises(RuntimeError):
        await db.initialize()
    try:
        # The connection either has enforcement back ON, or was dropped rather
        # than handed back unenforced. Both are acceptable; "open and unenforced"
        # is not.
        if db._conn is not None:
            cur = await db._conn.execute("PRAGMA foreign_keys")
            assert (await cur.fetchone())[
                0
            ] == 1, "a connection with foreign keys OFF was returned to the caller"
    finally:
        await db.close()
