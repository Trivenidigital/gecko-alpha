"""Stored evidence and route isolation, using real SQLite fixtures."""

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest

from dashboard.api import create_app


def fixture_db(path, rows):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE signal_params(signal_type TEXT, enabled, suspended_at, suspended_reason, last_calibration_at)"
        )
        conn.executemany("INSERT INTO signal_params VALUES(?,?,?,?,?)", rows)
    return str(path)


async def request(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/api/signal_lane_status")


@pytest.mark.asyncio
async def test_raw_fractional_value_is_unknown_and_apps_keep_own_database(tmp_path):
    a = create_app(fixture_db(tmp_path / "a.db", [("lane", 1.5, None, None, None)]))
    b = create_app(fixture_db(tmp_path / "b.db", [("lane", 1, None, None, None)]))
    for first, second in [
        await asyncio.gather(request(a), request(b)),
        await asyncio.gather(request(a), request(b)),
    ]:
        assert first.status_code == second.status_code == 200
        lane = first.json()["lanes"][0]
        assert lane["state"] == "unknown"
        assert lane["reason"] == "invalid_enabled_value"
        assert lane["evidence"]["enabled"]["sqlite_type"] == "real"
        assert lane["evidence"]["enabled"]["display"] == "1.5"
        assert second.json()["lanes"][0]["state"] == "enabled"
        assert first.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,stamp,state",
    [
        (0, None, "disabled"),
        (0, "2026-07-03 00:24:47", "suspended"),
        (1, "2026-07-03T00:24:47Z", "unknown"),
        (1, None, "enabled"),
        (None, None, "unknown"),
        ("1", None, "unknown"),
        (b"1", None, "unknown"),
        (2, None, "unknown"),
        (0, "", "unknown"),
        (0, "0001-01-01T00:00:00+01:00", "unknown"),
        (0, "9999-01-01T00:00:00Z", "unknown"),
        (0, "2026-01-01", "unknown"),
    ],
)
async def test_storage_and_suspension_semantics(tmp_path, enabled, stamp, state):
    app = create_app(
        fixture_db(tmp_path / "state.db", [("lane", enabled, stamp, None, None)])
    )
    response = await request(app)
    assert response.status_code == 200
    assert response.json()["lanes"][0]["state"] == state


@pytest.mark.asyncio
async def test_missing_file_and_schema_do_not_create_or_initialize(tmp_path):
    missing = tmp_path / "missing.db"
    response = await request(create_app(str(missing)))
    assert response.status_code == 503
    assert not missing.exists()
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    assert (await request(create_app(str(path)))).status_code == 503
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [("same", 1, None, None, None)] * 2,
        [(None, 1, None, None, None)],
        [(str(n), 1, None, None, None) for n in range(129)],
        [("lane", 1, None, "x" * 70000, None)],
    ],
)
async def test_bad_keys_and_read_budgets_fail_closed(tmp_path, rows):
    response = await request(create_app(fixture_db(tmp_path / "invalid.db", rows)))
    assert response.status_code == 503
    assert response.json()["meta"]["ok"] is False
    assert response.json()["lanes"] == []


@pytest.mark.asyncio
async def test_successful_empty_store_is_distinct(tmp_path):
    response = await request(create_app(fixture_db(tmp_path / "empty.db", [])))
    assert response.status_code == 200
    assert response.json()["meta"]["ok"] is True
    assert response.json()["lanes"] == []


@pytest.mark.asyncio
async def test_locked_database_degrades_with_bounded_wait(tmp_path):
    path = fixture_db(tmp_path / "locked.db", [("lane", 1, None, None, None)])
    conn = sqlite3.connect(path)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        response = await asyncio.wait_for(request(create_app(path)), 2)
        assert response.status_code == 503
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier_at", ["acquire", "read"])
async def test_cancelled_awaiter_leaves_worker_owned_connection_closed(
    tmp_path, monkeypatch, barrier_at
):
    import threading
    from dashboard import lane_status

    path = fixture_db(tmp_path / "cancel.db", [("lane", 1, None, None, None)])
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = sqlite3.connect

    def barrier():
        entered.set()
        assert release.wait(2)

    class Owned:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, key):
            return getattr(self.conn, key)

        def execute(self, *args):
            if barrier_at == "read":
                barrier()
            return self.conn.execute(*args)

        def close(self):
            self.conn.close()
            closed.set()

    def connect(*args, **kwargs):
        if barrier_at == "acquire":
            barrier()
        return Owned(original(*args, **kwargs))

    monkeypatch.setattr(lane_status.sqlite3, "connect", connect)
    task = asyncio.create_task(lane_status.get_lane_status(path))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(closed.wait, 2)


def test_sql_progress_budget_interrupts_and_nonfinite_evidence_is_json_safe(
    tmp_path, monkeypatch
):
    import json
    import types
    from dashboard import lane_status

    path = fixture_db(
        tmp_path / "progress.db", [(str(n), 1, None, None, None) for n in range(128)]
    )
    ticks = iter([0] + [2] * 100)
    monkeypatch.setattr(
        lane_status, "time", types.SimpleNamespace(monotonic=lambda: next(ticks))
    )
    with pytest.raises(lane_status.LaneStatusUnavailable, match="read_limit"):
        lane_status.read_lane_status(path)
    other = fixture_db(tmp_path / "inf.db", [("lane", float("inf"), None, None, None)])
    result = lane_status.read_lane_status(other)
    assert result["lanes"][0]["state"] == "unknown"
    json.dumps(result, allow_nan=False)
