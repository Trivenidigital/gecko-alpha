"""Solana cron watchdog: read-only, and no connection leak on init failure.

The incident (2026-08-08). The `*/2` cron watchdog routed through
`Database.initialize()`, so a READ-ONLY check executed `_create_tables` plus ~40
schema migrations against a live 6.8 GB scout.db that the pipeline was
concurrently writing — 720 times a day. The watchdog log carried 74
`database is locked` failures raised out of `initialize()`.

`solana_lane.main()` calls `initialize()` OUTSIDE the try/finally that owns
`db.close()`, so each failure skipped cleanup entirely and leaked the aiosqlite
connection with its worker thread. That is a lifecycle defect on its own terms.

Separately, 35 watchdog invocations (70 processes counting shell wrappers) were
found resident, ~1.5 GB RSS on a 3.8 GB box, oldest 5.45 days. That is
CONSISTENT with the leak but not explained by it — see
`test_a_failed_init_does_not_keep_the_process_alive`, which demonstrates by
mutation that a leaked connection alone still lets the process exit on aiosqlite
0.22.1. The residency mechanism is unproven; the specimens were terminated
during containment.

Two independent fixes, tested separately:
  1. `Database.initialize()` owns its connection — any failure after connect
     closes it before re-raising. Fixes every short-lived caller, not just this
     one.
  2. The watchdog command never enters the migrating initializer at all.
"""

from __future__ import annotations

import asyncio
import subprocess
import sqlite3
import sys
import textwrap
from pathlib import Path

import aiosqlite
import pytest

