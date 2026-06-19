"""/api/conviction/prospective endpoint (Task 7). Run-keyed; mcap rules."""

from __future__ import annotations

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
    _, db_path = db
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


def _row(coin_id, mcap, mcap_age, *, tier="high", early=4):
    return {
        "coin_id": coin_id,
        "symbol": coin_id.upper(),
        "name": coin_id,
        "early_count": early,
        "fresh_count": 0,
        "tier": tier,
        "contributing_surfaces": ["chains", "spikes", "momentum", "velocity"],
        "market_cap": mcap,
        "mcap_age_minutes": mcap_age,
        "first_detection_ages": {},
    }


async def _seed_run(d, snapshot_at, rows, status="ok"):
    if rows:
        await d.insert_conviction_watchlist_snapshot(rows, snapshot_at)
    await d.insert_conviction_watchlist_run(
        {
            "run_at": snapshot_at,
            "status": status,
            "rows_written": len(rows),
            "high_tier": sum(1 for r in rows if r["tier"] == "high"),
            "sub30m_high_fresh": 0,
            "per_surface_contrib": {},
            "truncated": False,
        }
    )


async def test_empty_when_never_run(client):
    body = (await client.get("/api/conviction/prospective")).json()
    assert body["meta"]["snapshot_at"] is None
    assert body["meta"]["run_status"] is None
    assert body["meta"]["observe_only"] is True
    assert body["meta"]["calibration"] == "prospective_unvalidated"
    # UI-contract fold: live gate/lead config surfaced (no hardcoded UI fallback)
    assert body["meta"]["high_tier_min_surfaces"] == 4
    assert body["meta"]["early_lead_minutes"] == 1440
    assert body["rows"] == [] and body["mcap_unknown"] == []


async def test_run_status_surfaced_in_meta(client, db):
    """P2 fold: a degraded / failed build's status is surfaced in meta so the UI
    can mark the batch incomplete instead of rendering it as healthy-but-empty."""
    d, _ = db
    await _seed_run(
        d,
        "2026-06-19T00:00:00+00:00",
        [_row("pepe", 12_000_000.0, 10.0)],
        status="degraded_surface_failed",
    )
    body = (await client.get("/api/conviction/prospective")).json()
    assert body["meta"]["run_status"] == "degraded_surface_failed"
    assert [r["coin_id"] for r in body["rows"]] == ["pepe"]  # rows still surfaced


async def test_high_tier_sub30m_in_rows(client, db):
    d, _ = db
    await _seed_run(d, "2026-06-19T00:00:00+00:00", [_row("pepe", 12_000_000.0, 10.0)])
    body = (await client.get("/api/conviction/prospective")).json()
    assert body["meta"]["snapshot_at"] == "2026-06-19T00:00:00+00:00"
    assert [r["coin_id"] for r in body["rows"]] == ["pepe"]


async def test_stale_mcap_goes_to_mcap_unknown(client, db):
    d, _ = db
    # mcap_age 5000 > 1440 default max-age → stale → mcap_unknown, NOT a sub-$30M hit
    await _seed_run(
        d, "2026-06-19T00:00:00+00:00", [_row("stale", 9_000_000.0, 5000.0)]
    )
    body = (await client.get("/api/conviction/prospective")).json()
    assert body["rows"] == []
    assert [r["coin_id"] for r in body["mcap_unknown"]] == ["stale"]


async def test_null_mcap_goes_to_mcap_unknown(client, db):
    d, _ = db
    await _seed_run(d, "2026-06-19T00:00:00+00:00", [_row("nomc", None, None)])
    body = (await client.get("/api/conviction/prospective")).json()
    assert body["rows"] == []
    assert [r["coin_id"] for r in body["mcap_unknown"]] == ["nomc"]


async def test_known_large_mcap_excluded(client, db):
    d, _ = db
    await _seed_run(d, "2026-06-19T00:00:00+00:00", [_row("big", 50_000_000.0, 10.0)])
    body = (await client.get("/api/conviction/prospective")).json()
    assert body["rows"] == [] and body["mcap_unknown"] == []  # known >$30M, not shown


async def test_min_tier_filter_excludes_watch(client, db):
    d, _ = db
    await _seed_run(
        d,
        "2026-06-19T00:00:00+00:00",
        [_row("w", 5_000_000.0, 10.0, tier="watch", early=2)],
    )
    high = (await client.get("/api/conviction/prospective?min_tier=high")).json()
    assert high["rows"] == []
    watch = (await client.get("/api/conviction/prospective?min_tier=watch")).json()
    assert [r["coin_id"] for r in watch["rows"]] == ["w"]


async def test_healthy_zero_row_run_reports_empty_not_prior_batch(client, db):
    """Regression: T0 has rows, T1 is healthy status=ok rows_written=0 → the API
    must report T1 freshness/status and rows=[] (NOT leak the T0 batch)."""
    d, _ = db
    await _seed_run(d, "2026-06-19T00:00:00+00:00", [_row("pepe", 12_000_000.0, 10.0)])
    await _seed_run(d, "2026-06-19T01:00:00+00:00", [])  # healthy 0-row T1
    body = (await client.get("/api/conviction/prospective")).json()
    assert body["meta"]["snapshot_at"] == "2026-06-19T01:00:00+00:00"  # T1 freshness
    assert body["meta"]["total_in_batch"] == 0
    assert body["rows"] == []
    assert body["mcap_unknown"] == []
