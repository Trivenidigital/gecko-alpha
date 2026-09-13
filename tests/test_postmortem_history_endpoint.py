"""Bounded read-only historical captures, independent of recorder/JSON shape."""

import sqlite3
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.api import create_app

PATH = "/api/postmortems/moved-already"


@pytest.fixture
def history_path(tmp_path):
    path = tmp_path / "history.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE moved_already_postmortems (
            id INTEGER PRIMARY KEY, token_id TEXT NOT NULL UNIQUE,
            detected_at TEXT NOT NULL, run_pct REAL, evidence TEXT NOT NULL,
            dropping_gate TEXT)""")
        conn.executemany(
            "INSERT INTO moved_already_postmortems VALUES (?,?,?,?,?,?)",
            [
                (
                    i,
                    f"token-{i}",
                    "2026-08-09T01:39:15+00:00",
                    i + 25.0,
                    "invalid JSON",
                    None,
                )
                for i in range(1, 32)
            ],
        )
    return path


@asynccontextmanager
async def client_for(path):
    async with AsyncClient(
        transport=ASGITransport(app=create_app(db_path=str(path))),
        base_url="http://test",
    ) as client:
        yield client


async def test_history_page_and_read_only(history_path):
    before = history_path.read_bytes()
    async with client_for(history_path) as client:
        response = await client.get(PATH)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["meta"]["total_records"] == 31
        assert body["meta"]["read_only"] is True
        assert body["meta"]["historical_only"] is True
        assert [r["id"] for r in body["rows"]] == [str(i) for i in range(31, 6, -1)]
        assert body["next_before_id"] == "7" and body["has_more"] is True
        assert "evidence" not in str(body)
        response = await client.get(PATH, params={"before_id": "7"})
        assert [r["id"] for r in response.json()["rows"]] == [
            str(i) for i in range(6, 0, -1)
        ]
        assert response.json()["next_before_id"] is None
        assert response.json()["has_more"] is False
    assert history_path.read_bytes() == before


async def test_cursor_precision_insertion_and_latest_by_id(history_path):
    large = 9007199254740993
    with sqlite3.connect(history_path) as conn:
        conn.execute(
            "INSERT INTO moved_already_postmortems VALUES (?,?,?,?,?,?)",
            (large, "large", "2000-01-01", 0, "x" * 1000000, "gate"),
        )
    async with client_for(history_path) as client:
        first = (await client.get(PATH, params={"limit": 1})).json()
        assert first["rows"][0]["id"] == str(large)
        assert first["next_before_id"] == str(large)
        assert first["meta"]["latest_detected_at"] == "2000-01-01"
        with sqlite3.connect(history_path) as conn:
            conn.execute(
                "INSERT INTO moved_already_postmortems VALUES (?,?,?,?,?,?)",
                (large + 2, "new", "2001-01-01", 1, "{}", None),
            )
        second = (
            await client.get(PATH, params={"limit": 100, "before_id": str(large)})
        ).json()
        assert [r["id"] for r in second["rows"]] == [str(i) for i in range(31, 0, -1)]
        assert second["meta"]["latest_detected_at"] == "2001-01-01"


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"before_id": 0},
        {"before_id": -1},
        {"before_id": 9223372036854775808},
    ],
)
async def test_invalid_query(history_path, params):
    async with client_for(history_path) as client:
        assert (await client.get(PATH, params=params)).status_code == 422


async def test_empty_missing_and_missing_column(history_path, tmp_path):
    with sqlite3.connect(history_path) as conn:
        conn.execute("DELETE FROM moved_already_postmortems")
    async with client_for(history_path) as client:
        body = (await client.get(PATH)).json()
        assert body["meta"]["total_records"] == 0
        assert body["meta"]["latest_detected_at"] is None
        assert body["rows"] == []
    for missing, reason in [
        (tmp_path / "absent.db", "database_unavailable"),
        (tmp_path / "empty.db", "schema_unavailable"),
    ]:
        if missing.name == "empty.db":
            sqlite3.connect(missing).close()
        async with client_for(missing) as client:
            result = await client.get(PATH)
            assert result.status_code == 503
            assert result.json()["meta"]["data_missing_reason"] == reason
            assert result.headers["cache-control"] == "no-store"
    with sqlite3.connect(history_path) as conn:
        conn.execute("ALTER TABLE moved_already_postmortems DROP COLUMN dropping_gate")
    async with client_for(history_path) as client:
        response = await client.get(PATH)
        assert response.status_code == 503
        assert response.json()["meta"]["data_missing_reason"] == "schema_unavailable"


async def test_anomalous_fields_are_explicit_and_finite(history_path):
    with sqlite3.connect(history_path) as conn:
        conn.execute(
            "UPDATE moved_already_postmortems SET token_id=?, detected_at=?, dropping_gate=?, run_pct=? WHERE id=31",
            ("t" * 257, "d" * 129, b"blob", float("inf")),
        )
        conn.execute(
            "UPDATE moved_already_postmortems SET dropping_gate=?, run_pct=? WHERE id=30",
            ("r" * 513, "garbage"),
        )
        conn.execute("UPDATE moved_already_postmortems SET run_pct=-2 WHERE id=29")
        conn.execute("UPDATE moved_already_postmortems SET run_pct=0 WHERE id=28")
    async with client_for(history_path) as client:
        response = await client.get(PATH)
        assert response.status_code == 200
        body = response.json()
        row = body["rows"][0]
        assert row["token_id"] is None and row["detected_at"] is None
        assert row["most_frequent_recorded_block_reason"] is None
        assert row["run_pct"] is None
        assert row["field_unavailable_reasons"] == {
            "token_id": "too_long",
            "detected_at": "too_long",
            "most_frequent_recorded_block_reason": "non_text",
        }
        assert body["meta"]["latest_detected_at"] is None
        assert body["meta"]["latest_detected_at_unavailable_reason"] == "too_long"
        assert (
            body["rows"][1]["field_unavailable_reasons"][
                "most_frequent_recorded_block_reason"
            ]
            == "too_long"
        )
        assert [r["run_pct"] for r in body["rows"][:4]] == [None, None, -2, 0]
        assert "Infinity" not in response.text


async def test_malformed_text_and_unexpected_failure_sanitized(
    history_path, monkeypatch
):
    with sqlite3.connect(history_path) as conn:
        conn.execute(
            "UPDATE moved_already_postmortems SET token_id=CAST(x'80' AS TEXT) WHERE id=31"
        )
    async with client_for(history_path) as client:
        response = await client.get(PATH)
        assert response.status_code == 503
        assert response.json()["meta"]["data_missing_reason"] == "query_failed"
        assert str(history_path) not in response.text

    async def failed(*args, **kwargs):
        raise RuntimeError("SECRET SQL FILE PATH")

    monkeypatch.setattr("dashboard.db.get_postmortem_history", failed)
    async with client_for(history_path) as client:
        response = await client.get(PATH)
        assert response.status_code == 503
        assert "SECRET" not in response.text


async def test_query_never_reads_evidence_and_ro_denies_writes(
    history_path, monkeypatch
):
    from dashboard import db as dashboard_db

    real_ro = dashboard_db._ro_db
    reads = []

    @asynccontextmanager
    async def guarded(path):
        async with real_ro(path) as conn:

            def authorize(action, table, column, database, source):
                if action == sqlite3.SQLITE_READ:
                    reads.append((table, column))
                    if column == "evidence":
                        return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            await conn._execute(conn._conn.set_authorizer, authorize)
            yield conn

    monkeypatch.setattr(dashboard_db, "_ro_db", guarded)
    body = await dashboard_db.get_postmortem_history(str(history_path))
    assert len(body["rows"]) == 25
    assert ("moved_already_postmortems", "run_pct") in reads
    assert ("moved_already_postmortems", "evidence") not in reads
    async with real_ro(str(history_path)) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await conn.execute("UPDATE moved_already_postmortems SET run_pct=99")


async def test_embedded_nul_cannot_bypass_response_bounds(history_path):
    with sqlite3.connect(history_path) as conn:
        conn.execute(
            "UPDATE moved_already_postmortems SET token_id=?, dropping_gate=?, run_pct=? WHERE id=31",
            ("a\0" + "x" * 1000000, "<b>gate</b>", "x" * 1000000),
        )
    async with client_for(history_path) as client:
        response = await client.get(PATH)
        assert response.status_code == 200
        row = response.json()["rows"][0]
        assert row["token_id"] is None
        assert row["field_unavailable_reasons"]["token_id"] == "too_long"
        assert row["most_frequent_recorded_block_reason"] == "<b>gate</b>"
        assert row["run_pct"] is None
        assert len(response.content) < 20000
