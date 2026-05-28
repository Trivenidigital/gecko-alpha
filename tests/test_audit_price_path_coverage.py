"""Tests for the price-path coverage audit diagnostic."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 5, 28, 22, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def audit():
    spec = importlib.util.spec_from_file_location(
        "audit_price_path_coverage",
        ROOT / "scripts" / "audit_price_path_coverage.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_price_path_coverage"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "scout.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE volume_history_cg (
            id INTEGER PRIMARY KEY,
            coin_id TEXT NOT NULL,
            price REAL,
            recorded_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    yield path, conn
    conn.close()


def _ro_conn(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _insert_point(conn, coin_id, price, hours_before_now):
    ts = (FIXED_NOW - timedelta(hours=hours_before_now)).isoformat()
    conn.execute(
        "INSERT INTO volume_history_cg (coin_id, price, recorded_at) VALUES (?, ?, ?)",
        (coin_id, price, ts),
    )


def test_empty_payload_returns_null_distributions(audit, db):
    path, _ = db
    conn = _ro_conn(path)
    try:
        report = audit.build_report(
            "http://test", [], conn, 36, 24, FIXED_NOW
        )
    finally:
        conn.close()
    assert report["total_rows"] == 0
    assert report["paper_corpus"]["join_rate"] is None
    assert report["paper_corpus"]["points_distribution"] is None
    assert report["tracker_corpus"]["join_rate"] is None
    assert report["tracker_corpus"]["points_distribution"] is None


def test_tracker_row_with_multiple_points_in_window(audit, db):
    path, conn = db
    for h in [1, 6, 12, 18, 23]:
        _insert_point(conn, "bitcoin", 60000.0 + h, h)
    conn.commit()
    rows = [{"source_corpus": "tracker", "token_id": "bitcoin", "symbol": "BTC"}]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36, 24, FIXED_NOW)
    finally:
        ro.close()
    tracker = report["tracker_corpus"]
    assert tracker["rows"] == 1
    assert tracker["rows_with_at_least_one_point"] == 1
    assert tracker["rows_with_zero_points"] == 0
    assert tracker["per_row"][0]["points"] == 5


def test_tracker_row_with_zero_points(audit, db):
    path, _ = db  # empty volume_history_cg
    rows = [{"source_corpus": "tracker", "token_id": "obscure-token", "symbol": "OBS"}]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36, 24, FIXED_NOW)
    finally:
        ro.close()
    tracker = report["tracker_corpus"]
    assert tracker["rows_with_at_least_one_point"] == 0
    assert tracker["rows_with_zero_points"] == 1
    assert tracker["per_row"][0]["points"] == 0


def test_lookback_boundary_inclusive_at_exactly_cutoff(audit, db):
    """Pin: a row at exactly the cutoff timestamp counts (>= is inclusive)."""
    path, conn = db
    # One point exactly at cutoff (24h before now) — must be counted
    cutoff = (FIXED_NOW - timedelta(hours=24)).isoformat()
    conn.execute(
        "INSERT INTO volume_history_cg (coin_id, price, recorded_at) VALUES (?, ?, ?)",
        ("bitcoin", 50000.0, cutoff),
    )
    conn.commit()
    rows = [{"source_corpus": "tracker", "token_id": "bitcoin", "symbol": "BTC"}]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36, 24, FIXED_NOW)
    finally:
        ro.close()
    assert report["tracker_corpus"]["per_row"][0]["points"] == 1


def test_lookback_excludes_point_just_outside_window(audit, db):
    """One point at cutoff-1s should be excluded."""
    path, conn = db
    just_outside = (FIXED_NOW - timedelta(hours=24, seconds=1)).isoformat()
    conn.execute(
        "INSERT INTO volume_history_cg (coin_id, price, recorded_at) VALUES (?, ?, ?)",
        ("bitcoin", 50000.0, just_outside),
    )
    conn.commit()
    rows = [{"source_corpus": "tracker", "token_id": "bitcoin", "symbol": "BTC"}]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36, 24, FIXED_NOW)
    finally:
        ro.close()
    assert report["tracker_corpus"]["per_row"][0]["points"] == 0


def test_null_zero_and_negative_price_excluded_from_count(audit, db):
    path, conn = db
    conn.executemany(
        "INSERT INTO volume_history_cg (coin_id, price, recorded_at) VALUES (?, ?, ?)",
        [
            ("bitcoin", None, (FIXED_NOW - timedelta(hours=1)).isoformat()),
            ("bitcoin", 0.0, (FIXED_NOW - timedelta(hours=2)).isoformat()),
            ("bitcoin", -5.0, (FIXED_NOW - timedelta(hours=3)).isoformat()),
            ("bitcoin", 60000.0, (FIXED_NOW - timedelta(hours=4)).isoformat()),
        ],
    )
    conn.commit()
    rows = [{"source_corpus": "tracker", "token_id": "bitcoin", "symbol": "BTC"}]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36, 24, FIXED_NOW)
    finally:
        ro.close()
    # Only the single positive-finite price counts.
    assert report["tracker_corpus"]["per_row"][0]["points"] == 1


def test_paper_row_with_zero_points_counted_as_unjoinable_or_zero(audit, db):
    path, _ = db
    rows = [{"source_corpus": "paper", "token_id": "0xunknown", "symbol": "UNK"}]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36, 24, FIXED_NOW)
    finally:
        ro.close()
    paper = report["paper_corpus"]
    assert paper["rows"] == 1
    assert paper["joinable_by_token_id"] == 0
    assert paper["unjoinable_or_zero_points"] == 1
    assert paper["join_rate"] == 0.0


def test_paper_row_with_cg_slug_token_id_joins_to_volume_history(audit, db):
    """Paper row whose token_id happens to be a CG slug joins directly."""
    path, conn = db
    for h in [2, 5, 10]:
        _insert_point(conn, "ethereum", 3000.0 + h, h)
    conn.commit()
    rows = [{"source_corpus": "paper", "token_id": "ethereum", "symbol": "ETH"}]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36, 24, FIXED_NOW)
    finally:
        ro.close()
    paper = report["paper_corpus"]
    assert paper["joinable_by_token_id"] == 1
    assert paper["per_row"][0]["points"] == 3


def test_distribution_null_when_n_below_5(audit, db):
    path, conn = db
    for h in [1, 5, 10]:
        _insert_point(conn, "bitcoin", 60000.0 + h, h)
    conn.commit()
    rows = [
        {"source_corpus": "tracker", "token_id": "bitcoin", "symbol": "BTC"},
        {"source_corpus": "tracker", "token_id": "ethereum", "symbol": "ETH"},
    ]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36, 24, FIXED_NOW)
    finally:
        ro.close()
    # N=2 < 5 → distribution null
    assert report["tracker_corpus"]["points_distribution"] is None
    # Per-row still populated
    assert len(report["tracker_corpus"]["per_row"]) == 2


def test_distribution_emitted_when_n_at_or_above_5(audit, db):
    path, conn = db
    # Populate 5 tracker tokens with different point counts: 1, 2, 3, 4, 5
    for i, n in enumerate([1, 2, 3, 4, 5]):
        token = f"token-{i}"
        for h in range(n):
            _insert_point(conn, token, 100.0 + h, h + 1)
    conn.commit()
    rows = [
        {"source_corpus": "tracker", "token_id": f"token-{i}", "symbol": f"T{i}"}
        for i in range(5)
    ]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36, 24, FIXED_NOW)
    finally:
        ro.close()
    dist = report["tracker_corpus"]["points_distribution"]
    assert dist is not None
    assert dist["min"] == 1
    assert dist["max"] == 5
    assert dist["median"] == 3


def test_empty_token_id_does_not_crash_sql_bind(audit, db):
    """Defensive: tracker row with empty token_id classifies as zero points."""
    path, _ = db
    rows = [{"source_corpus": "tracker", "token_id": "", "symbol": "EMPTY"}]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36, 24, FIXED_NOW)
    finally:
        ro.close()
    assert report["tracker_corpus"]["per_row"][0]["points"] == 0


def test_schema_findings_use_pragma_runtime(audit, db):
    path, conn = db
    # Drop and recreate volume_history_cg without price column
    conn.execute("DROP TABLE volume_history_cg")
    conn.execute(
        "CREATE TABLE volume_history_cg (coin_id TEXT, recorded_at TEXT)"
    )
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", [], ro, 36, 24, FIXED_NOW)
    finally:
        ro.close()
    assert report["schema_findings"]["volume_history_cg_has_price"] is False
    assert report["schema_findings"]["volume_history_cg_has_recorded_at"] is True
    # Alternate tables not present in our fixture
    for name, present in report["schema_findings"][
        "alternate_price_history_tables_present"
    ].items():
        assert present is False


def test_main_rejects_lookback_above_ceiling(audit, db, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit",
            "--db",
            str(path),
            "--url",
            "http://test",
            "--lookback-hours",
            "200",
            "--json",
        ],
    )
    rc = audit.main()
    out = capsys.readouterr().out
    assert rc == 2
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert payload["stage"] == "args"


def test_main_smoke_with_empty_payload(audit, db, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setattr(
        audit,
        "_fetch_focus_rows",
        lambda url, window_hours, timeout: ("http://test/api/todays_focus", []),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit", "--db", str(path), "--url", "http://test", "--json"],
    )
    rc = audit.main()
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["total_rows"] == 0
    assert payload["audited_at"].endswith("Z")
    assert payload["lookback_hours"] == 24


def test_main_fetch_failure_returns_2(audit, db, monkeypatch, capsys):
    import urllib.error

    path, _ = db

    def _fail(*_a, **_kw):
        raise urllib.error.URLError("synthetic")

    monkeypatch.setattr(audit, "_fetch_focus_rows", _fail)
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit", "--db", str(path), "--url", "http://test", "--json"],
    )
    rc = audit.main()
    out = capsys.readouterr().out
    assert rc == 2
    payload = json.loads(out)
    assert payload["stage"] == "fetch"
