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
# P0-4: ONE structural (AST-ancestry) auditor for EVERY shared-connection
# transaction-control (BEGIN/COMMIT/ROLLBACK) statement AND DML statement.
#
# There is NO source-substring `_txn_lock` allowance anywhere (the old
# explicit-BEGIN / commit substring scanners are retired and folded into this
# one auditor). A region is protective ONLY when it is structurally an
# `async with <root>.transaction()` / `async with <root>._txn_lock` whose
# subject ROOT matches the receiver root of the shared write — an unrelated
# object whose attribute chain merely ends in `.transaction()` does NOT protect
# a `db._conn` write, and a function that merely MENTIONS `_txn_lock` (in a
# string / comment / unrelated name) without structurally holding it is NOT
# protected. The auditor also:
#   - resolves static f-string DML (JoinedStr with a constant leading fragment),
#   - resolves SQL-stored-in-a-variable DML (str assignments in function scope),
#   - tracks alias-of-alias chains (conn2 = conn; conn = db._conn),
#   - classifies CTE-prefixed writes (WITH ... UPDATE/INSERT/DELETE/REPLACE) as
#     DML while leaving pure `WITH ... SELECT` reads SAFE,
#   - flags shared-connection executescript() (arbitrary multi-statement SQL),
#   - scopes the allowlist by EXACT (repo path, qualified Class.method),
#   - treats UNKNOWN / dynamic SQL on the shared connection outside a protective
#     region as a violation unless the enclosing function is exactly allowlisted.
# ---------------------------------------------------------------------------

# Exact, reviewed allowlist of functions permitted to touch the shared
# connection's transaction-control / DML WITHOUT an `async with transaction()` /
# `_txn_lock` ancestor. Scoped by EXACT (repo-relative path, qualified
# `Class.method`) — an unqualified name match in some other module, or a
# `_migrate_*`/`_create_*` function anywhere outside db.py's Database class, is
# NOT exempt. Each entry is safe for the enumerated reason:
#   - Database.transaction: IS the manager (owns BEGIN IMMEDIATE / commit /
#     rollback).
#   - Database.record_minara_alert_emission /
#     Database.record_tg_alert_operator_action: hold the lock via
#     `await self._txn_lock.acquire()` + try/finally (not async-with), so
#     ancestry cannot see it — enumerated here instead.
#   - Database.resolve_delivery_unknown_as_retryable: operator-only, audited
#     resolution of a delivery_unknown row into unknown_resolved_retryable —
#     holds _txn_lock via acquire()+try/finally (not async-with).
# The init-time DDL migrations / table creators (serial before any create_task,
# no concurrency; BEGIN EXCLUSIVE directly) are exempt via EXACT enumerated
# entries in `_MIGRATION_ALLOWLIST` — there is NO prefix/wildcard/directory/
# substring exemption anywhere (product-owner ruling); a new migration without an
# exact entry FAILS the scan.
_SCOPED_ALLOWLIST = frozenset(
    {
        ("scout/db.py", "Database.transaction"),
        ("scout/db.py", "Database.record_minara_alert_emission"),
        ("scout/db.py", "Database.record_tg_alert_operator_action"),
        ("scout/db.py", "Database.resolve_delivery_unknown_as_retryable"),
    }
)

