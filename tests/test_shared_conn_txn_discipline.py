"""F2 corrective: shared-connection transaction-lock discipline.

These tests pin the fix for the F2 defect — foreign writers transacting on the
process-shared aiosqlite connection ``db._conn`` WITHOUT holding ``db._txn_lock``
collided with ``suppression.should_open``'s parole-path ``BEGIN IMMEDIATE`` (which
DID hold the lock), producing "cannot start a transaction within a transaction"
(SUPP_DB_OP noise, fail-closed) AND a foreign ROLLBACK that silently destroyed the
other writer's uncommitted transaction.

All interleavings are driven by ``asyncio.Event`` + a lock that counts
``acquire()`` calls, so the ordering is deterministic — no ``sleep``/timing races.
``asyncio.sleep(0)`` is used only as a cooperative scheduler yield, always bounded
by a definite exit condition.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import io
import json
import pathlib
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
from structlog.testing import capture_logs

from scout.db import Database, NestedTransactionError
from scout.trading import auto_suspend, calibrate, combo_refresh, suppression
from scout.chains import tracker


class _CountingLock(asyncio.Lock):
    """An ``asyncio.Lock`` that records how many times ``acquire()`` was called.

    A second (blocked) ``acquire()`` is the deterministic signal that a waiter has
    reached the lock — used to prove ``should_open`` (or another writer) is parked
    behind an already-open foreign transaction before we release it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.acquire_calls = 0

    async def acquire(self) -> bool:  # type: ignore[override]
        self.acquire_calls += 1
        return await super().acquire()


async def _yield_until(pred, *, max_ticks: int = 100_000) -> None:
    """Cooperatively yield until ``pred()`` is true (bounded, deterministic)."""
    for _ in range(max_ticks):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true within max_ticks")


async def _seed_open_parole_combo(
    db: Database, key: str, *, remaining: int = 3
) -> None:
    """Seed a suppressed combo whose parole window is OPEN (parole_at in the past)
    so ``should_open`` takes the atomic parole-decrement (transaction) path."""
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await db._conn.execute(
        "INSERT OR REPLACE INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        " win_rate_pct, suppressed, suppressed_at, parole_at, "
        " parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES (?, '30d', 25, 5, 20, 0, 0, 20.0, 1, ?, ?, ?, 0, ?)",
        (key, past, past, remaining, past),
    )
    await db._conn.commit()


async def _insert_signal_event(conn, token_id: str) -> None:
    await conn.execute(
        "INSERT INTO signal_events "
        "(token_id, pipeline, event_type, event_data, source_module, created_at) "
        "VALUES (?, 'memecoin', 'first_seen', '{}', 'test', ?)",
        (token_id, datetime.now(timezone.utc).isoformat()),
    )


async def _insert_dex_pool(conn, pool_address: str) -> None:
    await conn.execute(
        "INSERT INTO dex_pool_discoveries "
        "(network, pool_address, base_token_address, base_token_symbol, "
        " quote_token_symbol, pool_created_at, first_seen_at, fdv_usd, "
        " liquidity_usd, volume_h1_usd) "
        "VALUES ('base', ?, '0xbase', 'FOO', 'WETH', NULL, ?, 1000.0, 500.0, 100.0)",
        (pool_address, datetime.now(timezone.utc).isoformat()),
    )


@pytest.fixture(autouse=True)
def _reset_suppression_fallback_state():
    suppression._fallback_timestamps.clear()
    suppression._last_alerted_ts = float("-inf")
    yield
    suppression._fallback_timestamps.clear()
    suppression._last_alerted_ts = float("-inf")


