"""Focused tests for investigation/ledger_backfill.py (PR #467 review gate).

Read-only backfill: one CSV row per paper_trades signal-fire with checkpoint
path, max_multiple (peak/entry), best-effort time_to_peak_min from the
gainers_snapshots price series, and realized-vs-fixed-24h columns.
"""

import csv
import importlib.util
import io
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "ledger_backfill", REPO_ROOT / "investigation" / "ledger_backfill.py"
)
ledger_backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger_backfill)


def _mkdb(tmp_path):
    db = tmp_path / "scout.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE paper_trades (token_id TEXT, symbol TEXT, chain TEXT,
           signal_type TEXT, opened_at TEXT, entry_price REAL, status TEXT,
           exit_reason TEXT, pnl_pct REAL, checkpoint_1h_pct REAL,
           checkpoint_6h_pct REAL, checkpoint_24h_pct REAL,
           checkpoint_48h_pct REAL, peak_price REAL, peak_pct REAL)""")
    conn.execute("""CREATE TABLE candidates (contract_address TEXT, quant_score REAL,
           conviction_score REAL, signals_fired TEXT, alerted_at TEXT)""")
    conn.execute("""CREATE TABLE gainers_snapshots (coin_id TEXT, snapshot_at TEXT,
           price_at_snapshot REAL)""")
    conn.execute(
        "INSERT INTO paper_trades VALUES ('mint1', 'wif', 'solana',"
        " 'gainers_early', datetime('now', '-2 days'), 1.0, 'closed',"
        " 'trailing_stop', 40.0, 5.0, 15.0, 30.0, 25.0, 2.0, 100.0)"
    )
    conn.execute(
        "INSERT INTO candidates VALUES ('mint1', 45.0, 60.0, '[\"vol\"]', NULL)"
    )
    # Price series hits >=99% of peak (2.0) one day after open.
    conn.execute(
        "INSERT INTO gainers_snapshots VALUES ('mint1',"
        " datetime('now', '-1 day'), 1.99)"
    )
    conn.commit()
    conn.close()
    return str(db)


def test_backfill_emits_row_with_derived_columns(tmp_path, capsys, monkeypatch):
    db = _mkdb(tmp_path)
    out_path = tmp_path / "out.csv"
    rc = ledger_backfill.main(["--db", db, "--days", "30", "--out", str(out_path)])
    assert rc == 0
    rows = list(csv.DictReader(io.StringIO(out_path.read_text())))
    assert len(rows) == 1
    row = rows[0]
    assert row["token_id"] == "mint1"
    assert row["signal_type"] == "gainers_early"
    assert float(row["max_multiple"]) == 2.0  # peak 2.0 / entry 1.0
    assert row["quant_score"] == "45.0"  # candidates join carried through
    assert float(row["time_to_peak_min"]) > 0  # snapshot series found the peak
    assert row["sim_fixed_24h_pct"] == "30.0"  # fixed-hold proxy = checkpoint_24h
    # Aggregate footer goes to stderr, keeping the CSV clean.
    assert "[ledger_backfill] closed n=1" in capsys.readouterr().err


def test_backfill_window_excludes_old_trades(tmp_path, capsys):
    db = _mkdb(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO paper_trades VALUES ('old', 'old', 'solana', 'x',"
        " datetime('now', '-200 days'), 1.0, 'closed', 'expired', 0,"
        " 0, 0, 0, 0, 1.0, 0)"
    )
    conn.commit()
    conn.close()
    out_path = tmp_path / "out.csv"
    assert (
        ledger_backfill.main(["--db", db, "--days", "30", "--out", str(out_path)]) == 0
    )
    rows = list(csv.DictReader(io.StringIO(out_path.read_text())))
    assert [r["token_id"] for r in rows] == ["mint1"]


def test_python_utilities_compile():
    import py_compile

    for script in ("case_replay.py", "ledger_backfill.py"):
        py_compile.compile(str(REPO_ROOT / "investigation" / script), doraise=True)
