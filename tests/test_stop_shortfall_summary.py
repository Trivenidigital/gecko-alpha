import asyncio
import sqlite3
import time

import aiosqlite
import httpx
import pytest

from dashboard.api import create_app
from tests.test_stop_shortfall import history_db


async def response(path):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(str(path))), base_url="http://test"
    ) as client:
        return await client.get("/api/trading/stop-shortfall-summary")


async def test_full_population_readonly_and_app_isolation(tmp_path):
    path = history_db(tmp_path)
    before = path.read_bytes()
    app = create_app(str(path))
    create_app(str(tmp_path / "absent"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(
            "/api/trading/stop-shortfall-summary?limit=1&actionability=unknown"
        )
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    data = r.json()["data"]
    assert data["total_stop_rows"] == 3
    assert data["eligible_rows"] == 2
    assert data["modeled_rows"] == 1
    assert data["exclusions_by_reason"] == {"modeled_exit": 1}
    assert data["mean_shortfall_pp"] == pytest.approx(3)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "sql,state,reason",
    [
        ("DELETE FROM paper_trades", "empty", None),
        ("UPDATE paper_trades SET price_source='legacy'", "no_eligible_rows", None),
        (
            "DROP TABLE paper_trade_entry_snapshots",
            "unavailable",
            "evidence_schema_unavailable",
        ),
        ("DELETE FROM paper_migrations", "unavailable", "cutover_unavailable"),
        (
            "UPDATE paper_migrations SET cutover_ts='bad'",
            "unavailable",
            "cutover_unavailable",
        ),
        (
            "UPDATE paper_trades SET price_source=zeroblob(4097)",
            "unavailable",
            "input_bounds_exceeded",
        ),
    ],
)
async def test_empty_unavailable_and_excluded_are_distinct(
    tmp_path, sql, state, reason
):
    path = history_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(sql)
    r = await response(path)
    assert r.status_code == (503 if reason else 200)
    assert r.json()["data"]["state"] == state
    assert r.json()["data"]["mean_shortfall_pp"] is None
    assert r.json()["meta"]["data_missing_reason"] == reason
    if reason:
        assert r.json()["data"]["total_stop_rows"] is None
        assert r.headers["retry-after"] == "60"


async def test_real_sql_deadline_and_slow_python_deadline(tmp_path, monkeypatch):
    from dashboard import stop_shortfall_summary as mod

    path = history_db(tmp_path)
    original_execute = aiosqlite.Connection.execute

    async def slow_sql(self, sql, parameters=None):
        if sql.startswith("SELECT COUNT(*) FROM paper_trades"):
            sql = "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n"
        return await original_execute(self, sql, parameters or ())

    monkeypatch.setattr(mod, "WORK_SECONDS", 0.03)
    with monkeypatch.context() as m:
        m.setattr(aiosqlite.Connection, "execute", slow_sql)
        start = time.monotonic()
        assert (await response(path)).json()["meta"][
            "data_missing_reason"
        ] == "query_timeout"
        assert time.monotonic() - start < 2
    original_classify = mod.classify_stop_shortfall

    def slow_classifier(*args):
        time.sleep(0.04)
        return original_classify(*args)

    with monkeypatch.context() as m:
        m.setattr(mod, "classify_stop_shortfall", slow_classifier)
        assert (await response(path)).json()["meta"][
            "data_missing_reason"
        ] == "query_timeout"
    monkeypatch.setattr(mod, "WORK_SECONDS", 3)
    assert (await response(path)).status_code == 200


async def test_cancel_during_fetch_interrupts_before_queued_cursor_close(
    tmp_path, monkeypatch
):
    from dashboard import stop_shortfall_summary as mod

    path = history_db(tmp_path)
    started = asyncio.Event()
    original_fetch = aiosqlite.Cursor.fetchmany

    async def slow_fetch(self, size=None):
        started.set()
        await self._conn.execute(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n"
        )
        return await original_fetch(self, size)

    with monkeypatch.context() as m:
        m.setattr(aiosqlite.Cursor, "fetchmany", slow_fetch)
        task = asyncio.create_task(mod.get_stop_shortfall_summary(str(path)))
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.sleep(0.02)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.5)
        # Always clean up before asserting so a falsifier cannot leak a worker.
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task in done


