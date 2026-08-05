"""Atomicity of a strategy mutation and its audit row, proven by failure injection.

An earlier revision of this delivery justified the write path with a claim about
SQLite implicitly COMMITting before DDL, and shipped *structural* tests — grep
the source for ordering, for a single `commit()`, for the absence of `CREATE
TABLE` in the audit writer. Two problems with that.

The claim was false. Measured on this stack (Python 3.14.3 / SQLite 3.50.4 /
aiosqlite 0.22.1, ``isolation_level ""``), the sequence ``UPDATE ->
execute("CREATE TABLE ...") -> ROLLBACK`` rolls the UPDATE back. The DDL hoist
is retained as clean separation, not because it repairs an implicit commit.

And structural tests cannot prove atomicity at all. Source order says nothing
about what the database does when a statement between two writes raises. These
tests injure the write path at each point and assert the observable end state:
either both the mutation and its audit row are present, or neither is.

Audit rows are the record of who changed a strategy parameter and why. A
half-written pair is worse than no audit at all — a value that moved with no
audit row reads as an agent action, and an audit row with no value change reads
as a change that never happened.
"""

from __future__ import annotations

import aiosqlite
import pytest

import dashboard.db as ddb

AUDIT = "agent_strategy_audit"


@pytest.fixture
async def seeded_db(tmp_path):
    """Minimal agent_strategy fixture.

    Local rather than shared: these tests need exactly one table, and a fixture
    that also builds candidates/predictions/alerts would make a failure here
    ambiguous with unrelated schema drift.
    """
    path = str(tmp_path / "strategy.db")
    conn = await aiosqlite.connect(path)
    try:
        await conn.execute("""CREATE TABLE agent_strategy (
                 key         TEXT PRIMARY KEY,
                 value       TEXT NOT NULL,
                 locked      INTEGER DEFAULT 0,
                 updated_by  TEXT,
                 updated_at  TEXT)""")
        await conn.execute(
            "INSERT INTO agent_strategy (key, value, locked, updated_by) "
            "VALUES (?, ?, ?, ?)",
            ("top_n", "5", 0, "agent"),
        )
        await conn.commit()
    finally:
        await conn.close()
    return path


@pytest.fixture
def make_app():
    def _make(db_path: str):
        from dashboard.api import create_app

        return create_app(db_path)

    return _make


# ---------------------------------------------------------------------------
# Helpers: read committed state through a SEPARATE connection.
#
# Deliberately not the connection under test — reading back through the same
# handle could observe uncommitted state inside its own transaction and report
# a rollback that never happened.
# ---------------------------------------------------------------------------