async def test_suppression_vs_chain_tracker_serialize(tmp_path, settings_factory):
    """A chain-tracker-style transaction held OPEN on the shared connection must
    not collide with should_open's parole transaction: should_open serializes
    behind the lock (no SUPP_DB_OP), and BOTH writes commit (no foreign rollback).
    """
    db = Database(tmp_path / "f2_chain.db")
    await db.initialize()
    db._txn_lock = _CountingLock()
    lock = db._txn_lock
    s = settings_factory()
    await _seed_open_parole_combo(db, "chain_combo", remaining=3)

    txn_open = asyncio.Event()
    may_commit = asyncio.Event()

    async def foreign_chain_writer() -> None:
        # Same discipline check_chains now uses: db.transaction() holds the lock
        # + an OPEN BEGIN IMMEDIATE transaction across an await point.
        async with db.transaction() as conn:
            await _insert_signal_event(conn, "FOREIGN_TOKEN")
            txn_open.set()
            await may_commit.wait()

    foreign_task = asyncio.create_task(foreign_chain_writer())
    await txn_open.wait()
    assert lock.locked()  # foreign holds the lock + an open transaction
    assert lock.acquire_calls == 1

    with capture_logs() as cap:
        should_task = asyncio.create_task(
            suppression.should_open(db, "chain_combo", settings=s)
        )
        # Deterministic: should_open has reached (and is blocked on) the lock
        # once it makes the 2nd acquire() call.
        await _yield_until(lambda: lock.acquire_calls >= 2)
        assert not should_task.done()  # provably parked behind the foreign txn
        # Release the foreign transaction; should_open must serialize behind it.
        may_commit.set()
        await foreign_task
        allow, reason = await should_task

    # should_open completed cleanly — no "transaction within a transaction".
    assert (allow, reason) == (True, "parole_retest")
    assert not any(e.get("err_id") == "SUPP_DB_OP" for e in cap)
    assert not any(e.get("event") == "suppression_db_operational_error" for e in cap)

    # The foreign chain-tracker write COMMITTED (should_open did not roll it back).
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE token_id = ?", ("FOREIGN_TOKEN",)
    )
    assert (await cur.fetchone())[0] == 1
    # should_open's decrement also committed.
    cur = await db._conn.execute(
        "SELECT parole_trades_remaining FROM combo_performance "
        "WHERE combo_key = 'chain_combo' AND window = '30d'"
    )
    assert (await cur.fetchone())[0] == 2
    await db.close()


async def test_suppression_vs_dex_serialize(tmp_path, settings_factory):
    """Same shape as the chain-tracker case, but against the DEX discovery writer.

    A DEX-style transaction is held open; should_open AND the real
    ``record_pool_discovery`` both attempt concurrently. All three writes commit
    and should_open never hits SUPP_DB_OP.
    """
    db = Database(tmp_path / "f2_dex.db")
    await db.initialize()
    db._txn_lock = _CountingLock()
    lock = db._txn_lock
    s = settings_factory()
    await _seed_open_parole_combo(db, "dex_combo", remaining=3)

    txn_open = asyncio.Event()
    may_commit = asyncio.Event()

    async def foreign_dex_writer() -> None:
        async with db.transaction() as conn:
            await _insert_dex_pool(conn, "0xforeign_pool")
            txn_open.set()
            await may_commit.wait()

    foreign_task = asyncio.create_task(foreign_dex_writer())
    await txn_open.wait()
    assert lock.locked()
    assert lock.acquire_calls == 1

    with capture_logs() as cap:
        should_task = asyncio.create_task(
            suppression.should_open(db, "dex_combo", settings=s)
        )
        # The migrated record_pool_discovery routes through db.transaction(),
        # so it too must serialize behind the open foreign transaction.
        dex_task = asyncio.create_task(
            db.record_pool_discovery(
                network="base",
                pool_address="0xreal_pool",
                base_token_address="0xreal",
                base_token_symbol="BAR",
                quote_token_symbol="WETH",
                pool_created_at=None,
                fdv_usd=2000.0,
                liquidity_usd=600.0,
                volume_h1_usd=200.0,
            )
        )
        # Both attemptors have reached the lock (foreign=1, +2 = 3).
        await _yield_until(lambda: lock.acquire_calls >= 3)
        assert not should_task.done()
        assert not dex_task.done()
        may_commit.set()
        await foreign_task
        allow, reason = await should_task
        dex_inserted = await dex_task

    assert (allow, reason) == (True, "parole_retest")
    assert dex_inserted is True
    assert not any(e.get("err_id") == "SUPP_DB_OP" for e in cap)

    # Foreign DEX write committed (not rolled back).
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM dex_pool_discoveries WHERE pool_address = ?",
        ("0xforeign_pool",),
    )
    assert (await cur.fetchone())[0] == 1
    # Real record_pool_discovery write committed.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM dex_pool_discoveries WHERE pool_address = ?",
        ("0xreal_pool",),
    )
    assert (await cur.fetchone())[0] == 1
    # should_open decrement committed.
    cur = await db._conn.execute(
        "SELECT parole_trades_remaining FROM combo_performance "
        "WHERE combo_key = 'dex_combo' AND window = '30d'"
    )
    assert (await cur.fetchone())[0] == 2
    await db.close()