# RETAINED PRE-EXISTING MANUAL-LOCK PATTERN — ROLLBACK-PARITY DEBT.
# Each entry is an ordinary runtime writer that serializes with a BARE
# ``async with <db>._txn_lock:`` + direct ``_conn`` DML + bare ``commit()`` (no
# transaction manager). After FU-1 (rev7) a bare lock is NOT protective, so these
# would trip the structural guard. They are enumerated here — NOT migrated in this
# narrow head — and surfaced for a reviewer disposition (migrate to
# ``db.transaction()`` in a follow-up PR, vs accept the manual-lock pattern). The
# TWO ruled FU-1 paths (tg_alert_dispatch.notify_paper_trade_opened's claim block
# and tg_alert_dispatch._log_outcome) are deliberately ABSENT — they are migrated
# to the manager in this head and must never be allowlisted.
# Launch-criticality (rev7 product-owner ruling): entries marked
# ``# LAUNCH-CRITICAL`` are directly on a live-money path (live order submission,
# balance/fill reconciliation, kill-switch enforcement, duplicate prevention, or
# exposure control). They are POTENTIALLY LAUNCH-CRITICAL rollback-parity debt and
# may need migration to ``db.transaction()`` in a bounded follow-up BEFORE live
# enablement (owner's supervised-live milestone: Kraken Aug 6). Classification
# ONLY — none are migrated in rev7. Unmarked entries are off the live-money path.
_MANUAL_LOCK_ALLOWLIST = frozenset(
    {
        ("scout/db.py", "Database.insert_conviction_watchlist_run"),
        ("scout/db.py", "Database.insert_conviction_watchlist_snapshot"),
        ("scout/db.py", "Database.insert_detection_decision_receipt"),
        ("scout/db.py", "Database.run_incremental_vacuum"),
        # LAUNCH-CRITICAL — live order submission / slippage record (Binance adapter)
        ("scout/live/binance_adapter.py", "BinanceSpotAdapter._write_slippage_bps"),
        ("scout/live/binance_adapter.py", "BinanceSpotAdapter.place_order_request"),
        # LAUNCH-CRITICAL — consecutive-correction / exposure-control counters
        ("scout/live/correction_counter.py", "increment_consecutive"),
        ("scout/live/correction_counter.py", "reset_on_correction"),
        # LAUNCH-CRITICAL — live dispatch / duplicate-prevention on paper->live open
        ("scout/live/engine.py", "LiveEngine._dispatch_live"),
        ("scout/live/engine.py", "LiveEngine.on_paper_trade_opened"),
        # LAUNCH-CRITICAL — kill-switch enforcement (trigger/clear)
        ("scout/live/kill_switch.py", "KillSwitch.clear"),
        ("scout/live/kill_switch.py", "KillSwitch.trigger"),
        # LAUNCH-CRITICAL — live position close / exposure control
        ("scout/live/live_evaluator.py", "_close_live_trade"),
        ("scout/live/live_evaluator.py", "_mark_exit_needs_review"),
        ("scout/live/metrics.py", "inc"),
        # LAUNCH-CRITICAL — balance/fill reconciliation (crossed rows / recovered fills)
        ("scout/live/reconciliation.py", "_close_crossed_row"),
        ("scout/live/reconciliation.py", "_flag_live_review"),
        ("scout/live/reconciliation.py", "_persist_recovered_fill"),
        ("scout/live/resolver.py", "VenueResolver._cache_put_negative"),
        ("scout/live/resolver.py", "VenueResolver._cache_put_positive"),
        ("scout/live/shadow_evaluator.py", "_bump_review"),
        ("scout/live/shadow_evaluator.py", "_close_shadow_trade"),
        ("scout/main.py", "_maybe_announce_m1_5c"),
        ("scout/main.py", "_maybe_announce_tg_alerts"),
        ("scout/outcome_ledger.py", "purge_expired_enrollments"),
        ("scout/social/telegram/listener.py", "_persist_message_with_watermark"),
        ("scout/trading/detection_alert.py", "_log_detection_outcome"),
        ("scout/trading/receipt_archive.py", "ReceiptArchiver._delete_hot_rows"),
        ("scout/trading/receipt_archive.py", "ReceiptArchiver._insert_manifest"),
        ("scout/trading/receipt_archive.py", "ReceiptArchiver._mark_offhost_confirmed"),
    }
)

