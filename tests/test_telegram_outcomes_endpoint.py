"""Recorded Telegram event counts: bounded, descriptive and strictly read-only."""

import sqlite3
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from dashboard.api import create_app


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE tg_alert_log (outcome TEXT, detail TEXT, alerted_at TEXT)"
        )
    return path


def seed(path, rows):
    with sqlite3.connect(path) as conn:
        conn.executemany("INSERT INTO tg_alert_log VALUES (?,?,?)", rows)


async def request(path, query=""):
    async with AsyncClient(
        transport=ASGITransport(app=create_app(str(path))), base_url="http://test"
    ) as client:
        return await client.get("/api/tg_alerts/outcomes" + query)


async def test_empty_ledger_is_available(ledger):
    response = await request(ledger)
    assert response.status_code == 200
    assert response.json()["total_events"] == 0
    assert response.json()["meta"]["ok"] is True


@pytest.fixture
def frozen_clock(monkeypatch):
    from dashboard import telegram_outcomes

    now = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(telegram_outcomes, "_utc_now", lambda: now)
    return now.isoformat()


async def test_exhaustive_partitions_and_no_raw_details(ledger, frozen_clock):
    rows = [
        ("sent", "secret-detail", frozen_clock),
        ("blocked_eligibility", "universe_filter:stock", frozen_clock),
        ("blocked_eligibility", "detection_lane:universe_filter:stock", frozen_clock),
        ("blocked_eligibility", None, frozen_clock),
        ("blocked_eligibility", "Universe_filter:stock", frozen_clock),
        ("blocked_eligibility", "prefix_universe_filter:stock", frozen_clock),
        ("blocked_cooldown", "universe_filter:stock", frozen_clock),
        ("blocked_dedup_24h", None, frozen_clock),
        ("dispatch_failed", None, frozen_clock),
        ("announcement_sent", None, frozen_clock),
        ("m1_5c_announcement_sent", None, frozen_clock),
        ("unfamiliar", None, frozen_clock),
        (None, None, frozen_clock),
    ]
    seed(ledger, rows)
    response = await request(ledger)
    data = response.json()
    assert response.status_code == 200
    assert data["total_events"] == len(rows)
    assert data["blocked_eligibility"] == {
        "paper_open_universe": 1,
        "detection_universe": 1,
        "other_or_unspecified": 3,
    }
    assert data["outcomes"]["other_recorded_outcomes"] == 2
    assert sum(data["outcomes"].values()) == data["total_events"]
    assert (
        sum(data["blocked_eligibility"].values())
        == data["outcomes"]["blocked_eligibility"]
    )
    assert "secret-detail" not in response.text and "stock" not in response.text
    assert response.headers["cache-control"] == "no-store"


async def test_time_boundaries_offsets_naive_and_tablewide_exclusions(
    ledger, frozen_clock
):
    seed(
        ledger,
        [
            ("sent", None, stamp)
            for stamp in [
                "2026-09-12T12:00:00+00:00",  # inclusive start
                "2026-09-13T12:00:00+00:00",  # inclusive end
                "2026-09-13T13:00:00+01:00",  # same end instant
                "2026-09-13 11:00:00",  # naive interpreted UTC
                "2026-09-12T11:59:59.999+00:00",  # before start
                "2026-09-13T12:00:00.001+00:00",  # future
                "2026-08-01T12:00:00+00:00",  # old, parseable
                "bad timestamp",
                None,
            ]
        ],
    )
    data = (await request(ledger)).json()
    assert data["total_events"] == 4
    assert data["meta"]["table_wide_invalid_timestamp_count"] == 2
    assert data["meta"]["table_wide_future_timestamp_count"] == 1
    assert data["meta"]["window_start"] == "2026-09-12T12:00:00+00:00"
    assert data["meta"]["as_of"] == frozen_clock
    assert data["meta"]["time_policy"] == "sqlite_julianday_inclusive_naive_utc"


@pytest.mark.parametrize("days", [1, 30])
async def test_valid_days(ledger, days):
    assert (await request(ledger, f"?days={days}")).status_code == 200


@pytest.mark.parametrize("days", ["0", "31", "-1", "1.5", "no"])
async def test_invalid_days(ledger, days):
    assert (await request(ledger, f"?days={days}")).status_code == 422


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("missing", "database_unavailable"),
        ("table", "schema_unavailable"),
        ("column", "schema_unavailable"),
        ("corrupt", "query_failed"),
    ],
)
async def test_unavailable_is_sanitized_not_zero(tmp_path, kind, reason):
    path = tmp_path / "private-location.db"
    if kind == "corrupt":
        path.write_bytes(b"not a database")
    elif kind != "missing":
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE other (id INTEGER)"
                if kind == "table"
                else "CREATE TABLE tg_alert_log (outcome TEXT)"
            )
    with capture_logs() as logs:
        response = await request(path)
    assert response.status_code == 503
    data = response.json()
    assert data["meta"]["data_missing_reason"] == reason
    assert data["meta"]["ok"] is False
    assert data["total_events"] is None
    assert data["outcomes"] is None and data["blocked_eligibility"] is None
    assert str(path) not in response.text and "SELECT" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "60"
    assert any(
        row["event"] == "telegram_outcomes_unavailable" and row["reason"] == reason
        for row in logs
    )
    if kind == "missing":
        assert not path.exists()


async def test_read_does_not_initialize_or_mutate(ledger, frozen_clock, monkeypatch):
    from dashboard import api

    async def forbidden(*args, **kwargs):
        pytest.fail("read-only endpoint initialized writer")

    monkeypatch.setattr(api, "_get_scout_db", forbidden)
    seed(ledger, [("sent", None, frozen_clock)])
    before = ledger.read_bytes()
    assert (await request(ledger)).status_code == 200
    assert ledger.read_bytes() == before
    from dashboard.db import _ro_db

    async with _ro_db(str(ledger)) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await conn.execute("DELETE FROM tg_alert_log")


async def test_empty_window_keeps_exclusions(ledger, frozen_clock):
    seed(ledger, [("sent", None, "bad"), ("sent", None, "2020-01-01")])
    data = (await request(ledger)).json()
    assert data["total_events"] == 0
    assert all(value == 0 for value in data["outcomes"].values())
    assert data["meta"]["table_wide_invalid_timestamp_count"] == 1


async def test_each_app_retains_its_own_ledger(ledger, tmp_path, frozen_clock):
    seed(ledger, [("sent", None, frozen_clock)])
    first_app = create_app(str(ledger))
    other_path = tmp_path / "other.db"
    with sqlite3.connect(other_path) as conn:
        conn.execute(
            "CREATE TABLE tg_alert_log (outcome TEXT, detail TEXT, alerted_at TEXT)"
        )
    second_app = create_app(str(other_path))
    for app, expected in [(first_app, 1), (second_app, 0), (first_app, 1)]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/tg_alerts/outcomes")
            assert response.json()["total_events"] == expected