async def test_transaction_manager_serializes_concurrent_writers(tmp_path):
    """Regression: the old failure mode — two unlocked concurrent BEGINs on the
    shared connection — is structurally impossible via db.transaction().

    While one transaction is open, a second db.transaction() cannot start a
    nested BEGIN; it is made to WAIT on the shared lock, and both commit with no
    "cannot start a transaction within a transaction" error.
    """
    db = Database(tmp_path / "f2_reg.db")
    await db.initialize()
    db._txn_lock = _CountingLock()
    lock = db._txn_lock

    first_open = asyncio.Event()
    first_may_finish = asyncio.Event()
    errors: list[tuple[str, BaseException]] = []

    async def first_writer() -> None:
        try:
            async with db.transaction() as conn:
                await _insert_signal_event(conn, "W1")
                first_open.set()
                await first_may_finish.wait()
        except BaseException as exc:  # noqa: BLE001 - test records any failure
            errors.append(("first", exc))

    async def second_writer() -> None:
        try:
            async with db.transaction() as conn:
                await _insert_signal_event(conn, "W2")
        except BaseException as exc:  # noqa: BLE001 - test records any failure
            errors.append(("second", exc))

    t1 = asyncio.create_task(first_writer())
    await first_open.wait()
    assert lock.acquire_calls == 1

    t2 = asyncio.create_task(second_writer())
    # Second writer provably blocked on the lock (cannot nest a BEGIN).
    await _yield_until(lambda: lock.acquire_calls >= 2)
    assert not t2.done()

    first_may_finish.set()
    await asyncio.gather(t1, t2)

    assert errors == [], f"transaction manager raised under contention: {errors}"
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE token_id IN ('W1', 'W2')"
    )
    assert (await cur.fetchone())[0] == 2
    await db.close()


def test_f2_writers_route_through_transaction_manager():
    """Structural regression guard: every migrated writer routes its transaction
    through the common ``transaction()`` manager, and no longer issues a bare,
    unlocked ``BEGIN`` on the shared connection.
    """
    should_src = inspect.getsource(suppression.should_open)
    assert "db.transaction(" in should_src
    # The manual BEGIN/ROLLBACK on the shared connection is gone.
    assert 'execute("BEGIN IMMEDIATE")' not in should_src
    assert 'execute("ROLLBACK")' not in should_src

    check_src = inspect.getsource(tracker.check_chains)
    assert "db.transaction(" in check_src
    assert 'execute("BEGIN")' not in check_src

    hydrate_src = inspect.getsource(tracker.update_chain_outcomes)
    assert "db.transaction(" in hydrate_src

    refresh_src = inspect.getsource(combo_refresh.refresh_combo)
    assert "db.transaction(" in refresh_src

    dex_src = inspect.getsource(Database.record_pool_discovery)
    assert "self.transaction(" in dex_src

    # Additional unlocked-BEGIN writers found during the exhaustive audit.
    suspend_src = inspect.getsource(auto_suspend.maybe_suspend_signals)
    assert "db.transaction(" in suspend_src
    assert 'execute("BEGIN EXCLUSIVE")' not in suspend_src

    calibrate_src = inspect.getsource(calibrate.apply_diffs)
    assert "db.transaction(" in calibrate_src
    assert 'execute("BEGIN EXCLUSIVE")' not in calibrate_src

    revive_src = inspect.getsource(Database.revive_signal_with_baseline)
    assert "self.transaction(" in revive_src
    assert 'execute("BEGIN EXCLUSIVE")' not in revive_src


