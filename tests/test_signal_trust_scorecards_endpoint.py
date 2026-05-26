"""Tests for /api/signal_trust/scorecards (read-only signal scorecards)."""

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
    d, db_path = db
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, d
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


async def test_scorecards_returns_200_with_invariants(client):
    c, _ = client
    resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    payload = resp.json()
    meta = payload["meta"]
    assert meta["ok"] is True
    assert meta["read_only"] is True
    assert meta["not_for_pruning"] is True
    assert meta["not_for_suppression"] is True
    assert meta["not_for_auto_disable"] is True
    assert meta["not_for_sizing"] is True
    assert meta["not_for_execution"] is True
    assert meta["not_for_alerting"] is True
    assert meta["not_for_source_ranking"] is True
    assert meta["experimental"] is True
    assert meta["visibility_only"] is True
    assert meta["not_live_eligibility_verdict"] is True
    assert meta["cohort_policy"] == "full_closed_paper_trades"
    assert meta["sort_policy"] == "signal_type_asc_not_ranked"
    assert meta["generated_at"]
    assert meta["windows_days"] == [7, 14, 30]
    assert "rows" in payload


async def test_scorecards_ordering_is_deterministic(client):
    c, d = client
    conn = d._conn
    assert conn is not None
    # Create two open trades with distinct signal types so union-of-keys is non-empty.
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity,
            tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at,
            would_be_live, actionable)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)""",
        (
            "t1",
            "T1",
            "t1",
            "coingecko",
            "volume_spike",
            "{}",
            1.0,
            100.0,
            10.0,
            20.0,
            10.0,
            1.2,
            0.9,
            "open",
            1,
            1,
        ),
    )
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity,
            tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at,
            would_be_live, actionable)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)""",
        (
            "t2",
            "T2",
            "t2",
            "coingecko",
            "chain_completed",
            "{}",
            1.0,
            100.0,
            10.0,
            20.0,
            10.0,
            1.2,
            0.9,
            "open",
            1,
            1,
        ),
    )
    await conn.commit()

    resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    signal_types = [r["signal_type"] for r in rows]
    assert signal_types == sorted(signal_types)


async def test_scorecards_returns_503_when_paper_trades_missing(tmp_path):
    db_path = tmp_path / "empty.db"  # never initialized
    db_path.write_bytes(b"")
    app = create_app(db_path=str(db_path))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["meta"]["ok"] is False
    assert payload["meta"]["read_only"] is True
    assert payload["meta"]["not_for_pruning"] is True
    assert payload["meta"]["not_for_suppression"] is True
    assert payload["meta"]["not_for_auto_disable"] is True
    assert payload["meta"]["not_for_sizing"] is True
    assert payload["meta"]["not_for_execution"] is True
    assert payload["meta"]["not_for_alerting"] is True
    assert payload["meta"]["not_for_source_ranking"] is True
    assert payload["meta"]["visibility_only"] is True
    assert payload["meta"]["not_live_eligibility_verdict"] is True
    assert payload["meta"]["cohort_policy"] == "full_closed_paper_trades"
    assert payload["meta"]["sort_policy"] == "signal_type_asc_not_ranked"
    assert payload["meta"]["data_missing_reason"] == "paper_trades_missing"
    assert payload["rows"] == []
    assert payload["error"]["code"] == "paper_trades_missing"