# Init-time DDL migrations / table creators in db.py's Database class. They run
# SERIALLY at initialize() before any create_task (no concurrency) and use
# BEGIN EXCLUSIVE directly. EXACT (path, Class.method) enumeration — NO prefix /
# wildcard / directory / substring exemption (product-owner ruling). A NEW
# migration added without an entry here will FAIL the scan, forcing review.
_MIGRATION_ALLOWLIST = frozenset(
    {
        ("scout/db.py", "Database._create_tables"),
        ("scout/db.py", "Database._migrate_actionability_entry_snapshot_v1"),
        ("scout/db.py", "Database._migrate_autosuspend_baseline_column"),
        ("scout/db.py", "Database._migrate_bl_quote_pair_v1"),
        ("scout/db.py", "Database._migrate_bl_slow_burn_v1"),
        ("scout/db.py", "Database._migrate_chain_pattern_provenance_v1"),
        ("scout/db.py", "Database._migrate_cohort_digest_state_v1"),
        ("scout/db.py", "Database._migrate_detection_decision_receipts_v1"),
        ("scout/db.py", "Database._migrate_detection_receipt_archive_v1"),
        ("scout/db.py", "Database._migrate_dex_discovery_v1"),
        ("scout/db.py", "Database._migrate_dex_instrumentation_v1"),
        ("scout/db.py", "Database._migrate_entry_snapshot_liquidity_provenance_v1"),
        ("scout/db.py", "Database._migrate_feedback_loop_schema"),
        ("scout/db.py", "Database._migrate_gainer_acceleration_v1"),
        ("scout/db.py", "Database._migrate_high_peak_fade_columns_and_audit_table"),
        ("scout/db.py", "Database._migrate_ingest_watchdog_state_v1"),
        ("scout/db.py", "Database._migrate_ledger_enrollment_evictions_v1"),
        ("scout/db.py", "Database._migrate_liquidity_enrichment_v1"),
        ("scout/db.py", "Database._migrate_live_client_order_id"),
        ("scout/db.py", "Database._migrate_live_eligible_column"),
        ("scout/db.py", "Database._migrate_live_trades_telemetry"),
        ("scout/db.py", "Database._migrate_live_trading_schema"),
        ("scout/db.py", "Database._migrate_minara_alert_emissions_v1"),
        ("scout/db.py", "Database._migrate_moonshot_opt_out_column"),
        ("scout/db.py", "Database._migrate_moved_already_postmortems_v1"),
        ("scout/db.py", "Database._migrate_narrative_resolution_status_v1"),
        ("scout/db.py", "Database._migrate_narrative_resolver_errors_v1"),
        ("scout/db.py", "Database._migrate_narrative_scanner_v1"),
        ("scout/db.py", "Database._migrate_per_venue_services"),
        ("scout/db.py", "Database._migrate_predictions_coin_predicted_id_idx_v1"),
        ("scout/db.py", "Database._migrate_price_provenance_v1"),
        ("scout/db.py", "Database._migrate_reject_reason_extend"),
        ("scout/db.py", "Database._migrate_reject_reason_extend_v2"),
        ("scout/db.py", "Database._migrate_retire_dead_tables_v1"),
        ("scout/db.py", "Database._migrate_scanned_at_index"),
        ("scout/db.py", "Database._migrate_signal_outcome_ledger_v1"),
        ("scout/db.py", "Database._migrate_signal_params_schema"),
        ("scout/db.py", "Database._migrate_source_call_price_snapshot_runs_v1"),
        ("scout/db.py", "Database._migrate_source_call_price_snapshots_v1"),
        ("scout/db.py", "Database._migrate_source_calls_v1"),
        ("scout/db.py", "Database._migrate_symbol_upper_indexes_v1"),
        ("scout/db.py", "Database._migrate_tg_alert_eligible_v1"),
        ("scout/db.py", "Database._migrate_tg_alert_log_dedup_outcome"),
        ("scout/db.py", "Database._migrate_tg_alert_log_dispatch_lease_v1"),
        ("scout/db.py", "Database._migrate_tg_alert_log_dispatch_pending_outcome"),
        ("scout/db.py", "Database._migrate_tg_alert_log_m1_5c_outcome"),
        ("scout/db.py", "Database._migrate_tg_alert_operator_actions_v1"),
        ("scout/db.py", "Database._migrate_tg_alert_unknown_resolution_audit_v1"),
        ("scout/db.py", "Database._migrate_trade_decision_events_v1"),
    }
)

_DML_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
_TXNCTL_RE = re.compile(
    r"^\s*(BEGIN|COMMIT|ROLLBACK|END\s+TRANSACTION|SAVEPOINT|RELEASE)\b",
    re.IGNORECASE,
)
# A CTE (`WITH ...`) statement is a read ONLY if it carries no DML keyword;
# `WITH cte AS (...) UPDATE ...` is a write and must classify as DML.
_CTE_PREFIX_RE = re.compile(r"^\s*WITH\b", re.IGNORECASE)
_CTE_DML_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)


