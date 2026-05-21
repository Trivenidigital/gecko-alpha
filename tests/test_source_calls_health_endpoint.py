"""Tests for /api/source_calls/health — read-only aggregate ledger health.

Per BL-NEW-DASHBOARD-SOURCE-CALL-HEALTH operator gates:
  - NO per-source ranking exposed
  - "not rankable yet" label communicates the gate honestly
  - returns gracefully on fresh DB (schema_missing path)

Tests use the same fixture pattern as tests/test_x_alerts_dashboard.py.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.api import create_app
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    d = Database(db_path)
    await d.initialize()
    yield d, str(db_path)
    await d.close()


@pytest.fixture
async def client(db):
    import dashboard.api as api_mod

    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None
    _d, db_path = db
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


async def _insert_source_call(
    conn,
    *,
    source_type: str = "tg",
    source_id: str = "@handle",
    source_event_id: str,
    observed_at: str | None = None,
    outcome_status: str | None = None,
    duplicate_rank_in_cluster: int = 1,
    duplicate_cluster_key: str | None = None,
    default_outcome_status: str = "complete",
    price_at_call: float | None = None,
    forward_30m_pct: float | None = None,
    forward_1h_pct: float | None = None,
    forward_6h_pct: float | None = None,
    forward_24h_pct: float | None = None,
):
    if observed_at is None:
        observed_at = datetime.now(timezone.utc).isoformat()
    if duplicate_cluster_key is None:
        duplicate_cluster_key = source_event_id
    await conn.execute(
        """INSERT INTO source_calls (
               source_type, source_id, source_event_id, call_ts, call_kind,
               cluster_identity, cluster_identity_kind, resolved_state,
               missing_fields,
               observed_at, outcome_status,
               duplicate_rank_in_cluster, duplicate_cluster_key,
               price_at_call, forward_30m_pct, forward_1h_pct,
               forward_6h_pct, forward_24h_pct
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source_type, source_id, source_event_id, observed_at,
            "first_mention",
            f"test-{source_event_id}", "source_event", "resolved",
            "[]",
            observed_at, outcome_status if outcome_status is not None else default_outcome_status,
            duplicate_rank_in_cluster, duplicate_cluster_key,
            price_at_call, forward_30m_pct, forward_1h_pct,
            forward_6h_pct, forward_24h_pct,
        ),
    )
    await conn.commit()


async def test_health_endpoint_returns_zero_state_on_empty_ledger(client):
    """Empty source_calls table → row_count=0, null rates, honest label."""
    resp = await client.get("/api/source_calls/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_count"] == 0
    assert body["row_count_by_source_type"] == {"tg": 0, "x": 0}
    assert body["unresolvable_rate"] is None
    assert body["duplicate_rate"] is None
    assert body["outcome_status_counts"] == {}
    assert body["price_coverage"]["with_price_at_call"] == 0
    assert body["rankability"]["source_count"] == 0
    assert body["rankability"]["rankable"] == 0
    assert "no source_calls rows yet" in body["rankability"]["not_rankable_label"]


async def test_health_endpoint_returns_aggregate_stats(client, db):
    d, _db_path = db
    now = datetime.now(timezone.utc)

    # 3 rows, mixed sources, mixed coverage
    await _insert_source_call(
        d._conn,
        source_type="tg",
        source_id="@alpha",
        source_event_id="tg-1",
        observed_at=now.isoformat(),
        outcome_status="complete",
        price_at_call=1.0,
        forward_30m_pct=2.0,
    )
    await _insert_source_call(
        d._conn,
        source_type="tg",
        source_id="@alpha",
        source_event_id="tg-2",
        observed_at=(now - timedelta(hours=1)).isoformat(),
        outcome_status="unresolvable",
    )
    await _insert_source_call(
        d._conn,
        source_type="x",
        source_id="@beta",
        source_event_id="x-1",
        observed_at=(now - timedelta(hours=2)).isoformat(),
        outcome_status="complete",
        price_at_call=0.5,
        forward_30m_pct=-1.0,
        forward_1h_pct=3.0,
    )

    resp = await client.get("/api/source_calls/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_count"] == 3
    assert body["row_count_by_source_type"] == {"tg": 2, "x": 1}
    assert body["unresolvable_rate"] == round(1 / 3, 4)
    assert body["duplicate_rate"] == 0.0  # no duplicates inserted
    assert body["outcome_status_counts"]["complete"] == 2
    assert body["outcome_status_counts"]["unresolvable"] == 1
    assert body["price_coverage"]["with_price_at_call"] == 2
    assert body["price_coverage"]["with_forward_30m_pct"] == 2
    assert body["price_coverage"]["with_forward_1h_pct"] == 1
    assert body["price_coverage"]["with_forward_6h_pct"] == 0
    assert body["price_coverage"]["with_forward_24h_pct"] == 0


