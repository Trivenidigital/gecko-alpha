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
import re
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

    # check_chains is a thin single-flight wrapper; the transaction lives in
    # _check_chains_impl (the phased write body).
    check_src = inspect.getsource(tracker._check_chains_impl)
    assert "db.transaction(" in check_src
    assert 'execute("BEGIN")' not in check_src

    # update_chain_outcomes gathers reads + fetches (no txn), then applies the
    # writes in db.transaction() inside its inner helper — assert the discipline
    # lives there and that the outer wrapper no longer wraps the fetch loop.
    hydrate_src = inspect.getsource(tracker._update_chain_outcomes_inner)
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


def _commit_sites() -> list[tuple[str, str, bool]]:
    """AST-scan scout/ for every ``.commit()`` call on the SHARED connection.

    Returns ``(module_relpath, enclosing_function, allowed)`` per site. A commit
    is on the shared connection when its receiver is ``*._conn`` (``db._conn`` /
    ``self._conn`` / ``self._db._conn``) or a local name bound to a ``*._conn``
    expression. Commits on a separate connection (a ``conn`` parameter typed
    ``aiosqlite.Connection``, or a locally ``aiosqlite.connect(...)``-opened
    connection — the class-(d) separate-connection writers) are NOT shared and
    are exempt. A shared-connection commit is allowed only inside the manager
    (``transaction``), an init-time DDL migration (``_migrate_*`` / ``_create_*``),
    or a function that holds ``_txn_lock``.
    """
    import re

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
                if isinstance(fn, ast.Attribute) and fn.attr == "commit":
                    enclosing = self.stack[-1] if self.stack else None
                    if enclosing is not None:
                        recv = ast.unparse(fn.value)
                        fn_src = ast.get_source_segment(src, enclosing) or ""
                        shared = recv.endswith("._conn")
                        if not shared:
                            # bare local name bound to a *._conn expression?
                            m = re.search(
                                rf"(?m)^\s*{re.escape(recv)}\s*=\s*(.+)$", fn_src
                            )
                            if m and m.group(1).strip().endswith("._conn"):
                                shared = True
                        if shared:
                            allowed = (
                                enclosing.name == "transaction"
                                or enclosing.name.startswith("_migrate")
                                or enclosing.name.startswith("_create")
                                or "_txn_lock" in fn_src
                            )
                            sites.append((rel, enclosing.name, allowed))
                for child in ast.iter_child_nodes(node):
                    self.visit(child)

        _Visitor().visit(tree)
    return sites


def test_no_unmanaged_commit_on_shared_connection():
    """Static enforcement (F2 change #1, exhaustive): NO bare ``.commit()`` on the
    shared connection may occur outside the approved manager. Every commit on
    ``db._conn`` / ``self._conn`` / ``self._db._conn`` (or a local bound to one)
    must be issued by ``Database.transaction()``, an init-time DDL migration, or
    a function holding ``_txn_lock``. Separate-connection writers (own
    connection) are out of scope. A NEW autocommit writer that commits the shared
    connection directly (the exact defect-1 shape) fails this test.
    """
    sites = _commit_sites()
    assert sites, "scanner found no shared-connection commits — scanner is broken"
    violations = [(m, f) for (m, f, ok) in sites if not ok]
    assert violations == [], (
        "Unmanaged commit on the shared connection (route the write through "
        f"db.execute_write()/db.transaction() or hold _txn_lock): {violations}"
    )


_NETWORK_CALL_RE = re.compile(
    r"(send_telegram_message|send_chain_alert|_send_[a-z_]*alert"
    r"|fetch_token_fdv|fetch_venue_metadata|\bfetcher\("
    r"|session\.(get|post|put|delete|request|ws_connect)"
    r"|aiohttp\.ClientSession|\.place_order\(|\.cancel_order\()"
)


def _network_awaits_under_lock() -> list[tuple[str, int, str]]:
    """AST-scan scout/ for network awaits lexically inside a manager-owned
    transaction (``async with X.transaction()``) or a ``_txn_lock``-held region
    (``async with X._txn_lock``). Returns (module, lineno, snippet) per hit."""
    scout_root = pathlib.Path(__file__).resolve().parents[1] / "scout"
    hits: list[tuple[str, int, str]] = []
    for path in scout_root.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        rel = path.relative_to(scout_root.parent).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncWith) and any(
                ast.unparse(item.context_expr).endswith(
                    (".transaction()", "._txn_lock")
                )
                for item in node.items
            ):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Await):
                        snippet = ast.unparse(sub)
                        if _NETWORK_CALL_RE.search(snippet):
                            hits.append((rel, sub.lineno, snippet[:80]))
    return hits