def _classify_sql(text: str) -> str:
    """Classify a concrete SQL string: DML / TXNCTL (transaction control) / SAFE
    (reads: SELECT / PRAGMA / `WITH ... SELECT` — anything not a write or txn
    statement). A `WITH`-prefixed statement carrying an INSERT/UPDATE/DELETE/
    REPLACE keyword is a CTE-prefixed write and classifies DML; a pure
    `WITH ... SELECT` stays SAFE."""
    if _DML_RE.match(text):
        return "DML"
    if _TXNCTL_RE.match(text):
        return "TXNCTL"
    if _CTE_PREFIX_RE.match(text) and _CTE_DML_RE.search(text):
        return "DML"
    return "SAFE"


class _SharedConnAudit(ast.NodeVisitor):
    """Structural auditor — see the section header. Flags shared-connection
    transaction-control / DML / commit / rollback (incl. via a `conn = db._conn`
    alias chain, an f-string with a DML leading fragment, a SQL variable, or an
    unknown/dynamic SQL expression) that is NOT structurally inside an
    `async with <root>.transaction()` / `<root>._txn_lock` region whose root
    matches the write's receiver root, and NOT in the exact allowlist."""

    def __init__(self, path: str = "<fixture>") -> None:
        # Repo-relative path of the audited file (e.g. "scout/db.py") — the
        # allowlist is scoped by (path, qualified Class.method).
        self.path = path
        self.func_stack: list[str] = []
        # Enclosing-class names (visit_ClassDef push/pop) — used to qualify a
        # method as "Class.method" for the exact scoped allowlist.
        self.class_stack: list[str] = []
        # One set of protected receiver-roots per enclosing protective async-with.
        self.protect_stack: list[set] = []
        # name -> receiver-root string for shared-conn aliases (conn = db._conn
        # -> {"conn": "db"}); alias-of-alias inherits the root.
        self.alias_stack: list[dict] = [{}]
        # name -> SQL classification ("DML"/"TXNCTL"/"SAFE"/"UNKNOWN")
        self.sqlvar_stack: list[dict] = [{}]
        self.violations: list[tuple] = []

    # -- scopes --------------------------------------------------------------
    def visit_ClassDef(self, node):
        self.class_stack.append(node.name)
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self.class_stack.pop()

    def visit_FunctionDef(self, node):
        self._fn(node)

    def visit_AsyncFunctionDef(self, node):
        self._fn(node)

    def _fn(self, node):
        self.func_stack.append(node.name)
        self.alias_stack.append(dict(self.alias_stack[-1]))
        self.sqlvar_stack.append(dict(self.sqlvar_stack[-1]))
        # A nested function body does NOT inherit the enclosing protective
        # region — its execution is deferred, so protection starts fresh.
        saved_protect = self.protect_stack
        self.protect_stack = []
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self.protect_stack = saved_protect
        self.sqlvar_stack.pop()
        self.alias_stack.pop()
        self.func_stack.pop()

    # -- protective regions --------------------------------------------------
    @staticmethod
    def _protective_root(item: ast.withitem) -> str | None:
        """Root of a structurally-approved protective context, else None. ONLY the
        manager ``<root>.transaction()`` protects ordinary runtime DML (it owns
        BEGIN IMMEDIATE + COMMIT/ROLLBACK, so a failed statement can never strand
        the shared connection). A BARE ``async with <root>._txn_lock`` is
        deliberately NOT protective (FU-1 rev7): it serializes but has no
        rollback/cleanup discipline, so ordinary DML under it must FAIL the scan
        unless the function is on the exact reviewed manual-lock allowlist."""
        ctx = item.context_expr
        if (
            isinstance(ctx, ast.Call)
            and isinstance(ctx.func, ast.Attribute)
            and ctx.func.attr == "transaction"
        ):
            return ast.unparse(ctx.func.value)
        return None

    def visit_AsyncWith(self, node):
        roots = {
            r for r in (self._protective_root(i) for i in node.items) if r is not None
        }
        self.protect_stack.append(roots)
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self.protect_stack.pop()

    # -- assignment tracking (aliases + sql vars) ----------------------------
    def visit_Assign(self, node):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            root = self._alias_root_of_value(node.value)
            if root is not None:
                self.alias_stack[-1][name] = root
            else:
                self.alias_stack[-1].pop(name, None)
            self.sqlvar_stack[-1][name] = self._classify_value(node.value)
        self.generic_visit(node)

    def _alias_root_of_value(self, val: ast.AST) -> str | None:
        s = ast.unparse(val)
        if s.endswith("._conn"):
            return s[: -len("._conn")]
        if isinstance(val, ast.Name) and val.id in self.alias_stack[-1]:
            return self.alias_stack[-1][val.id]  # alias-of-alias
        return None

    def _classify_value(self, val: ast.AST) -> str:
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            return _classify_sql(val.value)
        if isinstance(val, ast.JoinedStr):
            if (
                val.values
                and isinstance(val.values[0], ast.Constant)
                and isinstance(val.values[0].value, str)
            ):
                return _classify_sql(val.values[0].value)
            return "UNKNOWN"
        if isinstance(val, ast.Name):
            return self.sqlvar_stack[-1].get(val.id, "UNKNOWN")
        if isinstance(val, ast.BinOp):  # "INSERT ..." + x
            return self._classify_value(val.left)
        return "UNKNOWN"

    # -- shared-conn resolution ----------------------------------------------
    def _shared_root(self, recv: ast.AST) -> str | None:
        """Receiver root if ``recv`` is the shared connection (``*._conn`` or a
        tracked alias), else None."""
        s = ast.unparse(recv)
        if s.endswith("._conn"):
            return s[: -len("._conn")]
        if isinstance(recv, ast.Name) and recv.id in self.alias_stack[-1]:
            return self.alias_stack[-1][recv.id]
        return None

    def _protected(self, root: str) -> bool:
        return any(root in frame for frame in self.protect_stack)

    def _qualified_fn(self) -> str:
        """The enclosing function qualified as ``Class.method`` when inside a
        class, else the bare function name (``<module>`` at module scope)."""
        fn = self.func_stack[-1] if self.func_stack else "<module>"
        if self.class_stack:
            return "%s.%s" % (self.class_stack[-1], fn)
        return fn

    def _allowed(self) -> bool:
        # EXACT (path, qualified-function) allowlists ONLY — no prefix / wildcard /
        # directory / substring exemption anywhere (product-owner ruling). A
        # like-named function in another module/class, or a NEW migration without
        # an entry, is NOT exempt and FAILS the scan.
        key = (self.path, self._qualified_fn())
        return (
            key in _SCOPED_ALLOWLIST
            or key in _MIGRATION_ALLOWLIST
            # Retained pre-existing manual-lock writers (rollback-parity debt) —
            # enumerated + surfaced for reviewer disposition, not migrated here.
            or key in _MANUAL_LOCK_ALLOWLIST
        )

    def _flag(self, node, kind: str) -> None:
        self.violations.append((self._qualified_fn(), node.lineno, kind))

    def visit_Call(self, node):
        fn = node.func
        if isinstance(fn, ast.Attribute):
            root = self._shared_root(fn.value)
            if root is not None and not self._protected(root) and not self._allowed():
                if fn.attr in ("commit", "rollback"):
                    self._flag(node, "shared .%s()" % fn.attr)
                elif fn.attr == "executescript":
                    # executescript runs arbitrary multi-statement SQL; its
                    # argument is never statically classifiable, so an
                    # unprotected/unallowlisted shared-conn call always flags.
                    self._flag(node, "shared executescript")
                elif fn.attr in ("execute", "executemany"):
                    cls = self._classify_value(node.args[0]) if node.args else "UNKNOWN"
                    if cls in ("DML", "TXNCTL", "UNKNOWN"):
                        self._flag(node, "shared %s" % cls.lower())
        for kw in node.keywords:
            if kw.arg == "conn":
                root = self._shared_root(kw.value)
                if (
                    root is not None
                    and not self._protected(root)
                    and not self._allowed()
                ):
                    self._flag(node, "conn=<shared> outside transaction")
        self.generic_visit(node)


