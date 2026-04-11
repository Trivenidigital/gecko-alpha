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
            CREATE TABLE category_snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id   TEXT NOT NULL,
                name          TEXT NOT NULL,
                market_cap    REAL,
                market_cap_change_24h REAL,
                volume_24h    REAL,
                market_regime TEXT,
                snapshot_at   TEXT NOT NULL
            );

            CREATE TABLE predictions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT,
                category_id     TEXT,
                fit_score       REAL,
                confidence      REAL,
                price_change_6h REAL,
                price_change_24h REAL,
                price_change_48h REAL,
                peak_change_pct REAL,
                outcome_class   TEXT,
                is_control      INTEGER DEFAULT 0,
                predicted_at    TEXT NOT NULL
            );

            CREATE TABLE agent_strategy (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                locked      INTEGER DEFAULT 0,
                updated_by  TEXT,
                updated_at  TEXT
            );

            CREATE TABLE learn_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                message     TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

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
                alert_market_cap  REAL,
                alerted_at        TEXT NOT NULL
            );

            CREATE TABLE outcomes (
                id                INTEGER PRIMARY KEY,
                contract_address  TEXT NOT NULL,
                alert_price       REAL,
                check_price       REAL,
                check_time        TEXT,
                price_change_pct  REAL
            );

            CREATE TABLE mirofish_jobs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address  TEXT NOT NULL,
                created_at        TEXT NOT NULL
            );

            CREATE TABLE signal_events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id       TEXT NOT NULL,
                pipeline       TEXT NOT NULL,
                event_type     TEXT NOT NULL,
                event_data     TEXT NOT NULL,
                source_module  TEXT NOT NULL,
                created_at     TEXT NOT NULL
            );

            CREATE TABLE chain_patterns (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                name                 TEXT NOT NULL UNIQUE,
                description          TEXT NOT NULL,
                steps_json           TEXT NOT NULL,
                min_steps_to_trigger INTEGER NOT NULL,
                conviction_boost     INTEGER NOT NULL DEFAULT 0,
                alert_priority       TEXT NOT NULL DEFAULT 'low',
                is_active            INTEGER NOT NULL DEFAULT 1,
                historical_hit_rate  REAL,
                total_triggers       INTEGER DEFAULT 0,
                total_hits           INTEGER DEFAULT 0,
                created_at           TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE active_chains (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id       TEXT NOT NULL,
                pipeline       TEXT NOT NULL,
                pattern_id     INTEGER NOT NULL,
                pattern_name   TEXT NOT NULL,
                steps_matched  TEXT NOT NULL,
                step_events    TEXT NOT NULL,
                anchor_time    TEXT NOT NULL,
                last_step_time TEXT NOT NULL,
                is_complete    INTEGER DEFAULT 0,
                completed_at   TEXT,
                created_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE chain_matches (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id             TEXT NOT NULL,
                pipeline             TEXT NOT NULL,
                pattern_id           INTEGER NOT NULL,
                pattern_name         TEXT NOT NULL,
                steps_matched        INTEGER NOT NULL,
                total_steps          INTEGER NOT NULL,
                anchor_time          TEXT NOT NULL,
                completed_at         TEXT NOT NULL,
                chain_duration_hours REAL NOT NULL,
                conviction_boost     INTEGER NOT NULL,
                outcome_class        TEXT,
                outcome_change_pct   REAL,
                evaluated_at         TEXT,
                created_at           TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE narrative_signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE second_wave_candidates (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address         TEXT NOT NULL,
                detected_at              TEXT NOT NULL,
                reaccumulation_score     INTEGER NOT NULL
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

        # Seed category snapshots
        await db.execute(
            """INSERT INTO category_snapshots
            (category_id, name, market_cap, market_cap_change_24h, volume_24h, market_regime, snapshot_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("ai", "AI & Big Data", 5e9, 12.5, 8e8, "HEATING", now),
        )
        await db.execute(
            """INSERT INTO category_snapshots
            (category_id, name, market_cap, market_cap_change_24h, volume_24h, market_regime, snapshot_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("defi", "DeFi", 20e9, -3.2, 2e9, "COOLING", now),
        )

        # Seed predictions
        await db.execute(
            """INSERT INTO predictions
            (symbol, category_id, fit_score, confidence, price_change_6h, price_change_24h,
             price_change_48h, peak_change_pct, outcome_class, is_control, predicted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("AGIXT", "ai", 82, 75, 5.2, 12.1, 18.3, 22.5, "HIT", 0, now),
        )
        await db.execute(
            """INSERT INTO predictions
            (symbol, category_id, fit_score, confidence, price_change_6h, price_change_24h,
             price_change_48h, peak_change_pct, outcome_class, is_control, predicted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("CTRLTOKEN", "ai", 60, 50, -1.0, -5.0, -8.0, 2.0, "MISS", 1, now),
        )
        await db.execute(
            """INSERT INTO predictions
            (symbol, category_id, fit_score, confidence, outcome_class, is_control, predicted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("PENDING", "defi", 70, 65, None, 0, now),
        )

        # Seed agent_strategy
        await db.execute(
            "INSERT INTO agent_strategy (key, value, locked, updated_by) VALUES (?, ?, ?, ?)",
            ("top_n", "5", 0, "agent"),
        )
        await db.execute(
            "INSERT INTO agent_strategy (key, value, locked, updated_by) VALUES (?, ?, ?, ?)",
            ("lookback_hours", "48", 0, "agent"),
        )

        # Seed learn_logs
        await db.execute(
            "INSERT INTO learn_logs (message, created_at) VALUES (?, ?)",
            ("Learned that AI category outperforms in bull markets", now),
        )

        # Seed chain_patterns
        await db.execute(
            """INSERT INTO chain_patterns
            (id, name, description, steps_json, min_steps_to_trigger,
             conviction_boost, alert_priority, is_active, total_triggers, total_hits)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (1, "test_pattern", "Test chain pattern",
             json.dumps(["step_a", "step_b"]), 2, 15, "medium", 1, 10, 4),
        )

        # Seed active_chains
        await db.execute(
            """INSERT INTO active_chains
            (token_id, pipeline, pattern_id, pattern_name, steps_matched,
             step_events, anchor_time, last_step_time, is_complete)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("0xhigh", "narrative", 1, "test_pattern",
             json.dumps(["step_a"]), json.dumps([{"t": now, "step": "step_a"}]),
             now, now, 0),
        )

        # Seed signal_events
        await db.execute(
            """INSERT INTO signal_events
            (token_id, pipeline, event_type, event_data, source_module, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            ("0xhigh", "narrative", "momentum_spike", "{}", "scorer", now),
        )

        # Seed chain_matches
        await db.execute(
            """INSERT INTO chain_matches
            (token_id, pipeline, pattern_id, pattern_name, steps_matched,
             total_steps, anchor_time, completed_at, chain_duration_hours,
             conviction_boost, outcome_class)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("0xhigh", "narrative", 1, "test_pattern", 2, 2, now, now, 1.5, 15, "HIT"),
        )

        # Seed second_wave_candidates
        await db.execute(
            """INSERT INTO second_wave_candidates
            (contract_address, detected_at, reaccumulation_score)
            VALUES (?, ?, ?)""",
            ("0xsw", now, 72),
        )

        # Seed narrative_signals
        await db.execute(
            "INSERT INTO narrative_signals (created_at) VALUES (?)",
            (now,),
        )

        await db.commit()

    return db_path


@pytest.fixture
async def empty_db(tmp_path):
    """Create an empty database with schema only."""
    db_path = str(tmp_path / "empty_dashboard.db")
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE category_snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id   TEXT NOT NULL,
                name          TEXT NOT NULL,
                market_cap    REAL,
                market_cap_change_24h REAL,
                volume_24h    REAL,
                market_regime TEXT,
                snapshot_at   TEXT NOT NULL
            );
            CREATE TABLE predictions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT,
                category_id     TEXT,
                fit_score       REAL,
                confidence      REAL,
                price_change_6h REAL,
                price_change_24h REAL,
                price_change_48h REAL,
                peak_change_pct REAL,
                outcome_class   TEXT,
                is_control      INTEGER DEFAULT 0,
                predicted_at    TEXT NOT NULL
            );
            CREATE TABLE agent_strategy (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                locked      INTEGER DEFAULT 0,
                updated_by  TEXT,
                updated_at  TEXT
            );
            CREATE TABLE learn_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                message     TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
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
                conviction_score REAL NOT NULL, alert_market_cap REAL,
                alerted_at TEXT NOT NULL
            );
            CREATE TABLE outcomes (
                id INTEGER PRIMARY KEY,
                contract_address TEXT NOT NULL, alert_price REAL,
                check_price REAL, check_time TEXT, price_change_pct REAL
            );
            CREATE TABLE mirofish_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE signal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT NOT NULL, pipeline TEXT NOT NULL,
                event_type TEXT NOT NULL, event_data TEXT NOT NULL,
                source_module TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE chain_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE, description TEXT NOT NULL,
                steps_json TEXT NOT NULL, min_steps_to_trigger INTEGER NOT NULL,
                conviction_boost INTEGER NOT NULL DEFAULT 0,
                alert_priority TEXT NOT NULL DEFAULT 'low',
                is_active INTEGER NOT NULL DEFAULT 1,
                historical_hit_rate REAL,
                total_triggers INTEGER DEFAULT 0, total_hits INTEGER DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE active_chains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT NOT NULL, pipeline TEXT NOT NULL,
                pattern_id INTEGER NOT NULL, pattern_name TEXT NOT NULL,
                steps_matched TEXT NOT NULL, step_events TEXT NOT NULL,
                anchor_time TEXT NOT NULL, last_step_time TEXT NOT NULL,
                is_complete INTEGER DEFAULT 0, completed_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE chain_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT NOT NULL, pipeline TEXT NOT NULL,
                pattern_id INTEGER NOT NULL, pattern_name TEXT NOT NULL,
                steps_matched INTEGER NOT NULL, total_steps INTEGER NOT NULL,
                anchor_time TEXT NOT NULL, completed_at TEXT NOT NULL,
                chain_duration_hours REAL NOT NULL, conviction_boost INTEGER NOT NULL,
                outcome_class TEXT, outcome_change_pct REAL, evaluated_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE narrative_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL
            );
            CREATE TABLE second_wave_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL, detected_at TEXT NOT NULL,
                reaccumulation_score INTEGER NOT NULL
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


# ---------------------------------------------------------------------------
# GET /api/narrative/heating
# ---------------------------------------------------------------------------

class TestNarrativeHeating:

    async def test_returns_heating_categories(self, client):
        resp = await client.get("/api/narrative/heating")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Ordered by market_cap_change_24h DESC
        assert data[0]["category_id"] == "ai"
        assert data[0]["market_cap_change_24h"] == 12.5

    async def test_empty_db_returns_empty(self, empty_client):
        resp = await empty_client.get("/api/narrative/heating")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/narrative/predictions
# ---------------------------------------------------------------------------

class TestNarrativePredictions:

    async def test_returns_predictions(self, client):
        resp = await client.get("/api/narrative/predictions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3

    async def test_filter_by_outcome(self, client):
        resp = await client.get("/api/narrative/predictions?outcome=HIT")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "AGIXT"

    async def test_limit_param(self, client):
        resp = await client.get("/api/narrative/predictions?limit=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1

    async def test_empty_db_returns_empty(self, empty_client):
        resp = await empty_client.get("/api/narrative/predictions")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/narrative/metrics
# ---------------------------------------------------------------------------

class TestNarrativeMetrics:

    async def test_returns_metrics(self, client):
        resp = await client.get("/api/narrative/metrics")
        assert resp.status_code == 200
        data = resp.json()
        # 1 agent HIT out of 1 resolved agent prediction = 100%
        assert data["agent_hit_rate"] == 100.0
        # 0 ctrl HITs out of 1 resolved ctrl prediction = 0%
        assert data["ctrl_hit_rate"] == 0.0
        assert data["true_alpha"] == 100.0
        assert data["total_predictions"] == 3
        # 1 unresolved (PENDING has outcome_class=None)
        assert data["active_predictions"] == 1

    async def test_empty_db_returns_zeros(self, empty_client):
        resp = await empty_client.get("/api/narrative/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_hit_rate"] == 0
        assert data["true_alpha"] == 0
        assert data["total_predictions"] == 0


# ---------------------------------------------------------------------------
# GET /api/narrative/strategy + PUT
# ---------------------------------------------------------------------------

class TestNarrativeStrategy:

    async def test_returns_strategy_rows(self, client):
        resp = await client.get("/api/narrative/strategy")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        keys = [r["key"] for r in data]
        assert "top_n" in keys

    async def test_update_strategy(self, client):
        resp = await client.put(
            "/api/narrative/strategy/top_n",
            json={"value": "10"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["value"] == "10"
        assert data["locked"] == 1
        assert data["updated_by"] == "manual"

    async def test_update_nonexistent_key_returns_404(self, client):
        resp = await client.put(
            "/api/narrative/strategy/nonexistent",
            json={"value": "x"},
        )
        assert resp.status_code == 404

    async def test_empty_db_returns_empty(self, empty_client):
        resp = await empty_client.get("/api/narrative/strategy")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/narrative/learn-logs
# ---------------------------------------------------------------------------

class TestNarrativeLearnLogs:

    async def test_returns_learn_logs(self, client):
        resp = await client.get("/api/narrative/learn-logs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert "AI category" in data[0]["message"]

    async def test_empty_db_returns_empty(self, empty_client):
        resp = await empty_client.get("/api/narrative/learn-logs")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/narrative/categories/history
# ---------------------------------------------------------------------------

class TestNarrativeCategoryHistory:

    async def test_returns_history(self, client):
        resp = await client.get("/api/narrative/categories/history?category_id=ai")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["category_id"] == "ai"

    async def test_unknown_category_returns_empty(self, client):
        resp = await client.get("/api/narrative/categories/history?category_id=unknown")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_empty_db_returns_empty(self, empty_client):
        resp = await empty_client.get("/api/narrative/categories/history?category_id=ai")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/chains/*
# ---------------------------------------------------------------------------

class TestChains:

    async def test_chains_active_endpoint(self, client):
        resp = await client.get("/api/chains/active")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["token_id"] == "0xhigh"
        assert data[0]["pipeline"] == "narrative"
        assert isinstance(data[0]["steps_matched"], list)
        assert isinstance(data[0]["step_events"], list)

    async def test_chains_active_empty_db(self, empty_client):
        resp = await empty_client.get("/api/chains/active")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_chains_patterns_endpoint(self, client):
        resp = await client.get("/api/chains/patterns")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "test_pattern"
        # hit_rate computed: 4/10 = 40%
        assert data[0]["hit_rate"] == 40.0
        assert isinstance(data[0]["steps_json"], list)

    async def test_chains_patterns_empty_db(self, empty_client):
        resp = await empty_client.get("/api/chains/patterns")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_chains_matches_endpoint(self, client):
        resp = await client.get("/api/chains/matches")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["pattern_name"] == "test_pattern"

    async def test_chains_events_recent_endpoint(self, client):
        resp = await client.get("/api/chains/events/recent")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["event_type"] == "momentum_spike"

    async def test_chains_stats_endpoint(self, client):
        resp = await client.get("/api/chains/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_chains"] == 1
        assert data["completed_matches"] == 1
        assert data["total_events"] == 1


# ---------------------------------------------------------------------------
# GET /api/system/health
# ---------------------------------------------------------------------------


class TestSystemHealth:

    async def test_system_health_endpoint(self, client):
        resp = await client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "candidates" in data
        assert "active_chains" in data
        assert "chain_matches" in data
        assert "signal_events" in data
        assert "predictions" in data
        assert "second_wave_candidates" in data
        assert "learn_logs" in data
        assert "agent_strategy" in data
        assert data["candidates"]["count"] == 3
        assert data["active_chains"]["count"] == 1
        assert data["chain_matches"]["count"] == 1

    async def test_system_health_empty_db(self, empty_client):
        resp = await empty_client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["candidates"]["count"] == 0
        assert data["candidates"]["latest"] is None