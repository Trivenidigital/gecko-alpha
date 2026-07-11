"""ALR-03 universe-contamination report script.

scripts/universe_contamination_report.py is a READ-ONLY backfill count of
CLOSED paper_trades whose token_id matches the universe exclude patterns
(tokenized equities / ETFs). It reports count + realised PnL; it never
deletes or mutates rows.

Windows note: the script imports only sqlite3 + scout.token_ids (no aiohttp),
so it runs on Windows without the OpenSSL Applink hazard. These tests pass
--patterns / patterns explicitly so no Settings is constructed and no .env is
read — fully hermetic.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import universe_contamination_report as report  # noqa: E402


def _make_db(tmp_path):
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            token_id TEXT, symbol TEXT, name TEXT, chain TEXT,
            signal_type TEXT, status TEXT, pnl_usd REAL
        );
        """)
    return db_path, conn


def test_counts_and_sums_only_contaminated_closed_trades(tmp_path):
    _, conn = _make_db(tmp_path)
    conn.executemany(
        "INSERT INTO paper_trades (id, token_id, signal_type, status, pnl_usd) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (1, "spy-bstocks-tokenized-stock", "gainers_early", "closed_sl", -42.5),
            (2, "qcom-tokenized-stock", "volume_spike", "closed_tp", 10.0),
            (3, "pepe", "gainers_early", "closed_tp", 100.0),  # clean
            (
                4,
                "aapl-tokenized-stock",
                "volume_spike",
                "open",
                0.0,
            ),  # contaminated but OPEN
            (5, "bitcoin", "volume_spike", "closed_expired", -5.0),  # clean
        ],
    )
    conn.commit()

    result = report.build_report(conn, ["-tokenized-"])
    conn.close()

    assert result.total_closed == 4  # rows 1,2,3,5 (row 4 is still open)
    assert result.count == 2  # rows 1,2 (row 4 excluded: open)
    assert {t.trade_id for t in result.contaminated} == {1, 2}
    assert result.total_pnl_usd == -32.5  # -42.5 + 10.0
    assert all(t.pattern == "-tokenized-" for t in result.contaminated)


def test_empty_when_no_contamination(tmp_path):
    _, conn = _make_db(tmp_path)
    conn.execute(
        "INSERT INTO paper_trades (id, token_id, signal_type, status, pnl_usd) "
        "VALUES (1, 'pepe', 'gainers_early', 'closed_tp', 100.0)"
    )
    conn.commit()

    result = report.build_report(conn, ["-tokenized-"])
    conn.close()

    assert result.total_closed == 1
    assert result.count == 0
    assert result.total_pnl_usd == 0.0


def test_format_report_is_labeled_read_only(tmp_path):
    _, conn = _make_db(tmp_path)
    conn.execute(
        "INSERT INTO paper_trades (id, token_id, signal_type, status, pnl_usd) "
        "VALUES (1, 'spy-bstocks-tokenized-stock', 'gainers_early', 'closed_sl', -42.5)"
    )
    conn.commit()
    result = report.build_report(conn, ["-tokenized-"])
    conn.close()

    text = report.format_report(result)
    assert "REPORT ONLY" in text
    assert "spy-bstocks-tokenized-stock" in text
    assert "-42.5" in text


def test_main_read_only_end_to_end(tmp_path, capsys):
    db_path, conn = _make_db(tmp_path)
    conn.execute(
        "INSERT INTO paper_trades (id, token_id, signal_type, status, pnl_usd) "
        "VALUES (1, 'spy-bstocks-tokenized-stock', 'gainers_early', 'closed_sl', -42.5)"
    )
    conn.commit()
    conn.close()

    # NB: a pattern value starting with '-' must use the --patterns=VALUE form
    # (argparse would otherwise read a bare "-tokenized-" as another flag).
    rc = report.main(["--db", str(db_path), "--patterns=-tokenized-"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "REPORT ONLY" in out
    assert "spy-bstocks-tokenized-stock" in out