def _audit_source(src: str, path: str = "<fixture>") -> list:
    audit = _SharedConnAudit(path=path)
    audit.visit(ast.parse(src))
    return audit.violations


# Reviewed read-only dynamic-SQL sites: exact (repo path, qualified
# Class.method|func) pairs whose shared-connection ``execute(<dynamic>)`` is
# verified to run ONLY SELECT reads (dynamic in WHICH constant query runs, never
# in write-intent). This suppresses ONLY the ``shared unknown`` classification at
# these exact sites — a constant DML / TXNCTL / commit / rollback at the same
# site is STILL flagged, so the allowlist can never blind-spot a real write.
#   - build_prospective_watchlist (module-level function, bare name) iterates
#     ``_SURFACE_SOURCES`` (a module-level tuple of constant per-surface
#     SELECT ... MIN(detection) ... queries).
_DYNAMIC_READ_ALLOWLIST = frozenset(
    {
        ("scout/conviction/prospective.py", "build_prospective_watchlist"),
    }
)


def test_ast_no_unmanaged_shared_conn_txnctl_dml_commit_or_rollback():
    """Structural AST-ancestry enforcement (P0-4): every shared-connection
    transaction-control (BEGIN/COMMIT/ROLLBACK), DML, or commit/rollback (incl.
    via a `conn = db._conn` alias chain, an f-string DML, a SQL variable, or an
    unknown/dynamic SQL expression) must be lexically inside an
    `async with <root>.transaction()` / `<root>._txn_lock` region whose root
    matches the write's receiver, or in the exact reviewed allowlist. No
    source-substring lock detection is used anywhere."""
    scout_root = pathlib.Path(__file__).resolve().parents[1] / "scout"
    all_violations = []
    for path in scout_root.rglob("*.py"):
        rel = path.relative_to(scout_root.parent).as_posix()
        for fn, lineno, kind in _audit_source(
            path.read_text(encoding="utf-8"), path=rel
        ):
            # Reviewed read-only dynamic-SQL sites are exempt ONLY for the
            # dynamic-read ("shared unknown") kind — never for a real write.
            if kind == "shared unknown" and (rel, fn) in _DYNAMIC_READ_ALLOWLIST:
                continue
            all_violations.append((rel, fn, lineno, kind))
    assert all_violations == [], (
        "Unmanaged shared-connection txn-control/DML/commit/rollback outside a "
        "matching transaction()/_txn_lock region: %r" % (all_violations,)
    )


