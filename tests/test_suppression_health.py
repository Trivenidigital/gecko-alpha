from contextlib import closing, contextmanager
import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest
import httpx

from dashboard import suppression_health as health

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


@contextmanager
def db_connection(path):
    with closing(sqlite3.connect(path)) as conn:
        with conn:
            yield conn


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger.db"
    with db_connection(path) as conn:
        conn.executescript("""
        CREATE TABLE signal_outcome_ledger(id INTEGER PRIMARY KEY, kind TEXT,
        token_id, surface, gate_verdicts, emitted_at, r24h, r7d, label_status);
        CREATE TABLE trade_decision_events(signal_type, reason, created_at, decision);
        CREATE INDEX idx_sol_status_emitted ON signal_outcome_ledger(label_status,emitted_at);
        CREATE INDEX idx_tde_decision_reason_created ON trade_decision_events(decision,reason,created_at);
        """)
    return path


def add(
    path,
    token="a",
    surface="losers",
    stamp="2026-09-10T00:00:00+00:00",
    r7=None,
    status="complete",
    verdict=None,
):
    with db_connection(path) as conn:
        conn.execute(
            "INSERT INTO signal_outcome_ledger VALUES(NULL,?,?,?,?,?,?,?,?)",
            (
                "gated_out_sample",
                token,
                surface,
                (
                    verdict
                    if verdict is not None
                    else json.dumps(
                        {"reason": "suppressed", "source_layer": "dispatcher"}
                    )
                ),
                stamp,
                None,
                r7,
                status,
            ),
        )


def test_cohort_and_anchor_are_not_price_readiness(ledger):
    add(ledger)
    add(ledger, stamp="2026-09-11T00:00:00Z", r7=2)
    add(ledger, token="b", surface="chain", r7=1)
    add(ledger, token="bad", verdict="[]")
    with db_connection(ledger) as conn:
        conn.execute(
            "INSERT INTO trade_decision_events VALUES('losers','suppressed','2026-09-10T00:00:00Z','blocked')"
        )
    result = health.read_health(ledger, now=NOW)
    assert result["meta"]["ok"]
    assert result["cohort"]["rows"] == 3
    assert result["cohort"]["distinct_tokens"] == 2
    assert result["cohort"]["earliest_anchor_tokens_with_recorded_r7d"] == 1
    assert result["cohort"]["recorded_r7d"] == 2
    assert result["cohort"]["label_status"]["complete"] == 3
    assert result["diagnostics"]["malformed_verdict_rows"] == 1
    assert result["population"][0]["state"] == "ledger_only"
    assert result["meta"]["provenance"] == "unverified"
    assert "r7d" not in json.dumps(result).replace("recorded_r7d", "")


def test_exact_windows_future_invalid_and_offset(ledger):
    add(ledger, stamp="2026-08-31T00:00:00Z")
    add(ledger, token="b", stamp="2026-08-30T23:59:59.999999Z")
    add(ledger, token="c", stamp="2026-09-14T00:00:00Z")
    add(ledger, token="d", stamp="garbage")
    add(ledger, token="e", stamp="2026-09-07T01:00:00+01:00")
    result = health.read_health(ledger, now=NOW)
    assert result["cohort"]["rows"] == 2
    assert result["population"][0]["ledger_rows"] == 1
    assert result["meta"]["table_wide_timestamp_counts"] is None
    assert result["meta"]["window_days"] == 7
    assert result["meta"]["lookback_days"] == 14


def test_empty_missing_and_budget_are_distinct(ledger, tmp_path, monkeypatch):
    assert health.read_health(ledger, now=NOW)["state"] == "insufficient_data"
    missing = tmp_path / "absent.db"
    assert not health.read_health(missing, now=NOW)["meta"]["ok"]
    assert not missing.exists()
    add(ledger)
    monkeypatch.setattr(health, "LEDGER_LIMIT", 0)
    result = health.read_health(ledger, now=NOW)
    assert result["meta"]["reason"] == "read_limit"
    assert "cohort" not in result