@pytest.mark.parametrize(
    "exits,expected_mean,expected_median",
    [
        ([75], 0, 0),
        ([74, 72], 2, 2),
        ([74, 73, 66], 4, 2),
    ],
)
async def test_descriptive_statistics_include_zero_and_repeated_tokens(
    tmp_path, exits, expected_mean, expected_median
):
    path = history_db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("DELETE FROM paper_trades WHERE id>?", (len(exits),))
        c.execute(
            "UPDATE paper_trades SET exit_provenance='market',token_id='repeated',entry_price=100,amount_usd=quantity*100"
        )
        for i, price in enumerate(exits, 1):
            c.execute("UPDATE paper_trades SET exit_price=? WHERE id=?", (price, i))
    data = (await response(path)).json()["data"]
    assert data["eligible_rows"] == len(exits)
    assert data["mean_shortfall_pp"] == pytest.approx(expected_mean)
    assert data["median_shortfall_pp"] == pytest.approx(expected_median)


async def test_snapshot_is_pinned_and_write_attempt_denied(tmp_path, monkeypatch):
    path = history_db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("PRAGMA journal_mode=WAL")
    original_execute = aiosqlite.Connection.execute
    inserted = False

    async def interpose(self, sql, parameters=None):
        nonlocal inserted
        if sql.startswith("SELECT COUNT(*) FROM paper_trades") and not inserted:
            inserted = True
            with pytest.raises(aiosqlite.OperationalError, match="readonly"):
                await original_execute(self, "DELETE FROM paper_trades")
            # Schema reads already established this reader's snapshot.
            with sqlite3.connect(path) as other:
                other.execute("DELETE FROM paper_trades WHERE id=3")
        return await original_execute(self, sql, parameters or ())

    monkeypatch.setattr(aiosqlite.Connection, "execute", interpose)
    data = (await response(path)).json()["data"]
    assert inserted and data["total_stop_rows"] == 3
    assert data["eligible_rows"] == 2
    assert (await response(path)).json()["data"]["total_stop_rows"] == 2