def test_ast_auditor_is_not_vacuously_passing():
    """Sanity: the clean result above is due to the allowlist/protection, NOT a
    scanner that never reaches shared-conn writes. With the allowlist emptied, the
    auditor MUST flag the shared-conn writes the real allowlist exempts (the
    manager's BEGIN/commit/rollback + the migration DDL) inside db.py."""

    class _NoAllowlist(_SharedConnAudit):
        def _allowed(self):  # type: ignore[override]
            return False

    scout_root = pathlib.Path(__file__).resolve().parents[1] / "scout"
    db_src = (scout_root / "db.py").read_text(encoding="utf-8")
    audit = _NoAllowlist()
    audit.visit(ast.parse(db_src))
    assert audit.violations, "auditor did not reach any shared-conn write in db.py"


def test_dynamic_read_allowlist_does_not_exempt_real_dml():
    """The reviewed dynamic-read allowlist suppresses ONLY the ``shared unknown``
    kind. A constant DML at an allowlisted-name function is still flagged — the
    allowlist can never blind-spot a real write."""
    src = "async def build_prospective_watchlist(db):\n conn = db._conn\n await conn.execute('DELETE FROM t')\n"
    viol = _audit_source(src, path="scout/conviction/prospective.py")
    assert any(k == "shared dml" for _, _, k in viol), viol


