"""Tests for dashboard global search.

Covers normalize_query, per-table searchers, run_search orchestrator with
dedup, the /api/search FastAPI route, and SQL-injection safety.

See tasks/plan_dashboard_global_search.md + tasks/design_dashboard_global_search.md.
"""

import aiosqlite
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_dashboard_module_globals():
    """Reset dashboard.api module-level state between tests.

    create_app() mutates _db_path and _scout_db (dashboard/api.py:47-50).
    Without this fixture a cached _scout_db from test A leaks into test B.
    """
    import dashboard.api as _api

    saved_db_path = _api._db_path
    saved_scout_db = _api._scout_db
    yield
    _api._db_path = saved_db_path
    _api._scout_db = saved_scout_db


# ---------- Models ----------


def test_search_hit_minimal():
    from dashboard.models import SearchHit

    hit = SearchHit(
        canonical_id="0xabc",
        symbol="CHIP",
        name="ChipCoin",
        chain="solana",
        sources=["candidates"],
        first_seen_at="2026-05-14T10:00:00+00:00",
        last_seen_at="2026-05-14T10:00:00+00:00",
        match_quality="exact_symbol",
    )
    assert hit.symbol == "CHIP"
    assert hit.sources == ["candidates"]
    assert hit.entity_kind == "token"


def test_search_response_shape():
    from dashboard.models import SearchResponse

    resp = SearchResponse(query="CHIP", total_hits=0, hits=[])
    assert resp.query == "CHIP"
    assert resp.hits == []


# ---------- Query normalization ----------


def test_normalize_strips_whitespace():
    from dashboard.search import normalize_query

    assert normalize_query("  CHIP  ") == "chip"


def test_normalize_strips_dollar_prefix():
    from dashboard.search import normalize_query

    assert normalize_query("$CHIP") == "chip"


def test_normalize_strips_hash_prefix():
    from dashboard.search import normalize_query

    assert normalize_query("#CHIP") == "chip"


def test_normalize_strips_control_chars():
    from dashboard.search import normalize_query

    assert normalize_query("\x00chip\x01") == "chip"


def test_normalize_keeps_contract_address_letters():
    from dashboard.search import normalize_query

    assert normalize_query("0xAbC") == "0xabc"


def test_normalize_rejects_empty():
    from dashboard.search import QueryTooShortError, normalize_query

    with pytest.raises(QueryTooShortError):
        normalize_query("")


def test_normalize_rejects_single_char():
    from dashboard.search import QueryTooShortError, normalize_query

    with pytest.raises(QueryTooShortError):
        normalize_query("a")


def test_normalize_rejects_whitespace_only():
    from dashboard.search import QueryTooShortError, normalize_query

    with pytest.raises(QueryTooShortError):
        normalize_query("   ")


def test_normalize_rejects_sigil_only():
    from dashboard.search import QueryTooShortError, normalize_query

    with pytest.raises(QueryTooShortError):
        normalize_query("$")


# ---------- Seed helpers ----------


async def _seed_candidates(db_path):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE candidates (
                contract_address TEXT PRIMARY KEY,
                chain TEXT NOT NULL,
                token_name TEXT NOT NULL,
                ticker TEXT NOT NULL,
                market_cap_usd REAL,
                first_seen_at TEXT NOT NULL,
                alerted_at TEXT
            )
            """
        )
        await conn.executemany(
            "INSERT INTO candidates VALUES (?,?,?,?,?,?,?)",
            [
                ("0xCHIP01", "solana", "ChipCoin", "CHIP", 1000.0,
                 "2026-05-14T10:00:00+00:00", None),
                ("0xCHIP02", "base", "ChipperCoin", "CHIPR", 2000.0,
                 "2026-05-14T11:00:00+00:00", None),
                ("0xOTHER", "solana", "Other", "OTH", 500.0,
                 "2026-05-14T12:00:00+00:00", None),
            ],
        )
        await conn.commit()


# ---------- search_candidates ----------


async def test_search_candidates_exact_symbol(tmp_path):
    db_path = str(tmp_path / "scout.db")
    await _seed_candidates(db_path)

    from dashboard.search import search_candidates

    hits = await search_candidates(db_path, "chip", limit=10)
    assert len(hits) == 2
    symbols = [h.symbol for h in hits]
    assert "CHIP" in symbols and "CHIPR" in symbols
    assert hits[0].symbol == "CHIP"
    assert hits[0].match_quality == "exact_symbol"


async def test_search_candidates_contract_address(tmp_path):
    db_path = str(tmp_path / "scout.db")
    await _seed_candidates(db_path)

    from dashboard.search import search_candidates

    hits = await search_candidates(db_path, "0xCHIP01", limit=10)
    assert len(hits) == 1
    assert hits[0].canonical_id == "0xCHIP01"
    assert hits[0].match_quality == "exact_contract"


async def test_search_candidates_no_match(tmp_path):
    db_path = str(tmp_path / "scout.db")
    await _seed_candidates(db_path)

    from dashboard.search import search_candidates

    hits = await search_candidates(db_path, "nosuchtoken", limit=10)
    assert hits == []


# ---------- search_paper_trades ----------


async def test_search_paper_trades_by_symbol(tmp_path):
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                chain TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL,
                pnl_pct REAL,
                peak_pct REAL
            )
            """
        )
        await conn.execute(
            "INSERT INTO paper_trades(token_id,symbol,name,chain,signal_type,"
            "opened_at,status,pnl_pct,peak_pct) VALUES (?,?,?,?,?,?,?,?,?)",
            ("chip-coin", "CHIP", "ChipCoin", "coingecko", "gainers_early",
             "2026-05-10T10:00:00+00:00", "closed_tp", 25.4, 30.1),
        )
        await conn.commit()

    from dashboard.search import search_paper_trades

    hits = await search_paper_trades(db_path, "chip", limit=10)
    assert len(hits) == 1
    assert hits[0].symbol == "CHIP"
    assert hits[0].best_paper_trade_pnl_pct == 25.4
    assert "paper_trades" in hits[0].sources


