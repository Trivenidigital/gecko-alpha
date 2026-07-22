"""Tests for investigation/time_death_counterfactual.py (review ruling
2026-07-20: separate measured / dry-run-era / unresolved evidence; per-trade
incremental_benefit; forward-walk replay with no look-ahead)."""

import csv
import importlib.util
import io
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "time_death_counterfactual",
    REPO_ROOT / "investigation" / "time_death_counterfactual.py",
)
tdc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tdc)

RULES = dict(
    sl_pct=15.0,
    trail_activation_pct=10.0,
    trail_drawdown_pct=10.0,
    trail_floor_pct=3.0,
    fade_min_peak_pct=10.0,
    fade_retrace_ratio=0.7,
    floor_armed=False,
)


def test_replay_trailing_stop_no_lookahead():
    """Peak first, then retrace: trailing fires on the FIRST snapshot at/below
    the trigger — the later higher price must not be consulted."""
    path = [
        ("t1", 1.20),  # peak 20% → trailing armed
        ("t2", 1.07),  # <= 1.20*0.9=1.08 → fires HERE
        ("t3", 2.00),  # would be better; must never be reached
    ]
    r = tdc.replay_counterfactual(1.0, 1.0, path, "t9", **RULES)
    assert r["resolved"] and r["exit_reason"] == "trailing_stop"
    assert r["exit_price"] == 1.07 and r["exit_ts"] == "t2"


def test_replay_stop_loss_fires():
    r = tdc.replay_counterfactual(1.0, 1.0, [("t1", 0.84)], "t9", **RULES)
    assert r["resolved"] and r["exit_reason"] == "stop_loss"


def test_replay_unresolved_when_coverage_ends():
    r = tdc.replay_counterfactual(1.0, 1.0, [("t1", 1.02), ("t2", 1.03)], "t9", **RULES)
    assert not r["resolved"]


def test_replay_expiry_boundary_closes_at_last_covered_price():
    path = [("t1", 1.02), ("t2", 1.04), ("t8", 1.50)]  # t8 beyond boundary t5
    r = tdc.replay_counterfactual(1.0, 1.0, path, "t5", **RULES)
    assert r["resolved"] and r["exit_reason"] == "expired"
    assert r["exit_price"] == 1.04  # last price INSIDE coverage, no look-ahead


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
    # LIVE time_death close: entry 1.0, killed flat at 1.0 (pnl 0), $100 size.
    conn.execute(
        "INSERT INTO paper_trades VALUES ('mintA', 'aaa', 'gainers_early',"
        " '2026-07-18T00:00:00', '2026-07-19T00:00:00', 'closed_expired',"
        " 'time_death', 1.0, 1.0, 0.0, 0.0, 100.0, 0, NULL)"
    )
    # Post-close path: rallies to 1.30 then trailing-stops at 1.15.
    for ts, p in [
        ("2026-07-19T01:00:00", 1.10),
        ("2026-07-19T02:00:00", 1.30),
        ("2026-07-19T03:00:00", 1.15),
    ]:
        conn.execute("INSERT INTO gainers_snapshots VALUES ('mintA', ?, ?)", (ts, p))
    # DRY-RUN-ERA close (before cutoff), resolvable path.
    conn.execute(
        "INSERT INTO paper_trades VALUES ('mintB', 'bbb', 'gainers_early',"
        " '2026-07-10T00:00:00', '2026-07-11T00:00:00', 'closed_expired',"
        " 'time_death', 2.0, 2.0, 0.0, 0.0, 100.0, 0, NULL)"
    )
    conn.execute(
        "INSERT INTO gainers_snapshots VALUES"
        " ('mintB', '2026-07-11T01:00:00', 1.6)"  # stop_loss (-20%)
    )
    # LIVE close with NO post-close coverage → unresolved.
    conn.execute(
        "INSERT INTO paper_trades VALUES ('mintC', 'ccc', 'gainers_early',"
        " '2026-07-18T00:00:00', '2026-07-19T12:00:00', 'closed_expired',"
        " 'time_death', 1.0, 1.0, 0.0, 0.0, 100.0, 0, NULL)"
    )
    conn.commit()
    conn.close()
    return str(db)


def test_end_to_end_separates_eras_and_coverage(tmp_path, capsys):
    db = _mkdb(tmp_path)
    out_path = tmp_path / "out.csv"
    rc = tdc.main(
        [
            "--db",
            db,
            "--dry-run-cutoff-ts",
            "2026-07-17T12:28:52.954712Z",
            "--out",
            str(out_path),
        ]
    )
    assert rc == 0
    rows = {r["token_id"]: r for r in csv.DictReader(io.StringIO(out_path.read_text()))}

    live = rows["mintA"]
    assert live["era"] == "live" and live["coverage_class"] == "measured"
    assert live["counterfactual_exit_reason"] == "trailing_stop"
    # exit 1.15 on entry 1.0, $100 → cf +15.00; actual 0 → incremental -15.00
    assert float(live["counterfactual_normal_exit_pnl"]) == 15.0
    assert float(live["incremental_benefit"]) == -15.0

    dry = rows["mintB"]
    assert dry["era"] == "dry_run_era" and dry["coverage_class"] == "measured"
    assert dry["counterfactual_exit_reason"] == "stop_loss"

    unres = rows["mintC"]
    assert unres["coverage_class"] == "unresolved_coverage"
    assert unres["counterfactual_normal_exit_pnl"] == ""

    # Headline (stderr) is MEASURED-LIVE ONLY: excludes dry-run + unresolved.
    err = capsys.readouterr().err
    assert "measured_live=1" in err
    assert "incremental_benefit=-15.0" in err
    assert "coverage_pct=66.7" in err
