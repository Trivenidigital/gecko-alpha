"""Tests for investigation/ruling_response_queries.sh — the machine-captured
evidence collector for the 2026-07-20 ruling's operator-side items."""

import sqlite3
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "investigation" / "ruling_response_queries.sh"

REVIVAL_TS = "2026-07-17T12:28:52.954712"


def test_script_syntax_ok():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_script_uses_exact_revival_timestamp_and_readonly():
    text = SCRIPT.read_text()
    assert REVIVAL_TS in text
    # Every sqlite3 invocation must be read-only.
    for line in text.splitlines():
        if "sqlite3" in line:
            assert "-readonly" in line, f"non-readonly sqlite3 call: {line}"


def _mkdb(tmp_path):
    db = tmp_path / "scout.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE paper_trades (token_id TEXT, symbol TEXT,
           signal_type TEXT, opened_at TEXT, closed_at TEXT, status TEXT,
           exit_reason TEXT, entry_price REAL, exit_price REAL, pnl_usd REAL,
           pnl_pct REAL, position_size_usd REAL, floor_armed INTEGER,
           remaining_qty REAL)""")
    conn.execute("""CREATE TABLE gainers_snapshots (coin_id TEXT, snapshot_at TEXT,
           price_at_snapshot REAL)""")
    # Post-revival cohort: same token traded twice (unique_tokens < trade_rows).
    for opened in ("2026-07-18T00:00:00", "2026-07-19T00:00:00"):
        conn.execute(
            "INSERT INTO paper_trades VALUES ('mintX', 'xxx', 'gainers_early',"
            f" '{opened}', NULL, 'open', NULL, 1.0, NULL, NULL, NULL,"
            " 100.0, 0, NULL)"
        )
    # Boundary-window trade (opened inside 12:28:00 -> 12:28:52.954712).
    conn.execute(
        "INSERT INTO paper_trades VALUES ('mintW', 'www', 'gainers_early',"
        " '2026-07-17T12:28:30.000000', '2026-07-18T00:00:00', 'closed_expired',"
        " 'trailing_stop', 1.0, 1.1, 10.0, 10.0, 100.0, 0, NULL)"
    )
    conn.commit()
    conn.close()
    return str(db)


def test_end_to_end_sections_and_boundary_window(tmp_path):
    db = _mkdb(tmp_path)
    csv_out = tmp_path / "cf.csv"
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GECKO_DB": db,
            "CSV_OUT": str(csv_out),
        },
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    for marker in ("=== A.", "=== B.", "=== C.", "=== C2.", "=== D."):
        assert marker in out, f"missing section {marker}"
    # Boundary-window trade must surface in section B.
    assert "mintW" in out
    # Repeat-exposure token must surface in C2, and D must emit the CSV.
    assert "mintX" in out
    assert csv_out.exists()
    assert "incremental_benefit" in csv_out.read_text().splitlines()[0]