# ---------- search_alerts (LEFT JOIN candidates) ----------


async def test_search_alerts_left_join_recovers_ticker(tmp_path):
    """Pre-migration alert rows have NULL ticker/token_name. The LEFT JOIN
    to candidates must recover the ticker so the alert still matches."""
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE candidates (
                contract_address TEXT PRIMARY KEY,
                chain TEXT NOT NULL,
                token_name TEXT NOT NULL,
                ticker TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                alerted_at TEXT
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL,
                chain TEXT NOT NULL,
                conviction_score REAL NOT NULL,
                alert_market_cap REAL,
                alerted_at TEXT NOT NULL,
                token_name TEXT,
                ticker TEXT
            )
            """
        )
        await conn.execute(
            "INSERT INTO candidates VALUES (?,?,?,?,?,?)",
            ("0xchip", "solana", "ChipCoin", "CHIP",
             "2026-05-10T08:00:00+00:00", "2026-05-13T10:00:00+00:00"),
        )
        # Pre-migration alert: NULL ticker/token_name
        await conn.execute(
            "INSERT INTO alerts(contract_address,chain,conviction_score,"
            "alerted_at,token_name,ticker) VALUES (?,?,?,?,?,?)",
            ("0xchip", "solana", 72.0, "2026-05-13T10:00:00+00:00", None, None),
        )
        await conn.commit()

    from dashboard.search import search_alerts

    hits = await search_alerts(db_path, "chip", limit=10)
    assert len(hits) == 1
    assert hits[0].symbol == "CHIP"
    assert hits[0].name == "ChipCoin"
    assert "alerts" in hits[0].sources


# ---------- search_snapshots ----------


async def test_search_gainers_snapshots_aggregates(tmp_path):
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE gainers_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                price_change_24h REAL NOT NULL,
                market_cap REAL,
                volume_24h REAL,
                snapshot_at TEXT NOT NULL
            )
            """
        )
        await conn.executemany(
            "INSERT INTO gainers_snapshots(coin_id,symbol,name,"
            "price_change_24h,snapshot_at) VALUES (?,?,?,?,?)",
            [
                ("chip-coin", "CHIP", "ChipCoin", 65.0,
                 "2026-05-14T10:00:00+00:00"),
                ("chip-coin", "CHIP", "ChipCoin", 72.0,
                 "2026-05-14T11:00:00+00:00"),
                ("chip-coin", "CHIP", "ChipCoin", 80.0,
                 "2026-05-14T12:00:00+00:00"),
            ],
        )
        await conn.commit()

    from dashboard.search import search_snapshots

    hits = await search_snapshots(
        db_path, "chip", limit=10, table="gainers_snapshots"
    )
    assert len(hits) == 1
    assert hits[0].source_counts["gainers_snapshots"] == 3
    assert hits[0].first_seen_at == "2026-05-14T10:00:00+00:00"
    assert hits[0].last_seen_at == "2026-05-14T12:00:00+00:00"


