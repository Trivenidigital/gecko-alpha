"""BL-NEW-CROSS-SURFACE-CONVICTION-SCORE: /api/conviction/shortlist endpoint tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.api import create_app
from scout.conviction import SURFACE_LEAD_COLUMNS
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
        # Yield the raw aiosqlite connection (Database exposes ._conn, not
        # .execute/.commit) so the insert helper writes directly.
        yield c, db[0]._conn
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


async def _insert_gc(
    conn, coin_id, early, peak_gain_pct, *, lead=2000.0, symbol=None, appeared=None
):
    """Insert a gainers_comparisons row with `early` surfaces confirming at
    `lead` minutes (others not detected). `appeared` overrides the appearance
    timestamp (for recency-sort tests)."""
    now = datetime.now(timezone.utc).isoformat()
    cols = [
        "coin_id",
        "symbol",
        "name",
        "appeared_on_gainers_at",
        "is_gap",
        "peak_gain_pct",
    ]
    vals = [
        coin_id,
        symbol or coin_id.upper(),
        coin_id,
        appeared or now,
        0,
        peak_gain_pct,
    ]
    for surface, lead_col in SURFACE_LEAD_COLUMNS.items():
        cols.append(f"detected_by_{surface}")
        vals.append(1 if surface in early else 0)
        cols.append(lead_col)
        vals.append(lead if surface in early else None)
    placeholders = ",".join("?" for _ in vals)
    await conn.execute(
        f"INSERT INTO gainers_comparisons ({','.join(cols)}) VALUES ({placeholders})",
        vals,
    )
    await conn.commit()


async def test_shortlist_ranks_high_conviction_first(client):
    c, conn = client
    await _insert_gc(conn, "low1", early=("chains",), peak_gain_pct=10)
    await _insert_gc(
        conn,
        "high1",
        early=("chains", "momentum", "slow_burn", "velocity"),
        peak_gain_pct=300,
    )
    resp = await c.get("/api/conviction/shortlist")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["read_only"] is True
    assert body["meta"]["not_trade_advice"] is True
    # honest-framing contract: retrospective + non-silent truncation disclosure
    assert body["meta"]["retrospective"] is True
    assert body["meta"]["total_tracked"] == 2
    assert body["meta"]["truncated"] is False
    rows = body["rows"]
    assert rows[0]["coin_id"] == "high1"
    assert rows[0]["tier"] == "high"
    assert rows[0]["early_count"] == 4
    assert set(rows[0]["contributing_surfaces"]) == {
        "chains",
        "momentum",
        "slow_burn",
        "velocity",
    }
    # the 1-surface coin ranks below and is 'low'
    assert rows[-1]["coin_id"] == "low1"
    assert rows[-1]["tier"] == "low"


async def test_shortlist_min_tier_filter(client):
    c, conn = client
    await _insert_gc(conn, "low1", early=("chains",), peak_gain_pct=10)
    await _insert_gc(conn, "watch1", early=("chains", "momentum"), peak_gain_pct=50)
    await _insert_gc(
        conn,
        "high1",
        early=("chains", "momentum", "slow_burn", "velocity"),
        peak_gain_pct=300,
    )
    resp = await c.get("/api/conviction/shortlist?min_tier=high")
    assert resp.status_code == 200
    ids = [r["coin_id"] for r in resp.json()["rows"]]
    assert ids == ["high1"]

    resp2 = await c.get("/api/conviction/shortlist?min_tier=watch")
    ids2 = {r["coin_id"] for r in resp2.json()["rows"]}
    assert ids2 == {"high1", "watch1"}  # low excluded


async def test_shortlist_excludes_late_confirmations(client):
    # A coin with 4 surfaces but all confirming LATE (lead below threshold) must
    # NOT be high-tier — the score is about EARLY confirmation.
    c, conn = client
    await _insert_gc(
        conn,
        "late",
        early=("chains", "momentum", "slow_burn", "velocity"),
        peak_gain_pct=20,
        lead=60.0,  # only 1h early, < 1440 threshold
    )
    resp = await c.get("/api/conviction/shortlist")
    rows = resp.json()["rows"]
    assert rows[0]["coin_id"] == "late"
    assert rows[0]["early_count"] == 0
    assert rows[0]["tier"] == "low"


async def test_shortlist_empty_db(client):
    c, _ = client
    resp = await c.get("/api/conviction/shortlist")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert body["meta"]["read_only"] is True


async def test_shortlist_invalid_min_tier_rejected(client):
    c, _ = client
    resp = await c.get("/api/conviction/shortlist?min_tier=bogus")
    assert resp.status_code == 422  # pattern validation


async def test_shortlist_sort_recency_vs_score(client):
    c, conn = client
    # "older" = lower conviction but earlier appearance; "newer" = higher score.
    await _insert_gc(
        conn,
        "older",
        early=("chains", "momentum", "slow_burn", "velocity", "spikes"),
        peak_gain_pct=400,
        appeared="2026-06-01T00:00:00+00:00",
    )
    await _insert_gc(
        conn,
        "newer",
        early=("chains", "momentum", "slow_burn", "velocity"),
        peak_gain_pct=50,
        appeared="2026-06-10T00:00:00+00:00",
    )
    # sort=score → higher early_count first
    by_score = (await c.get("/api/conviction/shortlist?sort=score")).json()
    assert [r["coin_id"] for r in by_score["rows"]] == ["older", "newer"]
    assert by_score["meta"]["sort"] == "score"
    # sort=recency → newest appearance first
    by_recency = (await c.get("/api/conviction/shortlist?sort=recency")).json()
    assert [r["coin_id"] for r in by_recency["rows"]] == ["newer", "older"]
    assert by_recency["meta"]["sort"] == "recency"


async def test_shortlist_invalid_sort_rejected(client):
    c, _ = client
    resp = await c.get("/api/conviction/shortlist?sort=bogus")
    assert resp.status_code == 422