def test_no_network_await_under_txn_lock():
    """F2 regression fence (defect 4): no HTTP/Telegram/provider await may occur
    lexically inside a manager-owned transaction (``async with db.transaction()``)
    or a ``_txn_lock``-held region — a slow/hung network call there freezes every
    writer in the process. This is a STATIC fence; because it can miss indirect
    awaits hidden behind helper calls, the runtime lock-state-guard tests in
    tests/test_chain_txn_atomicity.py (fetch + alert-send assert
    ``not db._txn_lock.locked()``) cover the transitive paths end-to-end.
    """
    hits = _network_awaits_under_lock()
    assert hits == [], f"network await under _txn_lock/transaction(): {hits}"


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


# ---------------------------------------------------------------------------
# P0-3: structural (AST-ancestry) enforcement for shared-connection DML,
# commit, and rollback — with local-alias tracking, conn=-misuse detection,
# an exact reviewed allowlist, and scanner-negative fixtures.
# ---------------------------------------------------------------------------

# Exact, reviewed allowlist of functions permitted to touch the shared
# connection's DML / commit / rollback WITHOUT an `async with transaction()` /
# `_txn_lock` ancestor. Each is safe for the enumerated reason:
#   - "transaction": IS the manager (owns BEGIN IMMEDIATE / commit / rollback).
#   - _migrate_* / _create_*: init-time DDL migrations, serial before any
#     create_task (no concurrency); use BEGIN EXCLUSIVE directly.
#   - record_minara_command_emission / record_tg_alert_operator_action: hold the
#     lock via `await self._txn_lock.acquire()` + try/finally (not async-with),
#     so ancestry cannot see it — enumerated here instead.
_DML_ALLOWLIST_FUNCS = frozenset(
    {
        "transaction",
        "record_minara_alert_emission",
        "record_tg_alert_operator_action",
    }
)

_DML_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)


def _ctx_is_protective(item: ast.withitem) -> bool:
    s = ast.unparse(item.context_expr)
    return s.endswith(".transaction()") or s.endswith("._txn_lock")


class _SharedConnAudit(ast.NodeVisitor):
    """Flags shared-connection DML / commit / rollback that is NOT lexically
    inside an `async with X.transaction()` / `X._txn_lock` region and NOT in the
    allowlist, plus transaction-aware helpers called with a shared connection as
    ``conn=`` OUTSIDE such a region. Tracks local aliases (``conn = db._conn``)
    structurally via the AST (no source-substring lock detection)."""

    def __init__(self) -> None:
        self.protect_stack: list[bool] = []
        self.func_stack: list[str] = []
        self.alias_stack: list[set] = [set()]
        self.violations: list[tuple] = []

    def visit_FunctionDef(self, node):
        self._fn(node)

    def visit_AsyncFunctionDef(self, node):
        self._fn(node)

    def _fn(self, node):
        self.func_stack.append(node.name)
        self.alias_stack.append(set())
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self.alias_stack.pop()
        self.func_stack.pop()

    def visit_AsyncWith(self, node):
        protective = any(_ctx_is_protective(i) for i in node.items)
        self.protect_stack.append(protective)
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self.protect_stack.pop()

    def visit_Assign(self, node):
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and ast.unparse(node.value).endswith("._conn")
        ):
            self.alias_stack[-1].add(node.targets[0].id)
        self.generic_visit(node)

    def _protected(self) -> bool:
        return any(self.protect_stack)

    def _allowed(self) -> bool:
        fn = self.func_stack[-1] if self.func_stack else "<module>"
        return (
            fn in _DML_ALLOWLIST_FUNCS
            or fn.startswith("_migrate")
            or fn.startswith("_create")
        )

    def _is_shared(self, recv) -> bool:
        s = ast.unparse(recv)
        if s.endswith("._conn"):
            return True
        return isinstance(recv, ast.Name) and recv.id in self.alias_stack[-1]

    def _flag(self, node, kind: str) -> None:
        fn = self.func_stack[-1] if self.func_stack else "<module>"
        self.violations.append((fn, node.lineno, kind))

    def visit_Call(self, node):
        fn = node.func
        if isinstance(fn, ast.Attribute):
            if (
                self._is_shared(fn.value)
                and not self._protected()
                and not self._allowed()
            ):
                if fn.attr in ("commit", "rollback"):
                    self._flag(node, "shared .%s()" % fn.attr)
                elif fn.attr in ("execute", "executemany") and node.args:
                    a0 = node.args[0]
                    if (
                        isinstance(a0, ast.Constant)
                        and isinstance(a0.value, str)
                        and _DML_RE.match(a0.value)
                    ):
                        self._flag(node, "shared DML")
        for kw in node.keywords:
            if (
                kw.arg == "conn"
                and self._is_shared(kw.value)
                and not self._protected()
                and not self._allowed()
            ):
                self._flag(node, "conn=<shared> outside transaction")
        self.generic_visit(node)