async def test_search_snapshots_rejects_unknown_table(tmp_path):
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("CREATE TABLE evil (x TEXT)")
        await conn.commit()

    from dashboard.search import search_snapshots

    with pytest.raises(ValueError):
        await search_snapshots(db_path, "chip", limit=10, table="evil")


# ---------- search_tg_messages + search_narrative_inbound ----------


async def test_search_tg_messages_finds_cashtag(tmp_path):
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE tg_social_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_handle TEXT NOT NULL,
                msg_id INTEGER NOT NULL,
                posted_at TEXT NOT NULL,
                sender TEXT,
                text TEXT,
                cashtags TEXT,
                contracts TEXT,
                parsed_at TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            "INSERT INTO tg_social_messages(channel_handle,msg_id,posted_at,"
            "sender,text,cashtags,contracts,parsed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("@kol", 1, "2026-05-14T08:00:00+00:00", "alice",
             "Watch $CHIP about to break out", '["$CHIP"]', "[]",
             "2026-05-14T08:00:01+00:00"),
        )
        await conn.commit()

    from dashboard.search import search_tg_messages

    hits = await search_tg_messages(db_path, "chip", limit=10)
    assert len(hits) == 1
    assert hits[0].entity_kind == "tg_msg"
    assert "tg_social_messages" in hits[0].sources


async def test_search_narrative_inbound_resolved_vs_unresolved(tmp_path):
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE narrative_alerts_inbound (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                tweet_id TEXT NOT NULL,
                tweet_author TEXT NOT NULL,
                tweet_ts TEXT NOT NULL,
                tweet_text TEXT NOT NULL,
                tweet_text_hash TEXT NOT NULL,
                extracted_cashtag TEXT,
                resolved_coin_id TEXT,
                classifier_version TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
            """
        )
        await conn.executemany(
            """INSERT INTO narrative_alerts_inbound(
                event_id,tweet_id,tweet_author,tweet_ts,tweet_text,
                tweet_text_hash,extracted_cashtag,resolved_coin_id,
                classifier_version,received_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                ("e1", "t1", "kol_jim", "2026-05-14T07:00:00+00:00",
                 "$CHIP looks heated", "h1", "$CHIP", "chip-coin", "v1",
                 "2026-05-14T07:00:05+00:00"),
                ("e2", "t2", "kol_jane", "2026-05-14T07:10:00+00:00",
                 "Another chip mention", "h2", "$CHIP", None, "v1",
                 "2026-05-14T07:10:05+00:00"),
            ],
        )
        await conn.commit()

    from dashboard.search import search_narrative_inbound

    hits = await search_narrative_inbound(db_path, "chip", limit=10)
    assert len(hits) == 2
    kinds = {h.entity_kind for h in hits}
    assert "token" in kinds  # resolved
    assert "x_alert" in kinds  # unresolved


# ---------- run_search orchestrator ----------


async def test_run_search_aggregates_across_tables(tmp_path):
    """Token in candidates + gainers_snapshots collapses to one hit
    with sources=['candidates','gainers_snapshots']."""
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """CREATE TABLE candidates (
                contract_address TEXT PRIMARY KEY, chain TEXT, token_name TEXT,
                ticker TEXT, market_cap_usd REAL, first_seen_at TEXT,
                alerted_at TEXT
            )"""
        )
        await conn.execute(
            """CREATE TABLE gainers_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, coin_id TEXT,
                symbol TEXT, name TEXT, price_change_24h REAL, snapshot_at TEXT
            )"""
        )
        await conn.execute(
            "INSERT INTO candidates VALUES (?,?,?,?,?,?,?)",
            ("chip-coin", "coingecko", "ChipCoin", "CHIP", 50000.0,
             "2026-05-14T09:00:00+00:00", None),
        )
        await conn.execute(
            "INSERT INTO gainers_snapshots(coin_id,symbol,name,"
            "price_change_24h,snapshot_at) VALUES (?,?,?,?,?)",
            ("chip-coin", "CHIP", "ChipCoin", 65.0,
             "2026-05-14T10:00:00+00:00"),
        )
        await conn.commit()

    from dashboard.search import run_search

    resp = await run_search(db_path, "chip", limit=50)
    assert resp.total_hits == 1
    h = resp.hits[0]
    assert h.canonical_id == "chip-coin"
    assert set(h.sources) == {"candidates", "gainers_snapshots"}


async def test_run_search_empty_query(tmp_path):
    from dashboard.search import QueryTooShortError, run_search

    db_path = str(tmp_path / "scout.db")
    # Even with no DB the normalize step should raise first.
    with pytest.raises(QueryTooShortError):
        await run_search(db_path, "", limit=50)


