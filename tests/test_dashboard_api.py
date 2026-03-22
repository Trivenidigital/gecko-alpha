"""Tests for dashboard REST API endpoints.

Uses httpx.AsyncClient + pytest-asyncio with a tmp_path SQLite fixture
seeded with sample data. WebSocket testing is manual only.
"""

import json
from datetime import datetime, timezone

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def seeded_db(tmp_path):
    """Create and seed a test database, return its path."""
    db_path = str(tmp_path / "test_dashboard.db")
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE candidates (
                contract_address TEXT PRIMARY KEY,
                chain            TEXT NOT NULL,
                token_name       TEXT NOT NULL,
                ticker           TEXT NOT NULL,
                token_age_days   REAL    DEFAULT 0,
                market_cap_usd   REAL    DEFAULT 0,
                liquidity_usd    REAL    DEFAULT 0,
                volume_24h_usd   REAL    DEFAULT 0,
                holder_count     INTEGER DEFAULT 0,
                holder_growth_1h INTEGER DEFAULT 0,
                social_mentions_24h INTEGER DEFAULT 0,
                quant_score      INTEGER,
                narrative_score  INTEGER,
                conviction_score REAL,
                mirofish_report  TEXT,
                virality_class   TEXT,
                signals_fired    TEXT,
                alerted_at       TEXT,
                first_seen_at    TEXT NOT NULL
            );

            CREATE TABLE alerts (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address  TEXT NOT NULL,
                chain             TEXT NOT NULL,
                conviction_score  REAL NOT NULL,
                alerted_at        TEXT NOT NULL
            );

            CREATE TABLE mirofish_jobs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address  TEXT NOT NULL,
                created_at        TEXT NOT NULL
            );
        """)

        now = datetime.now(timezone.utc).isoformat()

        # Seed candidates
        await db.execute(
            """INSERT INTO candidates
            (contract_address, chain, token_name, ticker, market_cap_usd, liquidity_usd,
             volume_24h_usd, quant_score, conviction_score, signals_fired, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("0xhigh", "solana", "HighToken", "HIGH", 50000, 20000,
             120000, 85, 82.0, json.dumps(["vol_liq_ratio", "holder_growth", "market_cap_range"]), now),
        )
        await db.execute(
            """INSERT INTO candidates
            (contract_address, chain, token_name, ticker, market_cap_usd, liquidity_usd,
             volume_24h_usd, quant_score, conviction_score, signals_fired, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("0xlow", "ethereum", "LowToken", "LOW", 30000, 15000,
             40000, 25, 25.0, json.dumps(["market_cap_range"]), now),
        )
        await db.execute(
            """INSERT INTO candidates
            (contract_address, chain, token_name, ticker, market_cap_usd, liquidity_usd,
             volume_24h_usd, quant_score, conviction_score, signals_fired, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("0xnone", "solana", "NoSignals", "NONE", 10000, 16000,
             5000, 5, None, None, now),
        )

        # Seed alerts
        await db.execute(
            "INSERT INTO alerts (contract_address, chain, conviction_score, alerted_at) VALUES (?, ?, ?, ?)",
            ("0xhigh", "solana", 82.0, now),
        )
        await db.execute(
            "INSERT INTO alerts (contract_address, chain, conviction_score, alerted_at) VALUES (?, ?, ?, ?)",
            ("0xalert2", "base", 75.0, "2026-03-22T10:00:00+00:00"),
        )

        # Seed mirofish jobs
        await db.execute(
            "INSERT INTO mirofish_jobs (contract_address, created_at) VALUES (?, ?)",
            ("0xhigh", now),
        )

        await db.commit()

    return db_path


@pytest.fixture
async def empty_db(tmp_path):
    """Create an empty database with schema only."""
    db_path = str(tmp_path / "empty_dashboard.db")
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE candidates (
                contract_address TEXT PRIMARY KEY,
                chain TEXT NOT NULL, token_name TEXT NOT NULL, ticker TEXT NOT NULL,
                token_age_days REAL DEFAULT 0, market_cap_usd REAL DEFAULT 0,
                liquidity_usd REAL DEFAULT 0, volume_24h_usd REAL DEFAULT 0,
                holder_count INTEGER DEFAULT 0, holder_growth_1h INTEGER DEFAULT 0,
                social_mentions_24h INTEGER DEFAULT 0,
                quant_score INTEGER, narrative_score INTEGER, conviction_score REAL,
                mirofish_report TEXT, virality_class TEXT, signals_fired TEXT,
                alerted_at TEXT, first_seen_at TEXT NOT NULL
            );
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL, chain TEXT NOT NULL,
                conviction_score REAL NOT NULL, alerted_at TEXT NOT NULL
            );
            CREATE TABLE mirofish_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)
    return db_path