# Each forbidden snippet MUST be flagged; each disciplined snippet MUST NOT be.
# Fixtures are ``{name: (audit_path, src)}`` so each can declare the repo path it
# is audited under — the exact path+class-scoped allowlist depends on it.
_BAD_FIXTURES = {
    "module_dml": (
        "scout/x.py",
        "async def f(db):\n await db._conn.execute('INSERT INTO t VALUES (1)')\n",
    ),
    "alias_dml": (
        "scout/x.py",
        "async def f(db):\n conn = db._conn\n await conn.execute('UPDATE t SET x=1')\n",
    ),
    "self_conn_delete": (
        "scout/x.py",
        "class C:\n async def m(self):\n  await self._conn.execute('DELETE FROM t')\n",
    ),
    "bare_commit": (
        "scout/x.py",
        "async def f(db):\n await db._conn.commit()\n",
    ),
    "bare_rollback": (
        "scout/x.py",
        "async def f(db):\n await db._conn.rollback()\n",
    ),
    "alias_commit": (
        "scout/x.py",
        "async def f(db):\n c = db._conn\n await c.commit()\n",
    ),
    "conn_kwarg_misuse": (
        "scout/x.py",
        "async def f(db):\n await emit_event(db, 'x', conn=db._conn)\n",
    ),
    "self_db_conn": (
        "scout/x.py",
        "class C:\n async def m(self):\n  await self._db._conn.execute('REPLACE INTO t VALUES (1)')\n",
    ),
    "begin_on_shared": (
        "scout/x.py",
        "async def f(db):\n await db._conn.execute('BEGIN IMMEDIATE')\n",
    ),
    # P0-4 hardening — each closes a specific bypass:
    "fstring_dml": (
        "scout/x.py",
        "async def f(db, t):\n await db._conn.execute(f'INSERT INTO {t} VALUES (1)')\n",
    ),
    "sqlvar_dml": (
        "scout/x.py",
        "async def f(db):\n sql = 'DELETE FROM t'\n await db._conn.execute(sql)\n",
    ),
    "alias_of_alias": (
        "scout/x.py",
        "async def f(db):\n conn = db._conn\n conn2 = conn\n await conn2.execute('INSERT INTO t VALUES (1)')\n",
    ),
    "unrelated_txn_ctx": (
        "scout/x.py",
        "async def f(db, unrelated):\n async with unrelated.transaction():\n  await db._conn.execute('INSERT INTO t VALUES (1)')\n",
    ),
    "mentions_txn_lock_only": (
        "scout/x.py",
        "async def f(db):\n note = '_txn_lock'\n await db._conn.execute('INSERT INTO t VALUES (1)')\n",
    ),
    "dynamic_unknown_sql": (
        "scout/x.py",
        "async def f(db):\n await db._conn.execute(build_sql())\n",
    ),
    # B3.1 — SQL-form ROLLBACK on the shared connection is txn control.
    "rollback_sql": (
        "scout/x.py",
        "async def f(db):\n await db._conn.execute('ROLLBACK')\n",
    ),
    # B3.2 — CTE-prefixed DML must classify DML, not SAFE.
    "cte_update": (
        "scout/x.py",
        "async def f(db):\n await db._conn.execute('WITH x AS (SELECT 1) UPDATE t SET a=1')\n",
    ),
    "cte_delete": (
        "scout/x.py",
        "async def f(db):\n await db._conn.execute('WITH x AS (SELECT 1) DELETE FROM t')\n",
    ),
    # B3.4 — shared-conn executescript() runs arbitrary multi-statement SQL.
    "executescript_shared": (
        "scout/x.py",
        "async def f(db):\n await db._conn.executescript('UPDATE t SET a=1')\n",
    ),
    # B3.3 — exact path+class scoping: the allowlisted NAME at the WRONG path is
    # still flagged (Database.transaction is exempt ONLY in scout/db.py).
    "allowlisted_name_wrong_path": (
        "scout/other.py",
        "class Database:\n async def transaction(self):\n  await self._conn.execute('BEGIN IMMEDIATE')\n  await self._conn.execute('INSERT INTO t VALUES (1)')\n",
    ),
    # B3.3 — a _create_*/_migrate_* prefix outside db.py's Database is NOT exempt.
    "create_outside_reviewed": (
        "scout/foo.py",
        "class C:\n async def _create_thing(self):\n  await self._conn.execute('INSERT INTO t VALUES (1)')\n",
    ),
    # FU-1 (rev7) compliance: the prefix rule is GONE — a NEW _migrate_* method in
    # db.py's Database that is NOT in the exact _MIGRATION_ALLOWLIST still FLAGS.
    "unenumerated_migration_in_db_py": (
        "scout/db.py",
        "class Database:\n async def _migrate_not_enumerated(self):\n  await self._conn.execute('INSERT INTO t VALUES (1)')\n  await self._conn.commit()\n",
    ),
    # FU-1 (rev7): a BARE `async with db._txn_lock` is NOT protective — ordinary
    # runtime DML under it (in a function NOT on the manual-lock allowlist) flags.
    "bare_txn_lock_dml": (
        "scout/x.py",
        "async def f(db):\n async with db._txn_lock:\n  await db._conn.execute('UPDATE t SET x=1')\n  await db._conn.commit()\n",
    ),
    "bare_self_db_txn_lock_dml": (
        "scout/x.py",
        "class C:\n async def m(self):\n  async with self._db._txn_lock:\n   await self._db._conn.execute('UPDATE t SET x=1')\n   await self._db._conn.commit()\n",
    ),
}