@pytest.mark.asyncio
async def test_cancellation_keeps_worker_owned_until_close(ledger, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = health.read_health

    def blocked(path):
        entered.set()
        assert release.wait(2)
        return original(path, now=NOW)

    monkeypatch.setattr(health, "read_health", blocked)
    reader = health.HealthReader(ledger)
    task = asyncio.create_task(reader.get())
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(3):
            assert (await reader.get())["meta"]["reason"] == "busy"
    finally:
        release.set()
        await reader.drain()
    assert (await reader.get())["meta"]["ok"]


@pytest.mark.asyncio
async def test_endpoint_captures_each_app_path(ledger, tmp_path):
    from dashboard.api import create_app

    app_a = create_app(str(ledger))
    app_b = create_app(str(tmp_path / "missing.db"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_a), base_url="http://test"
    ) as a:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="http://test"
        ) as b:
            good, bad = await asyncio.gather(
                a.get("/api/suppression_cohort/health"),
                b.get("/api/suppression_cohort/health"),
            )
    assert good.status_code == 200
    assert good.headers["cache-control"] == "no-store"
    assert bad.status_code == 503


def test_sql_timeout_and_python_timeout(ledger, monkeypatch):
    add(ledger)
    original = health._stamp

    def slow(value):
        import time

        time.sleep(0.03)
        return original(value)

    monkeypatch.setattr(health, "READ_SECONDS", 0.02)
    monkeypatch.setattr(health, "_stamp", slow)
    assert health.read_health(ledger, now=NOW)["meta"]["reason"] == "read_limit"
    monkeypatch.setattr(health, "_stamp", original)
    original_connect = sqlite3.connect

    class ExpensiveQuery(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith("SELECT id,token_id"):
                # Keep real schema/index guards, then force actual VM work.
                return super().execute("""WITH RECURSIVE n(x) AS
                    (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000000)
                    SELECT sum(x) FROM n""")
            return super().execute(sql, parameters)

    monkeypatch.setattr(
        health.sqlite3,
        "connect",
        lambda *a, **kw: original_connect(*a, **kw, factory=ExpensiveQuery),
    )
    assert health.read_health(ledger, now=NOW)["meta"]["reason"] == "read_limit"


def test_invalid_keys_unknown_status_ties_and_no_writes(ledger):
    add(ledger, token="", surface=None, status="surprise")
    add(ledger, token="tied")
    add(ledger, token="tied", r7=1)
    before = ledger.read_bytes()
    result = health.read_health(ledger, now=NOW)
    assert ledger.read_bytes() == before
    assert result["cohort"]["invalid_token_rows"] == 1
    assert result["cohort"]["label_status"]["unknown"] == 1
    assert result["cohort"]["earliest_anchor_tokens_with_recorded_r7d"] == 0
    assert result["population"][-1]["signal_type"] is None


@pytest.mark.parametrize(
    "definition",
    [
        None,
        "CREATE INDEX idx_tde_decision_reason_created ON trade_decision_events(reason,decision,created_at)",
        "CREATE INDEX idx_tde_decision_reason_created ON trade_decision_events(decision,reason,created_at) WHERE decision='blocked'",
    ],
)
def test_missing_wrong_or_partial_index_refuses(ledger, definition):
    with db_connection(ledger) as conn:
        conn.execute("DROP INDEX idx_tde_decision_reason_created")
        if definition:
            conn.execute(definition)
    assert health.read_health(ledger, now=NOW)["meta"]["reason"] == "schema_unavailable"


def test_reason_only_population_across_decision_values(ledger):
    with db_connection(ledger) as conn:
        conn.executemany(
            "INSERT INTO trade_decision_events VALUES(?,?,?,?)",
            [
                ("lane", "suppressed", "2026-09-10T00:00:00Z", state)
                for state in ("blocked", "opened", "unknown", None)
            ],
        )
    result = health.read_health(ledger, now=NOW)
    assert result["population"] == [
        {
            "signal_type": "lane",
            "ledger_rows": 0,
            "decision_rows": 4,
            "state": "decision_only",
        }
    ]


@pytest.mark.asyncio
async def test_real_acquisition_cancel_eventually_closes_connection(
    ledger, monkeypatch
):
    opened, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = sqlite3.connect

    class Tracked(sqlite3.Connection):
        def close(self):
            super().close()
            closed.set()

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs, factory=Tracked)
        opened.set()
        assert release.wait(2)
        return connection

    monkeypatch.setattr(health.sqlite3, "connect", connect)
    reader = health.HealthReader(ledger)
    task = asyncio.create_task(reader.get())
    try:
        while not opened.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not closed.is_set()
        assert (await reader.get())["meta"]["reason"] == "busy"
    finally:
        release.set()
        await reader.drain()
    assert closed.is_set()
    ledger.unlink()  # Windows proves there is no remaining acquired handle.


