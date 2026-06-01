from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def audit():
    spec = importlib.util.spec_from_file_location(
        "audit_social_mentions_denominator",
        ROOT / "scripts" / "audit_social_mentions_denominator.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_social_mentions_denominator"] = module
    spec.loader.exec_module(module)
    return module


def _init_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE candidates (
            id INTEGER PRIMARY KEY,
            contract_address TEXT,
            social_mentions_24h INTEGER DEFAULT 0
        );
        CREATE TABLE score_history (
            id INTEGER PRIMARY KEY,
            contract_address TEXT NOT NULL,
            score REAL NOT NULL,
            scanned_at TEXT NOT NULL
        );
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            token_id TEXT NOT NULL,
            pnl_usd REAL
        );
        CREATE TABLE narrative_alerts_inbound (
            id INTEGER PRIMARY KEY,
            received_at TEXT NOT NULL,
            resolved_coin_id TEXT
        );
        CREATE TABLE tg_social_messages (
            id INTEGER PRIMARY KEY,
            parsed_at TEXT NOT NULL,
            contracts TEXT
        );
        CREATE TABLE social_signals (id INTEGER PRIMARY KEY);
        CREATE TABLE social_baselines (id INTEGER PRIMARY KEY);
        CREATE TABLE social_credit_ledger (id INTEGER PRIMARY KEY);
        """)
    conn.executemany(
        "INSERT INTO candidates(contract_address, social_mentions_24h) VALUES (?, ?)",
        [("A", 0), ("B", 0), ("C", 0)],
    )
    conn.executemany(
        "INSERT INTO score_history(contract_address, score, scanned_at) VALUES (?, ?, ?)",
        [
            ("A", 58, "2026-05-31T00:00:00+00:00"),
            ("A", 57, "2026-05-31T01:00:00+00:00"),
            ("B", 60, "2026-05-31T02:00:00+00:00"),
            ("C", 70, "2026-05-31T03:00:00+00:00"),
        ],
    )
    conn.execute("INSERT INTO paper_trades(token_id, pnl_usd) VALUES ('A', 12.5)")
    conn.execute(
        "INSERT INTO narrative_alerts_inbound(received_at, resolved_coin_id) VALUES (?, ?)",
        ("2026-06-01T00:00:00+00:00", "coin-a"),
    )
    conn.execute(
        "INSERT INTO tg_social_messages(parsed_at, contracts) VALUES (?, ?)",
        (
            "2026-06-01T01:00:00+00:00",
            '[{"chain":"ethereum","address":"0xAbC"},'
            '{"chain":"solana","address":"So111"}]',
        ),
    )
    conn.execute(
        "INSERT INTO tg_social_messages(parsed_at, contracts) VALUES (?, ?)",
        ("2026-06-01T02:00:00+00:00", '["0xabc"]'),
    )
    conn.commit()
    conn.close()


def test_build_report_quantifies_dead_social_and_variant_flips(tmp_path, audit):
    db_path = tmp_path / "scout.db"
    _init_db(db_path)

    report = audit.build_report(str(db_path), now=FIXED_NOW)

    assert report["social_mentions"]["total_candidates"] == 3
    assert report["social_mentions"]["would_fire_signal_5"] == 0
    assert report["social_mentions"]["nonzero"] == 0
    assert report["score_history"]["rows"] == 4
    assert report["score_history"]["max_score"] == 70
    assert report["variant_b"]["promoted_min_60_to_65"] == 0
    assert report["variant_b"]["demoted_min_60_to_65"] == 1
    assert report["variant_b"]["promoted_conviction_70_to_75"] == 0
    assert report["variant_b"]["demoted_conviction_70_to_75"] == 0
    assert report["variant_c"]["newly_passes_min_60"] == 2
    assert report["variant_c"]["newly_passes_conviction_70"] == 0
    assert report["variant_c"]["paper_trade_cross_check"]["n_promoted_candidates"] == 1
    assert report["variant_c"]["paper_trade_cross_check"]["n_with_paper_trades"] == 1
    assert report["bridges"]["narrative_alerts_inbound_7d"]["resolved"] == 1
    assert report["bridges"]["tg_social_messages_24h"]["distinct_contracts"] == 2
    assert report["bridges"]["tg_social_messages_24h"]["total_msgs_with_contracts"] == 2
    assert (
        report["bridges"]["tg_social_messages_24h"]["invalid_contract_json_rows"] == 0
    )


def test_open_readonly_prevents_writes(tmp_path, audit):
    db_path = tmp_path / "scout.db"
    _init_db(db_path)

    conn = audit.open_readonly(str(db_path))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO candidates(contract_address) VALUES ('write')")
    conn.close()


def test_missing_required_schema_exits_2(tmp_path, audit, capsys):
    db_path = tmp_path / "broken.db"
    sqlite3.connect(db_path).execute("CREATE TABLE candidates(id INTEGER PRIMARY KEY)")

    rc = audit.main(["--db", str(db_path)])

    captured = capsys.readouterr()
    assert rc == 2
    assert "schema" in captured.err


def test_cli_writes_json_report(tmp_path, audit, capsys):
    db_path = tmp_path / "scout.db"
    _init_db(db_path)
    out_path = tmp_path / "report.json"

    rc = audit.main(["--db", str(db_path), "--output", str(out_path)])

    assert rc == 0
    assert json.loads(out_path.read_text(encoding="utf-8"))["stage"] == "ok"
    assert capsys.readouterr().out == ""


def test_cli_empty_argv_uses_default_db_without_process_args(
    tmp_path, monkeypatch, audit, capsys
):
    _init_db(tmp_path / "scout.db")
    monkeypatch.chdir(tmp_path)

    rc = audit.main([])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["stage"] == "ok"