_GOOD_FIXTURES = {
    "in_transaction": (
        "scout/x.py",
        "async def f(db):\n async with db.transaction() as conn:\n  await conn.execute('INSERT INTO t VALUES (1)')\n",
    ),
    "conn_kwarg_in_txn": (
        "scout/x.py",
        "async def f(db):\n async with db.transaction() as conn:\n  await emit_event(db, 'x', conn=db._conn)\n",
    ),
    # migration_ok is exempt only via an EXACT enumerated entry in
    # _MIGRATION_ALLOWLIST (no prefix rule), so it must use a real enumerated
    # migration name at scout/db.py.
    "migration_ok": (
        "scout/db.py",
        "class Database:\n async def _migrate_dex_discovery_v1(self):\n  await self._conn.execute('INSERT INTO schema_version VALUES (1)')\n  await self._conn.commit()\n",
    ),
    "separate_conn": (
        "scout/x.py",
        "async def f(db_path):\n async with aiosqlite.connect(db_path) as conn:\n  await conn.execute('INSERT INTO t VALUES (1)')\n  await conn.commit()\n",
    ),
    # P0-4 — dynamic READS on the shared conn are not writes and must NOT flag:
    "fstring_select": (
        "scout/x.py",
        "async def f(db, y):\n await db._conn.execute(f'SELECT * FROM t WHERE x={y}')\n",
    ),
    "sqlvar_select": (
        "scout/x.py",
        "async def f(db):\n sql = 'SELECT * FROM t'\n await db._conn.execute(sql)\n",
    ),
    # FU-1 (rev7): a bare-lock DML in a function ON the reviewed manual-lock
    # allowlist (retained rollback-parity debt) is exempt at its exact path+fn.
    "allowlisted_manual_lock": (
        "scout/live/metrics.py",
        "async def inc(db):\n async with db._txn_lock:\n  await db._conn.execute('UPDATE t SET x=1')\n  await db._conn.commit()\n",
    ),
    # B3.2 — a pure `WITH ... SELECT` CTE read carries no DML keyword: SAFE.
    "cte_select": (
        "scout/x.py",
        "async def f(db):\n await db._conn.execute('WITH x AS (SELECT 1) SELECT * FROM x')\n",
    ),
}


@pytest.mark.parametrize("name", sorted(_BAD_FIXTURES))
def test_scanner_flags_forbidden_pattern(name):
    """Each forbidden snippet MUST be flagged — a broken scanner fails loudly."""
    path, src = _BAD_FIXTURES[name]
    assert _audit_source(src, path=path), "scanner missed %r" % name


@pytest.mark.parametrize("name", sorted(_GOOD_FIXTURES))
def test_scanner_accepts_disciplined_pattern(name):
    """Each disciplined snippet MUST NOT be flagged (no false positives)."""
    path, src = _GOOD_FIXTURES[name]
    assert _audit_source(src, path=path) == [], "false-positive on %r" % name
