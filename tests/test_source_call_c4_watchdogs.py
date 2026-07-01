"""C4 silent-failure watchdogs (design #392 §4.5): a per-cycle runs table (§12a
"read output rows") + coverage/freshness/provider-error checks. Suppression is
load-bearing — the C2 writer is default-off, so "never ran" must NOT alert.
DB-only — runs on Windows.
"""

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.source_quality.snapshot_writer import record_snapshot_run
from scout.source_quality.watchdogs import evaluate_snapshot_watchdogs

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "c4w.db")
    await d.initialize()
    yield d
    await d.close()


def _iso(dt):
    return dt.isoformat()


async def _fetchone(conn, sql, params=()):
    cur = await conn.execute(sql, params)
    return await cur.fetchone()


async def _insert_eligible_call(
    conn, *, event_id, ca, chain="base", call_ts, outcome="pending"
):
    await conn.execute(
        "INSERT INTO source_calls "
        "(source_type, source_id, source_event_id, contract_address, chain, "
        " call_ts, call_kind, cluster_identity, cluster_identity_kind, "
        " duplicate_cluster_key, resolved_state, outcome_status, missing_fields) "
        "VALUES ('x','kol',?,?,?,?,'ca_call','cid','contract',?,'eligible_contract',?,'[]')",
        (event_id, ca, chain, call_ts, f"dck-{event_id}", outcome),
    )
    await conn.commit()


async def _insert_snapshot(conn, *, identity_key, snapshot_at, price=1.0, chain="base"):
    await conn.execute(
        "INSERT INTO source_call_price_snapshots "
        "(identity_key, identity_kind, chain, price, snapshot_at, source) "
        "VALUES (?, 'contract', ?, ?, ?, 'gt')",
        (identity_key, chain, price, snapshot_at),
    )
    await conn.commit()


def _finding(findings, check):
    matches = [f for f in findings if f.check == check]
    assert matches, f"no finding for check={check}"
    return matches[0]


DEFAULT_STATS = {
    "identities_seen": 0,
    "snapshots_written": 0,
    "provider_errors": 0,
    "pools_unresolved": 0,
    "empty_ohlcv": 0,
}


def _stats(**over):
    s = dict(DEFAULT_STATS)
    s.update(over)
    return s


# --------------------------------------------------------------------------
# C4b — runs table + record_snapshot_run
# --------------------------------------------------------------------------


async def test_c4_record_snapshot_run_persists_stats(db):
    await record_snapshot_run(
        db._conn,
        ran_at=_iso(NOW),
        stats=_stats(identities_seen=3, snapshots_written=2, provider_errors=1),
    )
    row = await _fetchone(
        db._conn,
        "SELECT ran_at, identities_seen, snapshots_written, provider_errors, "
        "pools_unresolved, empty_ohlcv FROM source_call_price_snapshot_runs",
    )
    assert row["identities_seen"] == 3
    assert row["snapshots_written"] == 2
    assert row["provider_errors"] == 1
    assert row["ran_at"] == NOW.isoformat()


# --------------------------------------------------------------------------
# C4c — writer_freshness
# --------------------------------------------------------------------------


async def test_c4_writer_freshness_no_runs_suppressed(db):
    findings = await evaluate_snapshot_watchdogs(db._conn, now=NOW)
    assert _finding(findings, "writer_freshness").status == "suppressed"


async def test_c4_writer_freshness_recent_ok(db):
    await record_snapshot_run(
        db._conn, ran_at=_iso(NOW - timedelta(minutes=5)), stats=_stats()
    )
    findings = await evaluate_snapshot_watchdogs(
        db._conn, now=NOW, writer_staleness_min=30
    )
    assert _finding(findings, "writer_freshness").status == "ok"