async def test_scorecards_emits_low_n_and_no_stamps_warnings(client):
    c, d = client
    conn = d._conn
    assert conn is not None
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity,
            tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at, closed_at,
            pnl_usd, pnl_pct,
            would_be_live, actionable)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'),
                   ?, ?, ?, ?)""",
        (
            "t3",
            "T3",
            "t3",
            "coingecko",
            "narrative_prediction",
            "{}",
            1.0,
            50.0,
            5.0,
            20.0,
            10.0,
            1.2,
            0.9,
            "closed",
            5.0,
            10.0,
            None,
            None,
        ),
    )
    await conn.commit()

    resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    row = next(r for r in rows if r["signal_type"] == "narrative_prediction")
    w7 = next(w for w in row["windows"] if w["days"] == 7)
    assert "low_n" in w7["warnings"]
    assert "no_stamps" in w7["warnings"]


async def test_scorecards_degrades_when_stamps_columns_missing(tmp_path):
    import sqlite3

    db_path = tmp_path / "nostamps.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""CREATE TABLE paper_trades (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 signal_type TEXT,
                 status TEXT,
                 closed_at TEXT,
                 opened_at TEXT,
                 symbol TEXT,
                 amount_usd REAL,
                 pnl_usd REAL,
                 pnl_pct REAL
               )""")
        conn.execute(
            """INSERT INTO paper_trades (signal_type, status, closed_at, opened_at, symbol, amount_usd, pnl_usd, pnl_pct)
               VALUES ('volume_spike', 'open', NULL, datetime('now'), 'V', 123.0, NULL, NULL)"""
        )
        conn.commit()
    finally:
        conn.close()

    app = create_app(db_path=str(db_path))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["meta"]["ok"] is True
    assert payload["meta"]["data_missing_reason"] == "stamps_unavailable"
    row = next(r for r in payload["rows"] if r["signal_type"] == "volume_spike")
    w7 = next(w for w in row["windows"] if w["days"] == 7)
    assert w7["stamps"] is None


async def test_scorecards_stamp_confusion_matrix_and_null_denominators(client):
    c, d = client
    conn = d._conn
    assert conn is not None

    rows = [
        ("m1", 1, 1),
        ("m2", 1, 0),
        ("m3", 0, 1),
        ("m4", 0, 0),
        ("m5", None, 1),
    ]
    for token_id, actionable, would_be_live in rows:
        await conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity,
                tp_pct, sl_pct, tp_price, sl_price,
                status, opened_at, closed_at,
                pnl_usd, pnl_pct,
                would_be_live, actionable)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'),
                       ?, ?, ?, ?)""",
            (
                token_id,
                token_id.upper(),
                token_id,
                "coingecko",
                "matrix_signal",
                "{}",
                1.0,
                20.0,
                2.0,
                20.0,
                10.0,
                1.2,
                0.9,
                "closed",
                1.0,
                1.0,
                would_be_live,
                actionable,
            ),
        )
    await conn.commit()

    resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 200
    row = next(r for r in resp.json()["rows"] if r["signal_type"] == "matrix_signal")
    stamps = next(w for w in row["windows"] if w["days"] == 7)["stamps"]

    assert stamps["both_known_n"] == 4
    assert stamps["null_mismatch_n"] == 1
    assert stamps["unknown_n"] == 1
    assert stamps["actionable_known_n"] == 4
    assert stamps["actionable_unknown_n"] == 1
    assert stamps["actionable_rate"] == pytest.approx(0.5)
    assert stamps["would_be_live_known_n"] == 5
    assert stamps["would_be_live_unknown_n"] == 0
    assert stamps["would_be_live_rate"] == pytest.approx(0.6)
    assert stamps["confusion"] == {
        "a1_w1": 1,
        "a1_w0": 1,
        "a0_w1": 1,
        "a0_w0": 1,
    }
    assert stamps["disagree_n"] == 2
    assert stamps["disagree_rate"] == pytest.approx(0.5)


async def test_scorecards_returns_503_when_required_columns_missing(tmp_path):
    import sqlite3

    db_path = tmp_path / "bad_schema.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""CREATE TABLE paper_trades (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 signal_type TEXT,
                 status TEXT,
                 closed_at TEXT,
                 opened_at TEXT,
                 symbol TEXT,
                 pnl_usd REAL,
                 pnl_pct REAL,
                 actionable INTEGER,
                 would_be_live INTEGER
               )""")
        conn.commit()
    finally:
        conn.close()

    app = create_app(db_path=str(db_path))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["meta"]["ok"] is False
    assert payload["meta"]["data_missing_reason"] == "scorecards_query_failed"
    assert payload["error"]["code"] == "scorecards_query_failed"


