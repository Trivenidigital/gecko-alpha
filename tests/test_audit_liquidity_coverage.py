"""Tests for the liquidity coverage audit diagnostic.

The script consumes the live ``/api/todays_focus`` endpoint and looks up
``candidates.liquidity_usd`` for each paper-corpus row. These tests use
synthetic DB fixtures + monkeypatched HTTP to exercise every
classification path enumerated in the plan doc.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def audit():
    spec = importlib.util.spec_from_file_location(
        "audit_liquidity_coverage",
        ROOT / "scripts" / "audit_liquidity_coverage.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_liquidity_coverage"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "scout.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE candidates (
            contract_address TEXT PRIMARY KEY,
            chain TEXT NOT NULL DEFAULT '',
            liquidity_usd REAL DEFAULT 0
        );
        CREATE TABLE gainers_comparisons (
            id INTEGER PRIMARY KEY,
            coin_id TEXT
        );
        CREATE TABLE price_cache (
            coin_id TEXT PRIMARY KEY
        );
        CREATE TABLE volume_history_cg (
            id INTEGER PRIMARY KEY
        );
        CREATE TABLE trending_comparisons (
            id INTEGER PRIMARY KEY
        );
        """
    )
    conn.commit()
    yield path, conn
    conn.close()


def _ro_conn(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def test_empty_payload_returns_null_coverage_rates(audit, db):
    path, _ = db
    conn = _ro_conn(path)
    try:
        report = audit.build_report("http://test/api/todays_focus", [], conn, 36)
    finally:
        conn.close()
    assert report["total_rows"] == 0
    assert report["paper_corpus"]["rows"] == 0
    assert report["paper_corpus"]["join_rate"] is None
    assert report["paper_corpus"]["coverage_rate"] is None
    assert report["tracker_corpus"]["rows"] == 0
    assert report["tracker_corpus"]["rows_with_liquidity_source"] == 0


def test_paper_row_with_valid_liquidity_is_counted(audit, db):
    path, conn = db
    conn.execute(
        "INSERT INTO candidates (contract_address, chain, liquidity_usd) "
        "VALUES (?, ?, ?)",
        ("0xabc", "ethereum", 50000.0),
    )
    conn.commit()
    rows = [
        {"source_corpus": "paper", "token_id": "0xabc", "chain": "ethereum"}
    ]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36)
    finally:
        ro.close()
    paper = report["paper_corpus"]
    assert paper["rows"] == 1
    assert paper["joinable_to_candidates"] == 1
    assert paper["unjoinable_to_candidates"] == 0
    assert paper["rows_with_valid_liquidity"] == 1
    assert paper["coverage_rate"] == 1.0
    assert paper["by_chain"]["ethereum"]["with_liquidity"] == 1


def test_paper_row_with_null_liquidity_counts_as_missing(audit, db):
    path, conn = db
    conn.execute(
        "INSERT INTO candidates (contract_address, chain, liquidity_usd) "
        "VALUES (?, ?, NULL)",
        ("0xnull", "ethereum"),
    )
    conn.commit()
    rows = [{"source_corpus": "paper", "token_id": "0xnull", "chain": "ethereum"}]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36)
    finally:
        ro.close()
    paper = report["paper_corpus"]
    assert paper["joinable_to_candidates"] == 1
    assert paper["rows_with_valid_liquidity"] == 0
    assert paper["coverage_rate"] == 0.0


def test_paper_row_with_zero_liquidity_counts_as_missing(audit, db):
    path, conn = db
    conn.execute(
        "INSERT INTO candidates (contract_address, chain, liquidity_usd) "
        "VALUES (?, ?, ?)",
        ("0xzero", "ethereum", 0.0),
    )
    conn.commit()
    rows = [{"source_corpus": "paper", "token_id": "0xzero", "chain": "ethereum"}]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36)
    finally:
        ro.close()
    paper = report["paper_corpus"]
    assert paper["joinable_to_candidates"] == 1
    assert paper["rows_with_valid_liquidity"] == 0


def test_paper_row_with_negative_liquidity_counts_as_missing(audit, db):
    path, conn = db
    conn.execute(
        "INSERT INTO candidates (contract_address, chain, liquidity_usd) "
        "VALUES (?, ?, ?)",
        ("0xneg", "ethereum", -1.0),
    )
    conn.commit()
    rows = [{"source_corpus": "paper", "token_id": "0xneg", "chain": "ethereum"}]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36)
    finally:
        ro.close()
    paper = report["paper_corpus"]
    assert paper["joinable_to_candidates"] == 1
    assert paper["rows_with_valid_liquidity"] == 0


def test_paper_row_unjoinable_to_candidates_does_not_count_as_missing_liquidity(
    audit, db
):
    """Critical discipline check from design review B1: an unjoinable row
    must not be silently attributed to 'missing liquidity'."""
    path, _ = db  # candidates table is empty
    rows = [
        {"source_corpus": "paper", "token_id": "0xnotpresent", "chain": "ethereum"}
    ]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36)
    finally:
        ro.close()
    paper = report["paper_corpus"]
    assert paper["rows"] == 1
    assert paper["joinable_to_candidates"] == 0
    assert paper["unjoinable_to_candidates"] == 1
    assert paper["join_rate"] == 0.0
    assert paper["rows_with_valid_liquidity"] == 0


