import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest
import httpx

from dashboard import suppression_health as health

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE signal_outcome_ledger(id INTEGER PRIMARY KEY, kind TEXT,
        token_id, surface, gate_verdicts, emitted_at, r24h, r7d, label_status);
        CREATE TABLE trade_decision_events(signal_type, reason, created_at, decision);
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
    with sqlite3.connect(path) as conn:
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
    with sqlite3.connect(ledger) as conn:
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
    add(ledger, stamp="2026-08-15T00:00:00Z")
    add(ledger, token="b", stamp="2026-08-14T23:59:59.999999Z")
    add(ledger, token="c", stamp="2026-09-14T00:00:00Z")
    add(ledger, token="d", stamp="garbage")
    add(ledger, token="e", stamp="2026-09-07T01:00:00+01:00")
    result = health.read_health(ledger, now=NOW)
    assert result["cohort"]["rows"] == 2
    assert result["population"][0]["ledger_rows"] == 1
    assert result["meta"]["table_wide_timestamp_counts"] is None


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
    with sqlite3.connect(ledger) as conn:
        conn.execute("ALTER TABLE signal_outcome_ledger RENAME TO original")
        conn.execute("""CREATE VIEW signal_outcome_ledger AS
          WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000000)
          SELECT * FROM original WHERE id=(SELECT sum(x) FROM n)""")
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
    with sqlite3.connect(ledger) as conn:
        conn.execute("DROP INDEX idx_tde_decision_reason_created")
        if definition:
            conn.execute(definition)
    assert health.read_health(ledger, now=NOW)["meta"]["reason"] == "schema_unavailable"


def test_reason_only_population_across_decision_values(ledger):
    with sqlite3.connect(ledger) as conn:
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