@pytest.fixture
def make_app():
    """Factory that creates a FastAPI app pointing at a given DB path."""
    def _make(db_path: str):
        from dashboard.api import create_app
        return create_app(db_path)
    return _make


@pytest.fixture
async def client(seeded_db, make_app):
    app = make_app(seeded_db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def empty_client(empty_db, make_app):
    app = make_app(empty_db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# GET /api/candidates
# ---------------------------------------------------------------------------

class TestCandidates:

    async def test_returns_candidates_ordered_by_conviction(self, client):
        resp = await client.get("/api/candidates")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        # Ordered by conviction_score DESC — None sorts last
        assert data[0]["contract_address"] == "0xhigh"
        assert data[0]["conviction_score"] == 82.0
        assert data[1]["contract_address"] == "0xlow"

    async def test_signals_fired_parsed_as_list(self, client):
        resp = await client.get("/api/candidates")
        data = resp.json()
        high = data[0]
        assert isinstance(high["signals_fired"], list)
        assert "vol_liq_ratio" in high["signals_fired"]

    async def test_signals_fired_null_returns_empty_list(self, client):
        resp = await client.get("/api/candidates")
        data = resp.json()
        none_token = [c for c in data if c["contract_address"] == "0xnone"][0]
        assert none_token["signals_fired"] == []

    async def test_empty_db_returns_empty_list(self, empty_client):
        resp = await empty_client.get("/api/candidates")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/alerts/recent
# ---------------------------------------------------------------------------

class TestAlerts:

    async def test_returns_alerts_ordered_by_time(self, client):
        resp = await client.get("/api/alerts/recent")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Most recent first
        assert data[0]["contract_address"] == "0xhigh"

    async def test_empty_db_returns_empty_list(self, empty_client):
        resp = await empty_client.get("/api/alerts/recent")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/signals/today
# ---------------------------------------------------------------------------

class TestSignals:

    async def test_returns_signal_hit_rates(self, client):
        resp = await client.get("/api/signals/today")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # Should have entries for known signals
        signal_names = [s["signal_name"] for s in data]
        assert "vol_liq_ratio" in signal_names
        # vol_liq_ratio fired once (in 0xhigh)
        vlr = [s for s in data if s["signal_name"] == "vol_liq_ratio"][0]
        assert vlr["fired_count"] == 1

    async def test_empty_db_returns_zeros(self, empty_client):
        resp = await empty_client.get("/api/signals/today")
        assert resp.status_code == 200
        data = resp.json()
        for s in data:
            assert s["fired_count"] == 0


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------

class TestStatus:

    async def test_returns_status(self, client):
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "mirofish_jobs_today" in data
        assert data["mirofish_jobs_today"] == 1
        assert "pipeline_status" in data

    async def test_empty_db_returns_status(self, empty_client):
        resp = await empty_client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mirofish_jobs_today"] == 0


# ---------------------------------------------------------------------------
# GET /api/funnel/latest
# ---------------------------------------------------------------------------

class TestFunnel:

    async def test_returns_funnel_counts(self, client):
        resp = await client.get("/api/funnel/latest")
        assert resp.status_code == 200
        data = resp.json()
        assert "ingested" in data
        assert "alerted" in data

    async def test_empty_db_returns_zeros(self, empty_client):
        resp = await empty_client.get("/api/funnel/latest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["alerted"] == 0