async def test_scorecards_reuses_registry_validation(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    d = Database(db_path)
    await d.initialize()
    bad_registry = tmp_path / "bad-registry.json"
    bad_registry.write_text('{"schema_version":"wrong","entries":[]}', encoding="utf-8")
    monkeypatch.setenv("GECKO_SIGNAL_TRUST_REGISTRY_PATH", str(bad_registry))
    monkeypatch.setenv("GECKO_ALLOW_ARBITRARY_SIGNAL_TRUST_REGISTRY_PATH", "1")

    try:
        app = create_app(db_path=str(db_path))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/api/signal_trust/scorecards")
    finally:
        await d.close()

    assert resp.status_code == 503
    payload = resp.json()
    assert payload["meta"]["ok"] is False
    assert payload["meta"]["data_missing_reason"] == "registry_unavailable"
    assert payload["error"]["code"] == "registry_invalid"


def test_scorecards_not_consumed_by_alert_pruning_or_execution_paths():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    api_text = (root / "dashboard" / "api.py").read_text(encoding="utf-8")
    db_text = (root / "dashboard" / "db.py").read_text(encoding="utf-8")
    ui_text = (
        root / "dashboard" / "frontend" / "components" / "SignalTrustTab.jsx"
    ).read_text(encoding="utf-8")
    assert api_text.count("get_signal_trust_scorecards") == 2
    assert api_text.count("/api/signal_trust/scorecards") == 1
    assert db_text.count("def get_signal_trust_scorecards") == 1
    assert ui_text.count("/api/signal_trust/scorecards") == 1

    needles = (
        "db.get_signal_trust_scorecards",
        "get_signal_trust_scorecards(",
        "import get_signal_trust_scorecards",
        "/api/signal_trust/scorecards",
    )
    allowed = {
        Path("dashboard/api.py"),
        Path("dashboard/db.py"),
        Path("dashboard/frontend/components/SignalTrustTab.jsx"),
        Path("tests/test_signal_trust_scorecards_endpoint.py"),
    }
    scanned_roots = [root / "scout", root / "scripts", root / "dashboard"]
    hits: list[str] = []
    for base in scanned_roots:
        for path in base.rglob("*"):
            if path.suffix not in {".py", ".js", ".jsx", ".mjs", ".sh"}:
                continue
            rel = path.relative_to(root)
            if rel in allowed or "frontend/dist" in rel.as_posix():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(needle in text for needle in needles):
                hits.append(str(rel))
    assert hits == []


async def test_scorecards_union_of_keys_includes_registry_only_and_db_only(
    tmp_path, monkeypatch
):
    import json
    import sqlite3

    reg_path = tmp_path / "registry.json"
    reg_path.write_text(
        json.dumps(
            {
                "schema_version": "signal_trust_registry.v1",
                "generated_at": "2026-05-25T00:00:00Z",
                "experimental": True,
                "visibility_only": True,
                "not_for_pruning": True,
                "not_for_suppression": True,
                "not_for_auto_disable": True,
                "not_for_sizing": True,
                "not_for_execution": True,
                "not_for_alerting": True,
                "not_for_source_ranking": True,
                "notes": "test registry",
                "maturity_states": [
                    "trusted_experimental",
                    "context_only",
                    "data_insufficient",
                ],
                "operator_gate": [
                    "visibility_only",
                    "not_for_pruning",
                    "not_for_suppression",
                    "not_for_auto_disable",
                    "not_for_sizing",
                    "not_for_execution",
                    "not_for_alerting",
                    "not_for_source_ranking",
                ],
                "entries": [
                    {
                        "signal_type": "registry_only_signal",
                        "maturity_state": "context_only",
                        "data_quality": {"warning": "test"},
                        "operator_gate": [
                            "visibility_only",
                            "not_for_pruning",
                            "not_for_suppression",
                            "not_for_auto_disable",
                            "not_for_sizing",
                            "not_for_execution",
                            "not_for_alerting",
                            "not_for_source_ranking",
                        ],
                        "next_gate": {"type": "sample_size", "threshold": "n>=10"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GECKO_SIGNAL_TRUST_REGISTRY_PATH", str(reg_path))
    monkeypatch.setenv("GECKO_ALLOW_ARBITRARY_SIGNAL_TRUST_REGISTRY_PATH", "1")

    db_path = tmp_path / "dbonly.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""CREATE TABLE paper_trades (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 signal_type TEXT,
                 status TEXT,
                 closed_at TEXT,
                 opened_at TEXT,
                 symbol TEXT,
                 amount_usd REAL,
                 pnl_usd REAL,
                 pnl_pct REAL
               )""")
        conn.execute(
            """INSERT INTO paper_trades (signal_type, status, closed_at, opened_at, symbol, amount_usd, pnl_usd, pnl_pct)
               VALUES ('db_only_signal', 'open', NULL, datetime('now'), 'D', 10.0, NULL, NULL)"""
        )
        conn.commit()
    finally:
        conn.close()

    app = create_app(db_path=str(db_path))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 200
    signal_types = [r["signal_type"] for r in resp.json()["rows"]]
    assert "registry_only_signal" in signal_types
    assert "db_only_signal" in signal_types
    assert signal_types == sorted(signal_types)


async def test_scorecards_window_boundary_respects_time_of_day_for_isoformat_closed_at(
    client,
):
    from datetime import datetime, timedelta, timezone

    c, d = client
    conn = d._conn
    assert conn is not None

    # Craft a closed_at slightly BEFORE the 7d cutoff; this should be excluded from 7d stats.
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    closed_at = (cutoff - timedelta(hours=2)).isoformat()

    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity,
            tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at, closed_at,
            pnl_usd, pnl_pct,
            would_be_live, actionable)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "t4",
            "T4",
            "t4",
            "coingecko",
            "volume_spike",
            "{}",
            1.0,
            10.0,
            1.0,
            20.0,
            10.0,
            1.2,
            0.9,
            "closed",
            datetime.now(timezone.utc).isoformat(),
            closed_at,
            1.0,
            5.0,
            1,
            1,
        ),
    )
    await conn.commit()

    resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 200
    row = next(r for r in resp.json()["rows"] if r["signal_type"] == "volume_spike")
    w7 = next(w for w in row["windows"] if w["days"] == 7)
    assert w7["closed"]["closed_n"] == 0


async def test_scorecards_window_boundary_includes_isoformat_row_inside_window(client):
    from datetime import datetime, timedelta, timezone

    c, d = client
    conn = d._conn
    assert conn is not None

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    closed_at = (cutoff + timedelta(hours=2)).isoformat()

    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity,
            tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at, closed_at,
            pnl_usd, pnl_pct,
            would_be_live, actionable)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "t5",
            "T5",
            "t5",
            "coingecko",
            "inside_window_signal",
            "{}",
            1.0,
            10.0,
            1.0,
            20.0,
            10.0,
            1.2,
            0.9,
            "closed",
            datetime.now(timezone.utc).isoformat(),
            closed_at,
            1.0,
            5.0,
            1,
            1,
        ),
    )
    await conn.commit()

    resp = await c.get("/api/signal_trust/scorecards")
    assert resp.status_code == 200
    row = next(
        r for r in resp.json()["rows"] if r["signal_type"] == "inside_window_signal"
    )
    w7 = next(w for w in row["windows"] if w["days"] == 7)
    assert w7["closed"]["closed_n"] == 1