@pytest.mark.parametrize("setting", ["TOKEN_LIMIT", "SIGNAL_LIMIT", "DECISION_LIMIT"])
def test_population_budgets_fail_without_partial_counts(ledger, monkeypatch, setting):
    add(ledger)
    with db_connection(ledger) as conn:
        conn.execute(
            "INSERT INTO trade_decision_events VALUES('lane','suppressed','2026-09-10T00:00:00Z','anything')"
        )
    monkeypatch.setattr(health, setting, 0)
    assert health.read_health(ledger, now=NOW) == health.unavailable("read_limit")


def test_snapshot_spans_ledger_and_decisions(ledger, monkeypatch):
    with db_connection(ledger) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    add(ledger)
    original = health._stamp
    changed = False

    def concurrent_write(value):
        nonlocal changed
        if not changed:
            changed = True
            with db_connection(ledger) as writer:
                writer.execute(
                    "INSERT INTO trade_decision_events VALUES('new','suppressed','2026-09-10T00:00:00Z','blocked')"
                )
        return original(value)

    monkeypatch.setattr(health, "_stamp", concurrent_write)
    result = health.read_health(ledger, now=NOW)
    assert sum(row["decision_rows"] for row in result["population"]) == 0
    assert (
        sum(
            row["decision_rows"]
            for row in health.read_health(ledger, now=NOW)["population"]
        )
        == 1
    )


def test_oversized_verdict_and_invalid_anchor_id_refuse(ledger):
    add(ledger, verdict=" " * 16385)
    assert health.read_health(ledger, now=NOW)["meta"]["reason"] == "read_limit"
    with db_connection(ledger) as conn:
        conn.execute("DELETE FROM signal_outcome_ledger")
    add(ledger)
    with db_connection(ledger) as conn:
        conn.execute("ALTER TABLE signal_outcome_ledger RENAME TO original")
        conn.execute(
            "CREATE TABLE signal_outcome_ledger AS SELECT CAST(id AS TEXT) AS id,kind,token_id,surface,gate_verdicts,emitted_at,r24h,r7d,label_status FROM original"
        )
        conn.execute("DROP INDEX idx_sol_status_emitted")
        conn.execute(
            "CREATE INDEX idx_sol_status_emitted ON signal_outcome_ledger(label_status,emitted_at)"
        )
    assert health.read_health(ledger, now=NOW)["meta"]["reason"] == "schema_unavailable"


@pytest.mark.parametrize(
    "definition",
    [
        None,
        "CREATE INDEX idx_sol_status_emitted ON signal_outcome_ledger(emitted_at,label_status)",
        "CREATE INDEX idx_sol_status_emitted ON signal_outcome_ledger(label_status,emitted_at) WHERE label_status='complete'",
    ],
)
def test_missing_wrong_or_partial_ledger_index_refuses(ledger, definition):
    with db_connection(ledger) as conn:
        conn.execute("DROP INDEX idx_sol_status_emitted")
        if definition:
            conn.execute(definition)
    assert health.read_health(ledger, now=NOW)["meta"]["reason"] == "schema_unavailable"


def test_all_statuses_and_earliest_anchor_ignore_index_iteration_order(ledger):
    add(ledger, token="repeat", status="unlabelable", stamp="2026-09-09T00:00:00Z")
    add(ledger, token="repeat", status="complete", r7=1)
    for status in ("pending", "partial", "strange"):
        add(ledger, token=status, status=status)
    result = health.read_health(ledger, now=NOW)
    assert result["cohort"]["rows"] == 5
    assert all(value == 1 for value in result["cohort"]["label_status"].values())
    assert result["cohort"]["earliest_anchor_tokens_with_recorded_r7d"] == 0


def test_fourteen_day_anchor_is_window_relative(ledger):
    add(ledger, stamp="2026-08-30T00:00:00Z")  # 15days, excluded
    add(ledger, stamp="2026-09-01T01:00:00+01:00", r7=1)  # 13days, earliest in-window
    add(ledger, stamp="2026-09-10T00:00:00Z")
    result = health.read_health(ledger, now=NOW)
    assert result["cohort"]["rows"] == 2
    assert result["cohort"]["earliest_anchor_tokens_with_recorded_r7d"] == 1
    assert result["population"][0]["ledger_rows"] == 1
    assert result["meta"]["lookback_start"] == "2026-08-31T00:00:00+00:00"
