"""Focused tests for investigation/case_replay.py (PR #467 review gate).

Evidence discipline: a query failure (missing table, renamed/missing column,
incompatible schema) must yield verdict=indeterminate_query_error carrying
the exact error — never a synthesized seen/unseen/alerted classification —
and the process must exit nonzero (3) so automation notices.
"""

import importlib.util
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "case_replay", REPO_ROOT / "investigation" / "case_replay.py"
)
case_replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(case_replay)


FULL_SCHEMA = [
    """CREATE TABLE candidates (contract_address TEXT, chain TEXT, ticker TEXT,
       token_name TEXT, first_seen_at TEXT, quant_score REAL,
       conviction_score REAL, signals_fired TEXT, alerted_at TEXT,
       market_cap_usd REAL)""",
    """CREATE TABLE gainers_snapshots (coin_id TEXT, symbol TEXT, name TEXT,
       snapshot_at TEXT, price_change_24h REAL, price_at_snapshot REAL)""",
    "CREATE TABLE trending_snapshots (coin_id TEXT, snapshot_at TEXT)",
    """CREATE TABLE gainers_comparisons (coin_id TEXT, symbol TEXT,
       appeared_on_gainers_at TEXT, detected_by_pipeline INTEGER,
       pipeline_lead_minutes REAL, detected_by_narrative INTEGER,
       narrative_lead_minutes REAL)""",
    "CREATE TABLE tg_alert_log (token_id TEXT, outcome TEXT)",
    """CREATE TABLE paper_trades (token_id TEXT, symbol TEXT,
       signal_type TEXT, opened_at TEXT, entry_price REAL, status TEXT,
       exit_reason TEXT, pnl_pct REAL, peak_pct REAL,
       checkpoint_24h_pct REAL)""",
    """CREATE TABLE price_cache (coin_id TEXT, current_price REAL,
       market_cap REAL, updated_at TEXT)""",
]


def _mkdb(tmp_path, statements):
    db = tmp_path / "scout.db"
    conn = sqlite3.connect(db)
    for stmt in statements:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    return str(db)


def test_never_seen_verdict_on_full_schema(tmp_path):
    db = _mkdb(tmp_path, FULL_SCHEMA)
    conn = case_replay.ro(db)
    out = case_replay.replay(conn, "ghosttoken")
    assert out["verdict"].startswith("NEVER SEEN")
    assert "query_errors" not in out


def test_candidate_never_alerted_verdict(tmp_path):
    db = _mkdb(
        tmp_path,
        FULL_SCHEMA
        + [
            "INSERT INTO candidates VALUES ('mint1', 'solana', 'wif', 'dogwifhat',"
            " '2026-06-01T00:00:00', 40.0, 55.0, '[\"vol\"]', NULL, 1000.0)"
        ],
    )
    conn = case_replay.ro(db)
    out = case_replay.replay(conn, "wif")
    assert "never alerted" in out["verdict"]


def test_missing_candidates_table_is_indeterminate_never_alerted(tmp_path):
    """The reviewer's exact failure mode: with `candidates` missing, the old
    code fell into the final else and fabricated 'alerted — compare alert
    time vs pump start'. It must be indeterminate with the exact error."""
    schema = [s for s in FULL_SCHEMA if "CREATE TABLE candidates" not in s]
    db = _mkdb(tmp_path, schema)
    conn = case_replay.ro(db)
    out = case_replay.replay(conn, "anything")
    assert out["verdict"] == "indeterminate_query_error"
    assert "no such table" in out["query_errors"]["candidate"]
    assert "alerted" not in out["verdict"]


def test_missing_required_column_is_indeterminate(tmp_path):
    schema = [
        (
            s
            if "CREATE TABLE candidates" not in s
            else """CREATE TABLE candidates (contract_address TEXT, chain TEXT,
           ticker TEXT, token_name TEXT, first_seen_at TEXT, quant_score REAL,
           signals_fired TEXT, alerted_at TEXT, market_cap_usd REAL)"""
        )
        for s in FULL_SCHEMA
    ]  # conviction_score column removed
    db = _mkdb(tmp_path, schema)
    conn = case_replay.ro(db)
    out = case_replay.replay(conn, "anything")
    assert out["verdict"] == "indeterminate_query_error"
    assert "conviction_score" in out["query_errors"]["candidate"]


def test_main_exits_3_on_indeterminate_and_0_on_clean(tmp_path, capsys):
    clean_db = _mkdb(tmp_path, FULL_SCHEMA)
    assert case_replay.main(["--db", clean_db, "sometoken"]) == 0
    broken = tmp_path / "broken"
    broken.mkdir()
    broken_db = _mkdb(
        broken, [s for s in FULL_SCHEMA if "CREATE TABLE candidates" not in s]
    )
    assert case_replay.main(["--db", broken_db, "sometoken"]) == 3