def _begin_execute_sites() -> list[tuple[str, str, bool]]:
    """AST-scan scout/ for every ``.execute("BEGIN...")`` call.

    Returns ``(module_relpath, enclosing_function, holds_txn_lock)`` per site.
    """
    scout_root = pathlib.Path(__file__).resolve().parents[1] / "scout"
    sites: list[tuple[str, str, bool]] = []
    for path in scout_root.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        rel = path.relative_to(scout_root.parent).as_posix()

        class _Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.stack: list[ast.AST] = []

            def visit_FunctionDef(self, node):
                self._fn(node)

            def visit_AsyncFunctionDef(self, node):
                self._fn(node)

            def _fn(self, node):
                self.stack.append(node)
                for child in ast.iter_child_nodes(node):
                    self.visit(child)
                self.stack.pop()

            def visit_Call(self, node):
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr == "execute" and node.args:
                    arg0 = node.args[0]
                    if (
                        isinstance(arg0, ast.Constant)
                        and isinstance(arg0.value, str)
                        and arg0.value.strip().upper().startswith("BEGIN")
                    ):
                        enclosing = self.stack[-1] if self.stack else None
                        fn_src = (
                            ast.get_source_segment(src, enclosing) if enclosing else ""
                        )
                        sites.append(
                            (
                                rel,
                                enclosing.name if enclosing else "<module>",
                                "_txn_lock" in (fn_src or ""),
                            )
                        )
                for child in ast.iter_child_nodes(node):
                    self.visit(child)

        _Visitor().visit(tree)
    return sites


def test_no_unmanaged_begin_on_shared_connection():
    """Static enforcement (F2 change #1): every explicit ``BEGIN`` on the shared
    connection must be serialized — either issued by the ``transaction()``
    manager, by an init-time DDL migration (``_migrate_*`` / ``_create_*``, which
    run serially before any concurrency), or inside a function that holds
    ``_txn_lock``. A NEW runtime writer opening an unlocked ``BEGIN`` (the exact
    F2 regression shape) fails this test.
    """
    sites = _begin_execute_sites()
    # Sanity: the scan actually finds the manager + migrations (guards against a
    # broken scanner silently passing).
    assert any(
        f == "transaction" for _, f, _ in sites
    ), "scanner found no manager BEGIN"

    def allowed(func: str, holds_lock: bool) -> bool:
        return (
            func == "transaction"
            or func.startswith("_migrate")
            or func.startswith("_create")
            or holds_lock
        )

    violations = [(m, f) for (m, f, lock) in sites if not allowed(f, lock)]
    assert violations == [], (
        "Unmanaged BEGIN on the shared connection (route it through "
        f"db.transaction() or hold _txn_lock): {violations}"
    )


async def test_manager_failed_begin_does_not_rollback(tmp_path):
    """Owner-scoped rollback (F2 change #2): if BEGIN itself fails, the manager
    must NOT issue a ROLLBACK — it never opened a transaction, so a rollback
    would destroy another writer's in-flight work.
    """
    db = Database(tmp_path / "f2_failbegin.db")
    await db.initialize()

    rollback_calls: list[int] = []
    real_rollback = db._conn.rollback

    async def spy_rollback():
        rollback_calls.append(1)
        return await real_rollback()

    real_execute = db._conn.execute

    async def failing_execute(sql, *a, **k):
        if isinstance(sql, str) and sql.strip().upper().startswith("BEGIN"):
            raise aiosqlite.OperationalError(
                "cannot start a transaction within a transaction"
            )
        return await real_execute(sql, *a, **k)

    db._conn.execute = failing_execute
    db._conn.rollback = spy_rollback

    with pytest.raises(aiosqlite.OperationalError):
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO signal_events VALUES (1)")  # unreached

    assert rollback_calls == [], "failed BEGIN must not trigger a ROLLBACK"
    assert not db._txn_lock.locked(), "lock must be released after a failed BEGIN"

    # Restore + confirm the connection is still usable.
    db._conn.execute = real_execute
    db._conn.rollback = real_rollback
    await db.close()