from scout.db import Database

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestInitializeOwnsItsConnection:
    """*** THE LEAK PATH, CLOSED AT THE SOURCE. ***"""

    async def test_failure_after_connect_closes_the_connection(
        self, tmp_path, monkeypatch
    ):
        db = Database(str(tmp_path / "t.db"))

        boom = sqlite3.OperationalError("database is locked")

        async def _explode(self):  # noqa: ANN001
            raise boom

        # Fail in an EARLY migration — after the connection exists, before the
        # rest of the chain. That is the real shape: the production traceback
        # died in `_migrate_feedback_loop_schema`.
        monkeypatch.setattr(
            Database, "_migrate_feedback_loop_schema", _explode, raising=True
        )

        with pytest.raises(sqlite3.OperationalError) as exc:
            await db.initialize()

        # The ORIGINAL error must survive — a cleanup failure must never mask it.
        assert exc.value is boom
        assert db._conn is None, (
            "initialize() opened this connection, so it must not lose it on the "
            "way out — a caller that cannot reach db.close() has no other handle"
        )

    async def test_a_successful_initialize_still_leaves_the_connection_open(
        self, tmp_path
    ):
        """Discriminating control.

        Without this, the test above passes if `initialize()` closed the
        connection unconditionally — which would break every long-lived caller.
        """
        db = Database(str(tmp_path / "ok.db"))
        await db.initialize()
        try:
            assert db._conn is not None
            cur = await db._conn.execute("SELECT 1")
            assert (await cur.fetchone())[0] == 1
        finally:
            await db.close()

    def test_a_failed_init_does_not_keep_the_process_alive(self, tmp_path):
        """End-to-end liveness check — but NOT a regression guard. Read on.

        The production symptom was a process that never exited, so this runs a
        real subprocess whose migration raises and requires it to terminate on
        its own.

        *** WHAT THIS TEST DOES NOT PROVE. *** It passes with the leak fix
        REMOVED — verified by mutation. On the pinned aiosqlite 0.22.1,
        `Connection.__del__` calls `self.stop()`, so once the leaked connection
        becomes unreachable the worker thread is stopped by garbage collection
        and the process exits anyway.

        That is a real finding, not a weak assertion: it means "non-daemon
        worker thread keeps the interpreter alive" is NOT sufficient on its own
        to explain the 35 watchdog invocations that stayed resident for up to
        5.45 days. Something must have kept the connection REACHABLE — a
        retained traceback frame holding `main()`'s locals is the obvious
        candidate — and that part of the chain is not established.

        Kept because a future aiosqlite could drop `__del__` cleanup (it is a
        best-effort courtesy, not a contract), and because the liveness property
        is worth asserting regardless. The actual regression guard for the fix
        is `test_failure_after_connect_closes_the_connection`, which the same
        mutation does kill.
        """
        script = textwrap.dedent(
            f"""
            import asyncio, sqlite3, sys
            sys.path.insert(0, {str(REPO_ROOT)!r})
            from scout.db import Database

            async def _explode(self):
                raise sqlite3.OperationalError("database is locked")

            Database._migrate_feedback_loop_schema = _explode

            async def main():
                db = Database({str(tmp_path / "sub.db")!r})
                try:
                    await db.initialize()
                except sqlite3.OperationalError:
                    print("RAISED_AS_EXPECTED")

            asyncio.run(main())
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,  # a leaked worker thread hangs here instead
        )
        assert "RAISED_AS_EXPECTED" in proc.stdout, proc.stderr
        assert proc.returncode == 0, (
            f"process did not exit cleanly: rc={proc.returncode}\n{proc.stderr}"
        )


class TestWatchdogIsReadOnly:
    """The watchdog must not enter the migrating initializer at all."""

    def test_watchdog_command_returns_before_database_initialize(self):
        """Structural: the branch must precede the `Database(...)` construction.

        Ordering is the whole fix. A read-only path placed *after* the
        initializer would still run 40 migrations before reaching it.
        """
        src = (REPO_ROOT / "scout" / "live" / "solana_lane.py").read_text("utf-8")
        i_branch = src.index('if args.command == "watchdog":\n        return await _run_watchdog_readonly')
        i_init = src.index("await db.initialize()")
        assert i_branch < i_init, (
            "the watchdog branch must return BEFORE Database.initialize() — "
            "otherwise the migrations it exists to avoid have already run"
        )

    def test_the_readonly_path_opens_mode_ro(self):
        src = (REPO_ROOT / "scout" / "live" / "solana_lane.py").read_text("utf-8")
        fn = src[src.index("async def _run_watchdog_readonly") :][:2000]
        assert "?mode=ro" in fn and "uri=True" in fn, (
            "must open read-only so SQLite REFUSES schema writes rather than "
            "relying on us not to attempt them"
        )
        assert "Database(" not in fn, "must not construct the migrating Database"

    async def test_a_readonly_connection_refuses_writes(self, tmp_path):
        """Proves `mode=ro` is load-bearing, not decorative."""
        path = tmp_path / "ro.db"
        db = Database(str(path))
        await db.initialize()
        await db.close()

        conn = await aiosqlite.connect(f"file:{path}?mode=ro", uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError) as exc:
                await conn.execute("CREATE TABLE nope (x INTEGER)")
            assert "readonly" in str(exc.value).lower()
        finally:
            await conn.close()

    async def test_missing_solana_executions_fails_loudly(self, tmp_path):
        """It must NOT self-migrate the table it depends on.

        Quietly creating `solana_executions` would make a watchdog report
        "nothing stuck" against a table that had just been conjured empty —
        indistinguishable from a healthy lane.
        """
        path = tmp_path / "bare.db"
        conn = await aiosqlite.connect(path)
        await conn.execute("CREATE TABLE placeholder (x INTEGER)")
        await conn.commit()
        await conn.close()

        ro = await aiosqlite.connect(f"file:{path}?mode=ro", uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError) as exc:
                await ro.execute("SELECT decision_id FROM solana_executions")
            assert "no such table" in str(exc.value)
        finally:
            await ro.close()


class TestWatchdogSemanticsUnchanged:
    """Exit codes and strings must be identical to the pre-existing path."""

    def test_readonly_path_reuses_the_same_strings_and_exit_codes(self):
        src = (REPO_ROOT / "scout" / "live" / "solana_lane.py").read_text("utf-8")
        fn = src[src.index("async def _run_watchdog_readonly") :][:4000]
        assert 'print("solana lane: no stuck executions.")' in fn
        assert "SOLANA_EXECUTION_WATCHDOG_ENABLED is False" in fn
        assert "return EXIT_OK" in fn and "return EXIT_BLOCKED" in fn
        assert "NEVER rebuild" in fn, (
            "the operator instruction for a stuck execution must not be lost"
        )


class TestReadOnlyPathActuallyRuns:
    """*** THE TESTS THAT WOULD HAVE CAUGHT THE MISSING IMPORT. ***

    Every other test in this file inspects `_run_watchdog_readonly` as SOURCE
    TEXT or exercises a hand-rolled `mode=ro` connection alongside it. None of
    them ever called it. `solana_lane.py` imports `sqlite3` but not `aiosqlite`,
    so the first real cron invocation would have raised NameError — a green
    suite over code that cannot run.

    These execute the real function end to end.
    """

    async def test_no_stuck_executions_returns_exit_ok(
        self, tmp_path, settings_factory
    ):
        from scout.live.solana_lane import EXIT_OK, _run_watchdog_readonly

        db_path = tmp_path / "scout.db"
        db = Database(str(db_path))
        await db.initialize()
        await db.close()

        settings = settings_factory(SOLANA_EXECUTION_WATCHDOG_ENABLED=True)
        rc = await _run_watchdog_readonly(db_path, settings)
        assert rc == EXIT_OK

    async def test_it_prints_the_operator_facing_line(
        self, tmp_path, settings_factory, capsys
    ):
        """The string cron operators and the log grep depend on."""
        from scout.live.solana_lane import _run_watchdog_readonly

        db_path = tmp_path / "scout.db"
        db = Database(str(db_path))
        await db.initialize()
        await db.close()

        settings = settings_factory(SOLANA_EXECUTION_WATCHDOG_ENABLED=True)
        await _run_watchdog_readonly(db_path, settings)
        assert "solana lane: no stuck executions." in capsys.readouterr().out

    async def test_disabled_flag_short_circuits(self, tmp_path, settings_factory):
        from scout.live.solana_lane import EXIT_OK, _run_watchdog_readonly

        db_path = tmp_path / "scout.db"
        db = Database(str(db_path))
        await db.initialize()
        await db.close()

        settings = settings_factory(SOLANA_EXECUTION_WATCHDOG_ENABLED=False)
        rc = await _run_watchdog_readonly(db_path, settings)
        assert rc == EXIT_OK

    async def test_it_does_not_write_to_the_database(
        self, tmp_path, settings_factory
    ):
        """Structural claim, verified behaviourally.

        `mode=ro` is asserted elsewhere by reading the source. This proves the
        real invocation leaves the file byte-identical — no journal, no WAL
        growth, no migration.
        """
        import hashlib

        db_path = tmp_path / "scout.db"
        db = Database(str(db_path))
        await db.initialize()
        await db.close()

        before = hashlib.sha256(db_path.read_bytes()).hexdigest()
        settings = settings_factory(SOLANA_EXECUTION_WATCHDOG_ENABLED=True)
        await _run(db_path, settings)
        after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert before == after, "the watchdog must not modify the database"


async def _run(db_path, settings):
    from scout.live.solana_lane import _run_watchdog_readonly

    return await _run_watchdog_readonly(db_path, settings)