async def _row(db_path: str, key: str) -> tuple | None:
    conn = await aiosqlite.connect(db_path)
    try:
        cur = await conn.execute(
            "SELECT value, locked FROM agent_strategy WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        return tuple(row) if row else None
    finally:
        await conn.close()


async def _audit_rows(db_path: str, key: str) -> list[tuple]:
    conn = await aiosqlite.connect(db_path)
    try:
        cur = await conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{AUDIT}'"
        )
        if await cur.fetchone() is None:
            return []
        cur = await conn.execute(
            f"SELECT old_value, new_value, old_locked, new_locked, operator, reason "
            f"FROM {AUDIT} WHERE key = ? ORDER BY id",
            (key,),
        )
        return [tuple(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


class _SpyConn:
    """Records every statement, commit and rollback in order.

    Also the failure-injection point: ``fail_on`` names a SQL fragment whose
    statement should raise instead of executing.
    """

    def __init__(self, inner, fail_on: str | None = None):
        self._inner = inner
        self.fail_on = fail_on
        self.ops: list[str] = []

    # -- recording ----------------------------------------------------------
    async def execute(self, sql, *a, **kw):
        normalized = " ".join(str(sql).split())
        if self.fail_on and self.fail_on in normalized:
            self.ops.append(f"RAISE<{self.fail_on}>")
            raise RuntimeError(f"injected failure on: {self.fail_on}")
        self.ops.append(normalized)
        return await self._inner.execute(sql, *a, **kw)

    async def executescript(self, sql, *a, **kw):
        self.ops.append("EXECUTESCRIPT")
        return await self._inner.executescript(sql, *a, **kw)

    async def commit(self):
        self.ops.append("COMMIT")
        return await self._inner.commit()

    async def rollback(self):
        self.ops.append("ROLLBACK")
        return await self._inner.rollback()

    async def close(self):
        self.ops.append("CLOSE")
        return await self._inner.close()

    def __setattr__(self, name, value):
        if name in ("_inner", "fail_on", "ops"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def spy(monkeypatch):
    """Install a recording connection factory and hand back the spies made."""
    made: list[_SpyConn] = []
    state = {"fail_on": None}
    real_connect = aiosqlite.connect

    class _Shim:
        Row = aiosqlite.Row

        @staticmethod
        async def connect(*a, **kw):
            conn = await real_connect(*a, **kw)
            s = _SpyConn(conn, fail_on=state["fail_on"])
            made.append(s)
            return s

    monkeypatch.setattr(ddb, "aiosqlite", _Shim)

    class _Handle:
        conns = made

        @staticmethod
        def fail_on(fragment: str):
            state["fail_on"] = fragment

        @staticmethod
        def writer() -> _SpyConn:
            """The connection that performed the mutation."""
            for c in made:
                if any("UPDATE agent_strategy" in op for op in c.ops):
                    return c
            raise AssertionError(
                f"no connection ran an UPDATE; saw {[c.ops for c in made]}"
            )

    return _Handle


# ===========================================================================
# 1. Audit INSERT fails -> the mutation must be rolled back
# ===========================================================================


class TestAuditInsertFailureRollsBackTheMutation:
    async def test_a_value_update_is_reverted(self, seeded_db, monkeypatch):
        before = await _row(seeded_db, "top_n")
        assert before == ("5", 0)

        async def _boom(*a, **kw):
            raise RuntimeError("injected audit INSERT failure")

        monkeypatch.setattr(ddb, "_audit_strategy_change", _boom)

        with pytest.raises(RuntimeError):
            await ddb.update_narrative_strategy(
                seeded_db, "top_n", "999", reason="should not survive"
            )

        assert await _row(seeded_db, "top_n") == before, (
            "the value change committed without its audit row — a parameter "
            "moved with no record of who moved it or why"
        )
        assert await _audit_rows(seeded_db, "top_n") == []

    async def test_a_lock_is_reverted(self, seeded_db, monkeypatch):
        before = await _row(seeded_db, "top_n")

        async def _boom(*a, **kw):
            raise RuntimeError("injected audit INSERT failure")

        monkeypatch.setattr(ddb, "_audit_strategy_change", _boom)

        with pytest.raises(RuntimeError):
            await ddb.set_strategy_lock(
                seeded_db, "top_n", lock=True, operator="manual", reason="freeze"
            )

        assert await _row(seeded_db, "top_n") == before
        assert await _audit_rows(seeded_db, "top_n") == []

    async def test_an_unlock_is_reverted(self, seeded_db, monkeypatch):
        """Unlock is the recovery path. A half-applied unlock is the worst of
        the four: the operator believes the key is recoverable and it is not."""
        await ddb.set_strategy_lock(
            seeded_db, "top_n", lock=True, operator="manual", reason="freeze"
        )
        assert await _row(seeded_db, "top_n") == ("5", 1)
        audit_before = await _audit_rows(seeded_db, "top_n")

        async def _boom(*a, **kw):
            raise RuntimeError("injected audit INSERT failure")

        monkeypatch.setattr(ddb, "_audit_strategy_change", _boom)

        with pytest.raises(RuntimeError):
            await ddb.set_strategy_lock(
                seeded_db, "top_n", lock=False, operator="manual", reason="unfreeze"
            )

        assert await _row(seeded_db, "top_n") == (
            "5",
            1,
        ), "the key silently unlocked with no audit row"
        assert await _audit_rows(seeded_db, "top_n") == audit_before

    async def test_the_real_insert_failing_also_reverts(self, seeded_db, spy):
        """Injected at the SQL layer rather than by replacing the writer.

        Patching `_audit_strategy_change` proves the caller's transaction
        boundary. This proves the same thing when the INSERT itself is what
        fails, which is the shape a real disk/constraint error takes.
        """
        before = await _row(seeded_db, "top_n")
        spy.fail_on(f"INSERT INTO {AUDIT}")

        with pytest.raises(RuntimeError):
            await ddb.update_narrative_strategy(
                seeded_db, "top_n", "424242", reason="sql-layer injection"
            )

        assert await _row(seeded_db, "top_n") == before
        assert await _audit_rows(seeded_db, "top_n") == []


# ===========================================================================
# 2. UPDATE fails -> no audit row
# ===========================================================================


class TestUpdateFailureWritesNoAuditRow:
    async def test_a_failed_value_update_leaves_no_audit_row(self, seeded_db, spy):
        before = await _row(seeded_db, "top_n")
        spy.fail_on("UPDATE agent_strategy")

        with pytest.raises(RuntimeError):
            await ddb.update_narrative_strategy(
                seeded_db, "top_n", "31337", reason="never applied"
            )

        assert await _row(seeded_db, "top_n") == before
        assert (
            await _audit_rows(seeded_db, "top_n") == []
        ), "an audit row claims a change that never happened"

    async def test_a_failed_lock_leaves_no_audit_row(self, seeded_db, spy):
        before = await _row(seeded_db, "top_n")
        spy.fail_on("UPDATE agent_strategy")

        with pytest.raises(RuntimeError):
            await ddb.set_strategy_lock(
                seeded_db, "top_n", lock=True, operator="manual", reason="never applied"
            )

        assert await _row(seeded_db, "top_n") == before
        assert await _audit_rows(seeded_db, "top_n") == []


# ===========================================================================
# 3. Same connection, one transaction, nothing in between
# ===========================================================================


class TestOneTransactionOnOneConnection:
    async def test_mutation_and_audit_run_on_the_same_connection(self, seeded_db, spy):
        await ddb.update_narrative_strategy(
            seeded_db, "top_n", "12", reason="same-connection check"
        )
        writer = spy.writer()
        assert any(f"INSERT INTO {AUDIT}" in op for op in writer.ops), (
            "the audit INSERT went to a different connection than the UPDATE, "
            "so the two can never share a transaction"
        )

    async def test_no_commit_or_executescript_between_them(self, seeded_db, spy):
        await ddb.update_narrative_strategy(
            seeded_db, "top_n", "13", reason="interleaving check"
        )
        ops = spy.writer().ops
        i_update = next(i for i, o in enumerate(ops) if "UPDATE agent_strategy" in o)
        i_audit = next(i for i, o in enumerate(ops) if f"INSERT INTO {AUDIT}" in o)
        assert i_update < i_audit
        between = ops[i_update + 1 : i_audit]
        assert "COMMIT" not in between, (
            "an intervening COMMIT splits the state change from its audit row: "
            "a crash in the gap leaves the mutation durable and unaudited"
        )
        assert (
            "EXECUTESCRIPT" not in between
        ), "executescript() COMMITs any open transaction before running"
        assert not any(
            "CREATE TABLE" in o for o in between
        ), "DDL between the two writes; the hoist exists to prevent exactly this"

    async def test_exactly_one_commit_follows_the_pair(self, seeded_db, spy):
        await ddb.update_narrative_strategy(
            seeded_db, "top_n", "14", reason="single-commit check"
        )
        ops = spy.writer().ops
        i_audit = next(i for i, o in enumerate(ops) if f"INSERT INTO {AUDIT}" in o)
        assert ops[i_audit + 1 :].count("COMMIT") == 1

    async def test_the_lock_path_has_the_same_shape(self, seeded_db, spy):
        await ddb.set_strategy_lock(
            seeded_db, "top_n", lock=True, operator="manual", reason="shape check"
        )
        ops = spy.writer().ops
        i_update = next(i for i, o in enumerate(ops) if "UPDATE agent_strategy" in o)
        i_audit = next(i for i, o in enumerate(ops) if f"INSERT INTO {AUDIT}" in o)
        between = ops[i_update + 1 : i_audit]
        assert i_update < i_audit
        assert "COMMIT" not in between and "EXECUTESCRIPT" not in between

    async def test_the_ddl_is_committed_before_the_mutation_begins(
        self, seeded_db, spy
    ):
        """The hoist, verified by observation rather than by source order.

        `CREATE TABLE IF NOT EXISTS` and its commit must both precede the
        UPDATE, so the business transaction contains only the two writes.
        """
        await ddb.update_narrative_strategy(
            seeded_db, "top_n", "15", reason="ddl-hoist check"
        )
        ops = spy.writer().ops
        i_ddl = next(i for i, o in enumerate(ops) if "CREATE TABLE" in o)
        i_update = next(i for i, o in enumerate(ops) if "UPDATE agent_strategy" in o)
        assert i_ddl < i_update
        assert "COMMIT" in ops[i_ddl:i_update]


class TestTheEndpointErrorHandlersActuallyRun:
    """Both handlers claim to convert DB errors into clean JSON. Test that.

    The lock route's handler called an undefined name (`logger`, where this
    module binds `_log`), so a DB error raised NameError from inside the very
    block that exists to stop stack traces reaching the operator — and on the
    unlock recovery path, which is the one you reach for when something has
    already gone wrong.
    """

    @pytest.fixture
    def _explode(self, monkeypatch):
        async def _boom(*a, **kw):
            raise RuntimeError("database exploded")

        return _boom

    async def test_the_lock_route_returns_json_500_on_a_db_error(
        self, seeded_db, make_app, monkeypatch, _explode
    ):
        from httpx import ASGITransport, AsyncClient

        monkeypatch.setattr(ddb, "set_strategy_lock", _explode)
        app = make_app(seeded_db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.put(
                "/api/narrative/strategy/top_n/lock",
                json={"lock": True, "reason": "handler check"},
            )
        assert resp.status_code == 500
        assert resp.json() == {"detail": "lock update failed"}

    async def test_the_value_route_returns_json_500_on_a_db_error(
        self, seeded_db, make_app, monkeypatch, _explode
    ):
        from httpx import ASGITransport, AsyncClient

        monkeypatch.setattr(ddb, "update_narrative_strategy", _explode)
        app = make_app(seeded_db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.put("/api/narrative/strategy/top_n", json={"value": "9"})
        assert resp.status_code == 500
        assert resp.json()["error"] == "internal_error"

    async def test_no_handler_references_an_unbound_logger_name(self):
        """The class of bug, not just the instance.

        `dashboard.api` binds `_log`; any `logger.` reference in it is a
        NameError waiting for an error path to reach it.
        """
        from pathlib import Path

        import dashboard.api as api

        src = Path(api.__file__).read_text("utf-8")
        assert "_log = " in src
        import re

        assert not re.search(
            r"(?<![\w.])logger\.", src
        ), "dashboard/api.py references `logger.` but only binds `_log`"


class TestTheFailurePathUnwindsExplicitly:
    async def test_an_exception_rolls_back_before_closing(self, seeded_db, spy):
        """The unwind is explicit, not a side effect of `close()`."""
        spy.fail_on(f"INSERT INTO {AUDIT}")
        with pytest.raises(RuntimeError):
            await ddb.update_narrative_strategy(seeded_db, "top_n", "1", reason="x")
        ops = spy.writer().ops
        assert "ROLLBACK" in ops
        assert ops.index("ROLLBACK") < ops.index("CLOSE")

    async def test_a_failing_rollback_does_not_replace_the_real_error(
        self, seeded_db, monkeypatch
    ):
        """Cleanup must never become the reported cause.

        This path was added as a guard and would have raised NameError on the
        logging line — a guard nothing exercised. It is exercised now.
        """
        real_connect = aiosqlite.connect

        class _RollbackExplodes:
            def __init__(self, inner):
                object.__setattr__(self, "_inner", inner)

            async def rollback(self):
                raise RuntimeError("rollback itself failed")

            def __setattr__(self, name, value):
                setattr(self._inner, name, value)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        class _Shim:
            Row = aiosqlite.Row

            @staticmethod
            async def connect(*a, **kw):
                return _RollbackExplodes(await real_connect(*a, **kw))

        monkeypatch.setattr(ddb, "aiosqlite", _Shim)

        async def _boom(*a, **kw):
            raise ValueError("the original cause")

        monkeypatch.setattr(ddb, "_audit_strategy_change", _boom)

        with pytest.raises(ValueError, match="the original cause"):
            await ddb.update_narrative_strategy(seeded_db, "top_n", "2", reason="x")


# ===========================================================================
# 4. The happy path still records everything, and locks still hold
# ===========================================================================


class TestCommittedPairsAreComplete:
    async def test_a_successful_update_commits_both(self, seeded_db):
        await ddb.update_narrative_strategy(
            seeded_db, "top_n", "42", operator="manual", reason="deliberate change"
        )
        assert await _row(seeded_db, "top_n") == ("42", 0)
        rows = await _audit_rows(seeded_db, "top_n")
        assert rows == [("5", "42", 0, 0, "manual", "deliberate change")]

    async def test_a_successful_lock_commits_both(self, seeded_db):
        await ddb.set_strategy_lock(
            seeded_db, "top_n", lock=True, operator="manual", reason="freeze"
        )
        assert await _row(seeded_db, "top_n") == ("5", 1)
        rows = await _audit_rows(seeded_db, "top_n")
        # value unchanged on both sides of a lock-only change
        assert rows == [("5", "5", 0, 1, "manual", "freeze")]

    async def test_a_locked_value_is_unchangeable_by_an_ordinary_update(
        self, seeded_db, make_app
    ):
        """The endpoint refuses, the value holds, and the refusal writes no
        audit row — a rejected attempt is not a change."""
        from httpx import ASGITransport, AsyncClient

        await ddb.set_strategy_lock(
            seeded_db, "top_n", lock=True, operator="manual", reason="freeze"
        )
        audit_before = await _audit_rows(seeded_db, "top_n")

        app = make_app(seeded_db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.put("/api/narrative/strategy/top_n", json={"value": "999"})

        assert resp.status_code == 403
        assert await _row(seeded_db, "top_n") == ("5", 1)
        assert await _audit_rows(seeded_db, "top_n") == audit_before

    async def test_unlock_then_update_works_end_to_end(self, seeded_db):
        """The recovery path, whole: lock, unlock, edit."""
        await ddb.set_strategy_lock(
            seeded_db, "top_n", lock=True, operator="manual", reason="freeze"
        )
        await ddb.set_strategy_lock(
            seeded_db, "top_n", lock=False, operator="manual", reason="review done"
        )
        await ddb.update_narrative_strategy(
            seeded_db, "top_n", "7", operator="manual", reason="post-unlock edit"
        )
        assert await _row(seeded_db, "top_n") == ("7", 0)
        assert [r[-1] for r in await _audit_rows(seeded_db, "top_n")] == [
            "freeze",
            "review done",
            "post-unlock edit",
        ]