async def test_cap_missing_database_and_duplicate_snapshot_schema(
    tmp_path, monkeypatch
):
    from dashboard import stop_shortfall_summary as mod

    assert (await response(tmp_path / "missing")).json()["meta"][
        "data_missing_reason"
    ] == "database_unavailable"
    path = history_db(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(mod, "MAX_ROWS", 2)
        data = (await response(path)).json()
        assert data["meta"]["data_missing_reason"] == "population_too_large"
        assert data["data"]["eligible_rows"] is None
    with sqlite3.connect(path) as c:
        c.execute("ALTER TABLE paper_trade_entry_snapshots RENAME TO old_snapshots")
        c.execute(
            "CREATE TABLE paper_trade_entry_snapshots AS SELECT * FROM old_snapshots"
        )
    assert (await response(path)).json()["meta"][
        "data_missing_reason"
    ] == "duplicate_snapshot_identity"


async def test_repeated_cancellation_waits_for_cleanup(tmp_path, monkeypatch):
    from dashboard import stop_shortfall_summary as mod

    path = history_db(tmp_path)
    started, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_execute, original_close = (
        aiosqlite.Connection.execute,
        aiosqlite.Connection.close,
    )
    closed = []

    async def blocked_execute(self, sql, parameters=None):
        if sql.startswith("SELECT COUNT(*) FROM paper_trades"):
            started.set()
            await asyncio.Event().wait()
        return await original_execute(self, sql, parameters or ())

    async def held_close(self):
        closing.set()
        await release.wait()
        await original_close(self)
        closed.append(self)

    with monkeypatch.context() as m:
        m.setattr(aiosqlite.Connection, "execute", blocked_execute)
        m.setattr(aiosqlite.Connection, "close", held_close)
        task = asyncio.create_task(mod.get_stop_shortfall_summary(str(path)))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed
    assert (await response(path)).status_code == 200


async def test_all_history_not_first_page_and_classifier_parity(tmp_path):
    from collections import Counter
    from dashboard.stop_shortfall import classify_stop_shortfall
    from tests.test_stop_shortfall import CUTOVER

    path = history_db(tmp_path)
    with sqlite3.connect(path) as c:
        c.row_factory = sqlite3.Row
        original = dict(c.execute("SELECT * FROM paper_trades WHERE id=1").fetchone())
        for i in range(4, 64):
            row = dict(
                original, id=i, closed_at="2026-08-20T00:00:00Z", price_source="legacy"
            )
            c.execute(
                f"INSERT INTO paper_trades ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
            c.execute(
                "INSERT INTO paper_trade_entry_snapshots VALUES (?, 'v1', 25)", (i,)
            )
        c.execute(
            "UPDATE paper_trades SET price_source='cg_lane',conviction_locked_at='changed' WHERE id=4"
        )
        c.execute(
            "UPDATE paper_trades SET price_source='cg_lane',remaining_qty=0 WHERE id=5"
        )
        c.execute(
            "UPDATE paper_trades SET price_source='cg_lane',entry_price='invalid' WHERE id=6"
        )
        expected = [
            classify_stop_shortfall(dict(r), CUTOVER)
            for r in c.execute(
                "SELECT p.*,s.entry_snapshot_version,s.sl_pct_at_entry FROM paper_trades p LEFT JOIN paper_trade_entry_snapshots s ON p.id=s.paper_trade_id"
            )
        ]
    data = (await response(path)).json()["data"]
    assert data["total_stop_rows"] == 63 and data["eligible_rows"] == 2
    assert data["mean_shortfall_pp"] == pytest.approx(3)
    reasons = Counter(
        r["exclusion_reason"] for r in expected if r["state"] != "available"
    )
    assert data["exclusions_by_reason"] == dict(reasons)


async def test_outer_async_timeout_also_cleans_up(tmp_path, monkeypatch):
    from dashboard import stop_shortfall_summary as mod

    path = history_db(tmp_path)

    async def stalled_read(*args):
        await asyncio.sleep(10)

    with monkeypatch.context() as m:
        m.setattr(mod, "REQUEST_SECONDS", 0.03)
        m.setattr(mod, "_read", stalled_read)
        result = (await response(path)).json()
        assert result["meta"]["data_missing_reason"] == "query_timeout"
        assert result["data"]["total_stop_rows"] is None
    assert (await response(path)).status_code == 200


@pytest.mark.parametrize("cancel_kind", ["repeated_cancel", "outer_timeout"])
async def test_acquisition_remains_owned_until_real_connection_closed(
    tmp_path, monkeypatch, cancel_kind
):
    import threading
    from dashboard import stop_shortfall_summary as mod

    path = history_db(tmp_path)
    entered, release = threading.Event(), threading.Event()
    opened = []
    original_connect = sqlite3.connect

    class Tracked(sqlite3.Connection):
        was_closed = False

        def close(self):
            self.was_closed = True
            return super().close()

    def held_connect(*args, **kwargs):
        kwargs.update(factory=Tracked, check_same_thread=False)
        conn = original_connect(*args, **kwargs)
        opened.append(conn)
        entered.set()
        release.wait(2)
        return conn

    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", held_connect)
        if cancel_kind == "outer_timeout":
            m.setattr(mod, "REQUEST_SECONDS", 0.03)
        task = asyncio.create_task(mod.get_stop_shortfall_summary(str(path)))
        assert await asyncio.to_thread(entered.wait, 1)
        if cancel_kind == "repeated_cancel":
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
        await asyncio.sleep(0.06)
        premature = task.done()
        release.set()
        if cancel_kind == "repeated_cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)["meta"]["data_missing_reason"] == "query_timeout"
        await asyncio.sleep(0.02)
        properly_closed = all(conn.was_closed for conn in opened)
        # Release resources even when the intentional red run exposes abandonment.
        for conn in opened:
            if not conn.was_closed:
                conn.close()
        assert not premature, "request abandoned in-flight SQLite acquisition"
        assert properly_closed, "acquired native connection leaked"
    assert (await response(path)).status_code == 200


async def test_cutover_must_be_utc_normalizable(tmp_path):
    path = history_db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("UPDATE paper_migrations SET cutover_ts='0001-01-01T00:00:00+01:00'")
    result = await response(path)
    assert result.status_code == 503
    assert result.json()["meta"]["data_missing_reason"] == "cutover_unavailable"