async def test_health_endpoint_does_not_expose_per_source_ranking(client, db):
    """Operator gate: rankability rollup must NOT contain per-source data."""
    d, _db_path = db
    now = datetime.now(timezone.utc)

    # Insert 15 rows for one source @alpha — enough to hit min_sample=10
    for i in range(15):
        await _insert_source_call(
            d._conn,
            source_type="tg",
            source_id="@alpha",
            source_event_id=f"tg-{i}",
            observed_at=(now - timedelta(minutes=i * 5)).isoformat(),
            duplicate_cluster_key=f"cluster-{i}",
            duplicate_rank_in_cluster=1,
            forward_30m_pct=2.0 + i,  # all eligible_clusters
        )

    resp = await client.get("/api/source_calls/health")
    assert resp.status_code == 200
    body = resp.json()

    # Rankability should expose ROLLUP counts only — no source identifiers.
    rk = body["rankability"]
    assert isinstance(rk["source_count"], int)
    assert isinstance(rk["rankable"], int)
    assert isinstance(rk["insufficient_sample"], int)
    assert isinstance(rk["biased_low_coverage"], int)
    assert isinstance(rk["not_rankable_label"], str)
    # The rollup must NOT mention @alpha (source-id leak guard)
    flat = str(body)
    assert "@alpha" not in flat, (
        "rankability rollup must not expose per-source ids; the operator gate "
        "deliberately blocks source ranking until BL-NEW-DASHBOARD-SOURCE-"
        "CALL-QUALITY-SURFACE design"
    )


async def test_health_endpoint_not_rankable_label_explains_gate(client, db):
    """When insufficient_sample > 0 and rankable == 0, label must say so honestly."""
    d, _db_path = db
    now = datetime.now(timezone.utc)

    # Insert 3 rows from one source — far below min_sample=10
    for i in range(3):
        await _insert_source_call(
            d._conn,
            source_type="tg",
            source_id="@gamma",
            source_event_id=f"tg-{i}",
            observed_at=(now - timedelta(minutes=i * 5)).isoformat(),
            duplicate_cluster_key=f"c-{i}",
            duplicate_rank_in_cluster=1,
            forward_30m_pct=2.0,
        )

    resp = await client.get("/api/source_calls/health")
    body = resp.json()

    rk = body["rankability"]
    assert rk["source_count"] >= 1
    assert rk["rankable"] == 0
    assert rk["insufficient_sample"] >= 1
    label = rk["not_rankable_label"]
    assert "no sources rankable yet" in label
    assert "min_sample=10" in label


async def test_health_endpoint_writer_freshness_present(client, db):
    """Writer freshness surfaces max(observed_at) + minutes-since."""
    d, _db_path = db
    now = datetime.now(timezone.utc)

    await _insert_source_call(
        d._conn,
        source_type="tg",
        source_id="@alpha",
        source_event_id="tg-fresh",
        observed_at=(now - timedelta(minutes=5)).isoformat(),
    )
    await _insert_source_call(
        d._conn,
        source_type="tg",
        source_id="@alpha",
        source_event_id="tg-stale",
        observed_at=(now - timedelta(hours=2)).isoformat(),
    )

    resp = await client.get("/api/source_calls/health")
    body = resp.json()
    wf = body["writer_freshness"]
    assert wf["max_observed_at"] is not None
    # Most-recent should be ~5 minutes ago (with some tolerance)
    assert 0 <= wf["minutes_since_last_observed"] <= 20
    assert wf["lag_threshold_minutes"] == 30