async def test_run_search_sql_injection_attempt(tmp_path):
    """A query containing SQL meta chars must be treated as text, not SQL.
    Even if injection somehow escaped, _ro_db's URI mode=ro rejects writes."""
    db_path = str(tmp_path / "scout.db")
    await _seed_candidates(db_path)

    from dashboard.search import run_search

    resp = await run_search(
        db_path, "'; DROP TABLE candidates; --", limit=50
    )
    assert resp.total_hits == 0
    # Table still exists with original rows
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM candidates")
        cnt = (await cur.fetchone())[0]
        assert cnt == 3


async def test_run_search_missing_db_returns_empty(tmp_path):
    """If the DB file doesn't exist, search returns empty (not 500)."""
    db_path = str(tmp_path / "nonexistent.db")

    from dashboard.search import run_search

    resp = await run_search(db_path, "chip", limit=50)
    assert resp.total_hits == 0


async def test_run_search_empty_db_returns_empty(tmp_path):
    """If tables don't exist, _safe_call catches OperationalError."""
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("CREATE TABLE _placeholder (x TEXT)")
        await conn.commit()

    from dashboard.search import run_search

    resp = await run_search(db_path, "chip", limit=50)
    assert resp.total_hits == 0


async def test_run_search_dedup_tuple_keeps_distinct_chains_separate(tmp_path):
    """Dedup key is (canonical_id, entity_kind, chain). Two hits with the
    same canonical_id but different chains must stay separate — guards
    against Quack-AI-style 1-char canonical_id collisions across chains."""
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """CREATE TABLE candidates (
                contract_address TEXT PRIMARY KEY, chain TEXT, token_name TEXT,
                ticker TEXT, market_cap_usd REAL, first_seen_at TEXT,
                alerted_at TEXT
            )"""
        )
        await conn.execute(
            """CREATE TABLE tg_social_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_pk INTEGER NOT NULL,
                token_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                contract_address TEXT,
                chain TEXT,
                mcap_at_sighting REAL,
                resolution_state TEXT NOT NULL,
                source_channel_handle TEXT NOT NULL,
                alert_sent_at TEXT,
                paper_trade_id INTEGER,
                created_at TEXT NOT NULL
            )"""
        )
        # Same canonical_id "qq" — once as candidates(chain=coingecko),
        # once as tg_social_signals(chain=solana).
        await conn.execute(
            "INSERT INTO candidates VALUES (?,?,?,?,?,?,?)",
            ("qq", "coingecko", "Quack AI", "QQ", 1e8,
             "2026-05-14T09:00:00+00:00", None),
        )
        await conn.execute(
            "INSERT INTO tg_social_signals(message_pk,token_id,symbol,"
            "contract_address,chain,resolution_state,source_channel_handle,"
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (1, "qq", "QQ", "qq", "solana", "resolved", "@kol",
             "2026-05-14T10:00:00+00:00"),
        )
        await conn.commit()

    from dashboard.search import run_search

    resp = await run_search(db_path, "qq", limit=50)
    # Both entries match. Because chains differ, they DON'T collapse.
    chains = {h.chain for h in resp.hits}
    assert "coingecko" in chains
    assert "solana" in chains
    assert resp.total_hits == 2


# ---------- API route + integration ----------


async def test_search_endpoint_returns_results(tmp_path):
    db_path = str(tmp_path / "scout.db")
    await _seed_candidates(db_path)

    from dashboard.api import create_app

    app = create_app(db_path=db_path)
    client = TestClient(app)
    r = client.get("/api/search?q=chip")
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "chip"
    assert body["total_hits"] >= 1
    assert any(h["symbol"] == "CHIP" for h in body["hits"])


async def test_search_endpoint_rejects_short_query(tmp_path):
    db_path = str(tmp_path / "scout.db")
    await _seed_candidates(db_path)

    from dashboard.api import create_app

    app = create_app(db_path=db_path)
    client = TestClient(app)
    # FastAPI validator rejects min_length=2 at the query level → 422
    r = client.get("/api/search?q=a")
    assert r.status_code in (400, 422)


async def test_search_endpoint_limit_enforced(tmp_path):
    db_path = str(tmp_path / "scout.db")
    await _seed_candidates(db_path)

    from dashboard.api import create_app

    app = create_app(db_path=db_path)
    client = TestClient(app)
    r = client.get("/api/search?q=chip&limit=1")
    assert r.status_code == 200
    body = r.json()
    assert len(body["hits"]) <= 1