async def test_c4_writer_freshness_stale_alerts(db):
    await record_snapshot_run(
        db._conn, ran_at=_iso(NOW - timedelta(minutes=90)), stats=_stats()
    )
    findings = await evaluate_snapshot_watchdogs(
        db._conn, now=NOW, writer_staleness_min=30
    )
    assert _finding(findings, "writer_freshness").status == "alert"


# --------------------------------------------------------------------------
# C4c — fresh_calls_no_snapshots (writer alive but producing nothing)
# --------------------------------------------------------------------------


async def test_c4_fresh_calls_no_snapshots_alerts_when_writer_ran_empty(db):
    await _insert_eligible_call(
        db._conn, event_id="e1", ca="0xa", call_ts=_iso(NOW - timedelta(hours=1))
    )
    await record_snapshot_run(
        db._conn,
        ran_at=_iso(NOW - timedelta(minutes=5)),
        stats=_stats(identities_seen=1, snapshots_written=0),
    )
    findings = await evaluate_snapshot_watchdogs(db._conn, now=NOW)
    assert _finding(findings, "fresh_calls_no_snapshots").status == "alert"


async def test_c4_fresh_calls_no_snapshots_suppressed_when_writer_off(db):
    await _insert_eligible_call(
        db._conn, event_id="e1", ca="0xa", call_ts=_iso(NOW - timedelta(hours=1))
    )
    findings = await evaluate_snapshot_watchdogs(db._conn, now=NOW)  # no runs
    assert _finding(findings, "fresh_calls_no_snapshots").status == "suppressed"


# --------------------------------------------------------------------------
# C4c — eligible_no_snapshots (per-call coverage gap)
# --------------------------------------------------------------------------


async def test_c4_eligible_no_snapshots_counts_gap(db):
    await _insert_eligible_call(
        db._conn, event_id="e1", ca="0xAAA", call_ts=_iso(NOW - timedelta(hours=1))
    )
    await _insert_eligible_call(
        db._conn, event_id="e2", ca="0xBBB", call_ts=_iso(NOW - timedelta(hours=1))
    )
    await _insert_snapshot(
        db._conn,
        identity_key="base|0xaaa",
        snapshot_at=_iso(NOW - timedelta(minutes=50)),
    )
    await record_snapshot_run(
        db._conn,
        ran_at=_iso(NOW - timedelta(minutes=5)),
        stats=_stats(identities_seen=2, snapshots_written=1),
    )
    findings = await evaluate_snapshot_watchdogs(db._conn, now=NOW)
    f = _finding(findings, "eligible_no_snapshots")
    assert f.status == "alert"
    assert f.detail["count"] == 1  # 0xBBB has no snapshot


# --------------------------------------------------------------------------
# C4c — matured_all_null
# --------------------------------------------------------------------------


async def test_c4_matured_all_null_alerts(db):
    await _insert_eligible_call(
        db._conn,
        event_id="old",
        ca="0xc",
        call_ts=_iso(NOW - timedelta(hours=30)),
        outcome="unresolvable",
    )
    findings = await evaluate_snapshot_watchdogs(
        db._conn, now=NOW, matured_all_null_alert=1
    )
    f = _finding(findings, "matured_all_null")
    assert f.status == "alert"
    assert f.detail["count"] == 1


# --------------------------------------------------------------------------
# C4c — provider_error_spike
# --------------------------------------------------------------------------


async def test_c4_provider_error_spike_alerts(db):
    await record_snapshot_run(
        db._conn,
        ran_at=_iso(NOW - timedelta(minutes=5)),
        stats=_stats(identities_seen=4, provider_errors=3),
    )
    findings = await evaluate_snapshot_watchdogs(
        db._conn, now=NOW, provider_error_rate_alert=0.5
    )
    f = _finding(findings, "provider_error_spike")
    assert f.status == "alert"


async def test_c4_provider_error_spike_no_runs_suppressed(db):
    findings = await evaluate_snapshot_watchdogs(db._conn, now=NOW)
    assert _finding(findings, "provider_error_spike").status == "suppressed"