def test_paper_row_case_insensitive_match_is_joinable(audit, db):
    path, conn = db
    conn.execute(
        "INSERT INTO candidates (contract_address, chain, liquidity_usd) "
        "VALUES (?, ?, ?)",
        ("0xABCDEF", "ethereum", 10000.0),
    )
    conn.commit()
    rows = [
        {"source_corpus": "paper", "token_id": "0xabcdef", "chain": "ethereum"}
    ]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36)
    finally:
        ro.close()
    paper = report["paper_corpus"]
    assert paper["joinable_to_candidates"] == 1
    assert paper["rows_with_valid_liquidity"] == 1


def test_tracker_row_skips_candidates_lookup_entirely(audit, db):
    path, conn = db
    # Populate candidates with a row that COULD coincidentally match if we tried
    conn.execute(
        "INSERT INTO candidates (contract_address, chain, liquidity_usd) "
        "VALUES (?, ?, ?)",
        ("bitcoin", "coingecko", 1000000.0),
    )
    conn.commit()
    rows = [
        {"source_corpus": "tracker", "token_id": "bitcoin", "chain": "coingecko"}
    ]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36)
    finally:
        ro.close()
    # Tracker row counted in tracker bucket, NOT in paper bucket.
    assert report["paper_corpus"]["rows"] == 0
    assert report["tracker_corpus"]["rows"] == 1
    # Structural zero — no candidates lookup even though one would have hit.
    assert report["tracker_corpus"]["rows_with_liquidity_source"] == 0
    assert "structural_note" in report["tracker_corpus"]


def test_multi_chain_paper_rows_break_down_correctly(audit, db):
    path, conn = db
    conn.executemany(
        "INSERT INTO candidates (contract_address, chain, liquidity_usd) VALUES (?, ?, ?)",
        [
            ("0xeth1", "ethereum", 100.0),
            ("0xeth2", "ethereum", None),
            ("solbase58a", "solana", 200.0),
        ],
    )
    conn.commit()
    rows = [
        {"source_corpus": "paper", "token_id": "0xeth1", "chain": "ethereum"},
        {"source_corpus": "paper", "token_id": "0xeth2", "chain": "ethereum"},
        {"source_corpus": "paper", "token_id": "solbase58a", "chain": "solana"},
    ]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36)
    finally:
        ro.close()
    paper = report["paper_corpus"]
    assert paper["rows"] == 3
    assert paper["joinable_to_candidates"] == 3
    assert paper["rows_with_valid_liquidity"] == 2
    eth = paper["by_chain"]["ethereum"]
    sol = paper["by_chain"]["solana"]
    assert eth["rows"] == 2
    assert eth["with_liquidity"] == 1
    assert eth["coverage_rate"] == 0.5
    assert sol["rows"] == 1
    assert sol["with_liquidity"] == 1
    assert sol["coverage_rate"] == 1.0


def test_empty_chain_field_bucketed_separately(audit, db):
    path, conn = db
    conn.execute(
        "INSERT INTO candidates (contract_address, chain, liquidity_usd) "
        "VALUES (?, ?, ?)",
        ("0xempty", "", 50.0),
    )
    conn.commit()
    rows = [{"source_corpus": "paper", "token_id": "0xempty", "chain": ""}]
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", rows, ro, 36)
    finally:
        ro.close()
    paper = report["paper_corpus"]
    # Empty chain should bucket under '<empty>' sentinel, not under '' (since
    # the script normalises empty/None to '<empty>').
    assert "<empty>" in paper["by_chain"]
    assert paper["by_chain"]["<empty>"]["rows"] == 1


def test_schema_findings_use_pragma_runtime(audit, db):
    """Design-review B2 fold: schema_findings must come from PRAGMA, not literals."""
    path, conn = db
    # Drop the liquidity_usd column from candidates by recreating without it
    conn.execute("DROP TABLE candidates")
    conn.execute("CREATE TABLE candidates (contract_address TEXT PRIMARY KEY)")
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report("http://test", [], ro, 36)
    finally:
        ro.close()
    findings = report["schema_findings"]
    # Without the column, PRAGMA must report False — not a hard-coded True.
    assert findings["candidates_has_liquidity_usd"] is False


def test_script_main_smoke_with_empty_payload(audit, db, monkeypatch, capsys):
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
    assert payload["window_hours"] == 36
    assert "audited_at" in payload
    assert payload["audited_at"].endswith("Z")


def test_script_returns_2_on_fetch_failure(audit, db, monkeypatch, capsys):
    import urllib.error

    path, _ = db

    def _fail(*_args, **_kwargs):
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
    assert payload["status"] == "error"
    assert payload["stage"] == "fetch"