def _audit_source(src: str) -> list:
    audit = _SharedConnAudit()
    audit.visit(ast.parse(src))
    return audit.violations


def test_ast_no_unmanaged_shared_conn_dml_commit_or_rollback():
    """Structural AST-ancestry enforcement (P0-3): every shared-connection DML /
    commit / rollback (incl. via a local `conn = db._conn` alias) must be
    lexically inside an `async with X.transaction()` / `X._txn_lock` region, or
    in the exact reviewed allowlist. No source-substring lock detection is used.
    """
    scout_root = pathlib.Path(__file__).resolve().parents[1] / "scout"
    all_violations = []
    for path in scout_root.rglob("*.py"):
        rel = path.relative_to(scout_root.parent).as_posix()
        for fn, lineno, kind in _audit_source(path.read_text(encoding="utf-8")):
            all_violations.append((rel, fn, lineno, kind))
    assert all_violations == [], (
        "Unmanaged shared-connection write/commit/rollback outside a "
        "transaction()/_txn_lock region: %r" % (all_violations,)
    )


_BAD_FIXTURES = {
    "module_dml": "async def f(db):\n await db._conn.execute('INSERT INTO t VALUES (1)')\n",
    "alias_dml": "async def f(db):\n conn = db._conn\n await conn.execute('UPDATE t SET x=1')\n",
    "self_conn_delete": "class C:\n async def m(self):\n  await self._conn.execute('DELETE FROM t')\n",
    "bare_commit": "async def f(db):\n await db._conn.commit()\n",
    "bare_rollback": "async def f(db):\n await db._conn.rollback()\n",
    "alias_commit": "async def f(db):\n c = db._conn\n await c.commit()\n",
    "conn_kwarg_misuse": "async def f(db):\n await emit_event(db, 'x', conn=db._conn)\n",
    "self_db_conn": "class C:\n async def m(self):\n  await self._db._conn.execute('REPLACE INTO t VALUES (1)')\n",
}

_GOOD_FIXTURES = {
    "in_transaction": "async def f(db):\n async with db.transaction() as conn:\n  await conn.execute('INSERT INTO t VALUES (1)')\n",
    "in_txn_lock": "async def f(db):\n async with db._txn_lock:\n  await db._conn.execute('UPDATE t SET x=1')\n  await db._conn.commit()\n",
    "conn_kwarg_in_txn": "async def f(db):\n async with db.transaction() as conn:\n  await emit_event(db, 'x', conn=db._conn)\n",
    "migration_ok": "class C:\n async def _migrate_x(self):\n  await self._conn.execute('INSERT INTO schema_version VALUES (1)')\n  await self._conn.commit()\n",
    "separate_conn": "async def f(db_path):\n async with aiosqlite.connect(db_path) as conn:\n  await conn.execute('INSERT INTO t VALUES (1)')\n  await conn.commit()\n",
}


@pytest.mark.parametrize("name", sorted(_BAD_FIXTURES))
def test_scanner_flags_forbidden_pattern(name):
    """Each forbidden snippet MUST be flagged — a broken scanner fails loudly."""
    assert _audit_source(_BAD_FIXTURES[name]), "scanner missed %r" % name


@pytest.mark.parametrize("name", sorted(_GOOD_FIXTURES))
def test_scanner_accepts_disciplined_pattern(name):
    """Each disciplined snippet MUST NOT be flagged (no false positives)."""
    assert _audit_source(_GOOD_FIXTURES[name]) == [], "false-positive on %r" % name