async def test_manager_rollback_scoped_to_own_transaction(tmp_path):
    """A caller whose body raises rolls back ONLY its own writes; a previously
    committed writer's row is untouched.
    """
    db = Database(tmp_path / "f2_scope.db")
    await db.initialize()

    async with db.transaction() as conn:
        await _insert_signal_event(conn, "COMMITTED")

    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            await _insert_signal_event(conn, "DOOMED")
            raise RuntimeError("boom")

    cur = await db._conn.execute("SELECT token_id FROM signal_events ORDER BY token_id")
    rows = [r[0] for r in await cur.fetchall()]
    assert rows == ["COMMITTED"], f"rollback leaked/over-reached: {rows}"
    assert not db._txn_lock.locked()
    await db.close()


async def test_manager_releases_lock_on_cancellation(tmp_path):
    """Cancellation safety (F2 change #3): cancelling a task inside the manager
    releases the lock (never stranded) and rolls back the owner's transaction.
    """
    db = Database(tmp_path / "f2_cancel.db")
    await db.initialize()

    started = asyncio.Event()
    parked = asyncio.Event()  # never set — parks the worker until cancelled

    async def worker():
        async with db.transaction() as conn:
            await _insert_signal_event(conn, "CANCELLED")
            started.set()
            await parked.wait()

    task = asyncio.create_task(worker())
    await started.wait()
    assert db._txn_lock.locked()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Lock released despite cancellation; transaction rolled back by owner path.
    assert not db._txn_lock.locked(), "cancellation stranded the transaction lock"
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE token_id = 'CANCELLED'"
    )
    assert (await cur.fetchone())[0] == 0, "cancelled writer's work was not rolled back"

    # A subsequent writer can still acquire + commit — the lock is not stranded.
    async with db.transaction() as conn:
        await _insert_signal_event(conn, "AFTER")
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE token_id = 'AFTER'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_manager_rejects_nested_transaction(tmp_path):
    """Nested-transaction behavior is explicit (F2 change #4): re-entry on the
    same task raises NestedTransactionError rather than deadlocking. The outer
    transaction is rolled back by its owner path and the lock is released.
    """
    db = Database(tmp_path / "f2_nested.db")
    await db.initialize()

    with pytest.raises(NestedTransactionError):
        async with db.transaction() as conn:
            await _insert_signal_event(conn, "OUTER")
            async with db.transaction():  # same task -> reject, do not deadlock
                pass

    # Outer rolled back (the nested raise propagated through it); lock free.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE token_id = 'OUTER'"
    )
    assert (await cur.fetchone())[0] == 0
    assert not db._txn_lock.locked()

    # Manager is reusable after a rejected nesting.
    async with db.transaction() as conn:
        await _insert_signal_event(conn, "OK")
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE token_id = 'OK'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


def test_log_processors_emit_exception_info():
    """The pipeline's structlog processor chain must carry the exception
    text/traceback into the JSON line for log.exception() calls (F2 change #3).
    """
    import structlog

    from scout.main import LOG_PROCESSORS

    buf = io.StringIO()
    logger = structlog.wrap_logger(
        structlog.PrintLogger(file=buf),
        processors=LOG_PROCESSORS,
    )

    try:
        raise ValueError("f2-boom-marker")
    except ValueError:
        logger.exception("suppression_db_operational_error", err_id="SUPP_DB_OP")

    out = buf.getvalue()
    assert "f2-boom-marker" in out
    assert "ValueError" in out
    assert "Traceback" in out

    payload = json.loads(out.strip())
    # JSON shape otherwise unchanged: normal keys still present.
    assert payload["event"] == "suppression_db_operational_error"
    assert payload["err_id"] == "SUPP_DB_OP"
    assert payload["level"] == "error"
    # format_exc_info populated the exception field with the traceback text.
    assert "exception" in payload
    assert "f2-boom-marker" in payload["exception"]
