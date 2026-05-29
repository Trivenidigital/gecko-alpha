"""Tests for the clean price-path runner-attribution audit diagnostic.

Mirrors the fixture/loader style of ``test_audit_price_path_coverage.py``:
importlib spec_from_file_location loader, a ``tmp_path`` sqlite fixture, a
module-scoped ``FIXED_NOW``, an ``_ro_conn`` helper, and an
``_insert_point(conn, coin_id, price, hours_after_detection)`` helper.

TDD: written before the implementation. One test per bucket plus boundaries,
the review folds (#1-#8), and the offline / read-only / no-import contract.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "audit_clean_price_path.py"

# Pinned "now". Detections used in tests are placed well before this so their
# maturity windows have elapsed (except the explicit window_incomplete test).
FIXED_NOW = datetime(2026, 5, 29, 14, 0, 0, tzinfo=timezone.utc)
# Default detection instant used by most tests: 10 days before now so a 168h
# (7d) window is fully matured.
DETECTION = FIXED_NOW - timedelta(days=10)
DETECTION_ISO = DETECTION.isoformat()


@pytest.fixture(scope="module")
def audit():
    spec = importlib.util.spec_from_file_location("audit_clean_price_path", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_clean_price_path"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "scout.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE volume_history_cg (
            id INTEGER PRIMARY KEY,
            coin_id TEXT NOT NULL,
            price REAL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id TEXT NOT NULL,
            symbol TEXT,
            signal_type TEXT NOT NULL,
            entry_price REAL NOT NULL,
            opened_at TEXT NOT NULL
        );
        CREATE TABLE gainers_comparisons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin_id TEXT NOT NULL,
            symbol TEXT,
            appeared_on_gainers_at TEXT NOT NULL,
            detected_price REAL,
            peak_price REAL,
            peak_gain_pct REAL
        );
        """)
    conn.commit()
    yield path, conn
    conn.close()


def _ro_conn(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _insert_point(conn, coin_id, price, hours_after_detection, detection=DETECTION):
    ts = (detection + timedelta(hours=hours_after_detection)).isoformat()
    conn.execute(
        "INSERT INTO volume_history_cg (coin_id, price, recorded_at) VALUES (?, ?, ?)",
        (coin_id, price, ts),
    )


# --- common kwargs for build_report (defaults mirror §1) --------------------
DEFAULTS = dict(
    window_hours=168,
    run_threshold=30.0,
    drawdown_threshold=15.0,
    flat_gap_hours=48.0,
    flat_band_pct=10.0,
    min_points=5,
    maturity_hours=168.0,
    now=FIXED_NOW,
)


def _cohort_row(
    coin_id, detected_price=None, source="paper", detection_ts=DETECTION_ISO
):
    return {
        "coin_id": coin_id,
        "detection_ts": detection_ts,
        "detected_price": detected_price,
        "cohort_source": source,
    }


def _bucket_of(report, coin_id):
    for r in report["per_row"]:
        if r["coin_id"] == coin_id:
            return r
    raise AssertionError(f"{coin_id} not in per_row")


# ============================================================================
# Bucket tests + boundaries
# ============================================================================
def test_continuous_move(audit, db):
    """Ran (MFE>=run), prompt (no long flat span), shallow dip (MAE<=drawdown)."""
    path, conn = db
    cid = "cont"
    # P0=100; gentle climb to 140 (MFE=40%), small dip to 95 (MAE=5%).
    _insert_point(conn, cid, 100.0, 0)
    _insert_point(conn, cid, 95.0, 2)
    _insert_point(conn, cid, 110.0, 4)
    _insert_point(conn, cid, 125.0, 6)
    _insert_point(conn, cid, 140.0, 8)
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, 100.0)], ro, **DEFAULTS)
    finally:
        ro.close()
    row = _bucket_of(report, cid)
    assert row["bucket"] == "continuous_move"
    assert row["mfe"] == pytest.approx(40.0)
    assert row["mae"] == pytest.approx(5.0)


def test_drawdown_then_recovery(audit, db):
    """Ran, prompt, but pre-peak dip OVER drawdown_threshold."""
    path, conn = db
    cid = "ddr"
    # P0=100; deep dip to 75 (MAE=25% > 15%), then run to 150 (MFE=50%).
    _insert_point(conn, cid, 100.0, 0)
    _insert_point(conn, cid, 75.0, 4)
    _insert_point(conn, cid, 90.0, 8)
    _insert_point(conn, cid, 120.0, 12)
    _insert_point(conn, cid, 150.0, 16)
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, 100.0)], ro, **DEFAULTS)
    finally:
        ro.close()
    row = _bucket_of(report, cid)
    assert row["bucket"] == "drawdown_then_recovery"
    assert row["mae"] == pytest.approx(25.0)


def test_drawdown_boundary_at_exactly_threshold_is_continuous(audit, db):
    """MAE == drawdown_threshold (15%) => continuous_move (<= inclusive)."""
    path, conn = db
    cid = "ddbound"
    _insert_point(conn, cid, 100.0, 0)
    _insert_point(conn, cid, 85.0, 4)  # exactly 15% dip
    _insert_point(conn, cid, 140.0, 8)  # MFE 40%
    _insert_point(conn, cid, 138.0, 10)
    _insert_point(conn, cid, 139.0, 12)
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, 100.0)], ro, **DEFAULTS)
    finally:
        ro.close()
    row = _bucket_of(report, cid)
    assert row["mae"] == pytest.approx(15.0)
    assert row["bucket"] == "continuous_move"


def test_unrelated_later_move(audit, db):
    """Long flat span (>= flat_gap_hours) then a run => unrelated_later_move,
    winning over the dip-based split."""
    path, conn = db
    cid = "later"
    # P0=100; flat near 100 (+/-10%) for 60h (> 48h), then spike to 200.
    _insert_point(conn, cid, 100.0, 0)
    _insert_point(conn, cid, 102.0, 12)
    _insert_point(conn, cid, 98.0, 24)
    _insert_point(conn, cid, 101.0, 48)
    _insert_point(conn, cid, 99.0, 60)
    _insert_point(conn, cid, 200.0, 72)  # MFE 100%
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, 100.0)], ro, **DEFAULTS)
    finally:
        ro.close()
    row = _bucket_of(report, cid)
    assert row["bucket"] == "unrelated_later_move"


def test_flat_gap_boundary_exactly_at_threshold_is_unrelated(audit, db):
    """flat_gap == flat_gap_hours (48h) => unrelated_later_move (>= inclusive)."""
    path, conn = db
    cid = "flatbound"
    _insert_point(conn, cid, 100.0, 0)
    _insert_point(conn, cid, 101.0, 24)
    _insert_point(conn, cid, 99.0, 48)  # flat span first..last = 48h exactly
    _insert_point(conn, cid, 200.0, 60)
    _insert_point(conn, cid, 190.0, 72)
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, 100.0)], ro, **DEFAULTS)
    finally:
        ro.close()
    row = _bucket_of(report, cid)
    assert row["bucket"] == "unrelated_later_move"


def test_run_threshold_boundary_exactly_equal_is_a_run(audit, db):
    """MFE == run_threshold (30%) IS a run (not-ran uses MFE < run_threshold)."""
    path, conn = db
    cid = "runbound"
    _insert_point(conn, cid, 100.0, 0)
    _insert_point(conn, cid, 105.0, 2)
    _insert_point(conn, cid, 110.0, 4)
    _insert_point(conn, cid, 120.0, 6)
    _insert_point(conn, cid, 130.0, 8)  # MFE exactly 30%
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, 100.0)], ro, **DEFAULTS)
    finally:
        ro.close()
    row = _bucket_of(report, cid)
    assert row["mfe"] == pytest.approx(30.0)
    # 30% == run_threshold => a run; shallow dip => continuous_move.
    assert row["bucket"] == "continuous_move"


def test_run_threshold_just_below_is_no_significant_move(audit, db):
    path, conn = db
    cid = "below"
    _insert_point(conn, cid, 100.0, 0)
    _insert_point(conn, cid, 105.0, 2)
    _insert_point(conn, cid, 110.0, 4)
    _insert_point(conn, cid, 120.0, 6)
    _insert_point(conn, cid, 129.0, 8)  # MFE 29% < 30%
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, 100.0)], ro, **DEFAULTS)
    finally:
        ro.close()
    row = _bucket_of(report, cid)
    assert row["bucket"] == "no_significant_move"


def test_no_significant_move(audit, db):
    path, conn = db
    cid = "flat"
    for i, p in enumerate([100.0, 101.0, 99.0, 100.5, 100.0]):
        _insert_point(conn, cid, p, i * 4)
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, 100.0)], ro, **DEFAULTS)
    finally:
        ro.close()
    assert _bucket_of(report, cid)["bucket"] == "no_significant_move"


# ============================================================================
# Residual / guard buckets
# ============================================================================
def test_window_incomplete_when_not_matured(audit, db):
    path, conn = db
    cid = "young"
    recent_detection = FIXED_NOW - timedelta(hours=10)  # 10h < 168h maturity
    _insert_point(conn, cid, 100.0, 0, detection=recent_detection)
    _insert_point(conn, cid, 200.0, 2, detection=recent_detection)
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report(
            [_cohort_row(cid, 100.0, detection_ts=recent_detection.isoformat())],
            ro,
            **DEFAULTS,
        )
    finally:
        ro.close()
    row = _bucket_of(report, cid)
    assert row["bucket"] == "window_incomplete"
    assert row["mfe"] is None
    assert row["mae"] is None
    assert row["time_to_peak"] is None


def test_insufficient_data_when_below_min_points(audit, db):
    path, conn = db
    cid = "sparse"
    _insert_point(conn, cid, 100.0, 0)
    _insert_point(conn, cid, 150.0, 4)  # only 2 points < min_points=5
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, 100.0)], ro, **DEFAULTS)
    finally:
        ro.close()
    row = _bucket_of(report, cid)
    assert row["bucket"] == "insufficient_data"
    assert row["mfe"] is None


def test_insufficient_data_when_p0_unresolvable(audit, db):
    """No detected_price and no valid in-window point => insufficient_data."""
    path, _ = db  # empty volume_history_cg
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row("ghost", None)], ro, **DEFAULTS)
    finally:
        ro.close()
    row = _bucket_of(report, "ghost")
    assert row["bucket"] == "insufficient_data"


def test_p0_falls_back_to_first_valid_in_window_point(audit, db):
    """No ledger detected_price -> P0 = first valid in-window price; basis recorded."""
    path, conn = db
    cid = "fallback"
    for i, p in enumerate([100.0, 110.0, 120.0, 130.0, 140.0]):
        _insert_point(conn, cid, p, i * 2)
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, None)], ro, **DEFAULTS)
    finally:
        ro.close()
    row = _bucket_of(report, cid)
    assert row["p0_basis"] == "first_in_window_point"
    assert row["mfe"] == pytest.approx(40.0)  # (140-100)/100


def test_p0_uses_ledger_detected_price_when_valid(audit, db):
    path, conn = db
    cid = "ledgerp0"
    for i, p in enumerate([200.0, 210.0, 220.0, 230.0, 260.0]):
        _insert_point(conn, cid, p, i * 2)
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, 200.0)], ro, **DEFAULTS)
    finally:
        ro.close()
    row = _bucket_of(report, cid)
    assert row["p0_basis"] == "ledger_detected_price"
    assert row["mfe"] == pytest.approx(30.0)  # (260-200)/200


# ============================================================================
# Price validity + temporal ordering + join (folds #2, CG-slug caveat)
# ============================================================================
def test_null_zero_negative_inf_prices_excluded(audit, db):
    path, conn = db
    cid = "dirty"
    conn.executemany(
        "INSERT INTO volume_history_cg (coin_id, price, recorded_at) VALUES (?, ?, ?)",
        [
            (cid, None, (DETECTION + timedelta(hours=1)).isoformat()),
            (cid, 0.0, (DETECTION + timedelta(hours=2)).isoformat()),
            (cid, -5.0, (DETECTION + timedelta(hours=3)).isoformat()),
            (cid, 1e309, (DETECTION + timedelta(hours=4)).isoformat()),  # +Inf
        ],
    )
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, 100.0)], ro, **DEFAULTS)
    finally:
        ro.close()
    # No valid points => insufficient_data (P0 from ledger but 0 in-window pts).
    assert _bucket_of(report, cid)["bucket"] == "insufficient_data"


def test_pre_detection_points_excluded(audit, db):
    """Points before detection_ts never enter the series."""
    path, conn = db
    cid = "preonly"
    # 5 points all BEFORE detection.
    for i in range(5):
        _insert_point(conn, cid, 100.0 + i, -(i + 1))
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, 100.0)], ro, **DEFAULTS)
    finally:
        ro.close()
    assert _bucket_of(report, cid)["bucket"] == "insufficient_data"


def test_lower_bound_inclusive_at_detection_instant(audit, db):
    """A point at exactly recorded_at == detection_ts IS included (>= inclusive)."""
    path, conn = db
    cid = "atinstant"
    _insert_point(conn, cid, 100.0, 0)  # exactly at detection
    for i, p in enumerate([110.0, 120.0, 130.0, 140.0]):
        _insert_point(conn, cid, p, (i + 1) * 2)
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, None)], ro, **DEFAULTS)
    finally:
        ro.close()
    # 5 points including the at-instant one => classifiable (not insufficient).
    assert _bucket_of(report, cid)["bucket"] != "insufficient_data"


def test_cg_slug_token_joins_contract_address_does_not(audit, db):
    """CG-slug identity joins; raw contract-address identity yields 0 points =>
    insufficient_data (NEW test establishing the behavior for this script)."""
    path, conn = db
    for i, p in enumerate([100.0, 110.0, 120.0, 130.0, 140.0]):
        _insert_point(conn, "pepe", p, i * 2)  # volume_history keyed by CG slug
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report(
            [
                _cohort_row("pepe", 100.0),  # joins
                _cohort_row("0x" + "a" * 40, 100.0),  # contract addr, no join
            ],
            ro,
            **DEFAULTS,
        )
    finally:
        ro.close()
    assert _bucket_of(report, "pepe")["bucket"] != "insufficient_data"
    assert _bucket_of(report, "0x" + "a" * 40)["bucket"] == "insufficient_data"


# ============================================================================
# Fold #3 — unrelated_later_move retains metrics
# ============================================================================
def test_unrelated_later_move_retains_metrics(audit, db):
    path, conn = db
    cid = "later2"
    _insert_point(conn, cid, 100.0, 0)
    _insert_point(conn, cid, 92.0, 12)  # small dip within band
    _insert_point(conn, cid, 101.0, 30)
    _insert_point(conn, cid, 99.0, 54)  # flat span > 48h
    _insert_point(conn, cid, 200.0, 66)
    conn.commit()
    ro = _ro_conn(path)
    try:
        report = audit.build_report([_cohort_row(cid, 100.0)], ro, **DEFAULTS)
    finally:
        ro.close()
    row = _bucket_of(report, cid)
    assert row["bucket"] == "unrelated_later_move"
    assert row["mfe"] is not None
    assert row["mae"] is not None
    assert row["time_to_peak"] is not None


# ============================================================================
# Fold #4 — join_failure_breakdown split by source + identity class
# ============================================================================
def test_join_failure_breakdown_splits_by_source_and_identity_class(audit, db):
    path, _ = db  # empty volume_history => everything insufficient_data
    ro = _ro_conn(path)
    try:
        report = audit.build_report(
            [
                _cohort_row("0x" + "b" * 40, 100.0, source="paper"),
                _cohort_row("0x" + "c" * 40, 100.0, source="paper"),
                _cohort_row("nice-slug", 100.0, source="gainers"),
            ],
            ro,
            **DEFAULTS,
        )
    finally:
        ro.close()
    br = report["join_failure_breakdown"]
    assert br["insufficient_data_total"] == 3
    assert br["by_cohort_source"]["paper"] == 2
    assert br["by_cohort_source"]["gainers"] == 1
    assert br["by_identity_class"]["contract_address_like"] == 2
    assert br["by_identity_class"]["cg_slug_like"] == 1


# ============================================================================
# Fold #5 — gainers runner-def crosscheck, both disagreement directions
# ============================================================================
def test_gainers_crosscheck_counts_both_disagreement_directions(audit, db):
    path, conn = db
    # Row A: audit says no-move (flat), stored peak_gain_pct says ran (48%).
    for i, p in enumerate([100.0, 101.0, 99.0, 100.5, 100.0]):
        _insert_point(conn, "rowA", p, i * 2)
    # Row B: audit says ran (50%), stored peak_gain_pct says not-ran (5%).
    for i, p in enumerate([100.0, 110.0, 120.0, 135.0, 150.0]):
        _insert_point(conn, "rowB", p, i * 2)
    conn.commit()
    rows = [
        {**_cohort_row("rowA", 100.0, source="gainers"), "stored_peak_gain_pct": 48.0},
        {**_cohort_row("rowB", 100.0, source="gainers"), "stored_peak_gain_pct": 5.0},
    ]
    ro = _ro_conn(path)
    try:
        report = audit.build_report(rows, ro, **DEFAULTS)
    finally:
        ro.close()
    cc = report["gainers_runner_def_crosscheck"]
    assert cc["rows_compared"] == 2
    assert cc["disagree_audit_no_stored_yes"] == 1
    assert cc["disagree_audit_yes_stored_no"] == 1


# ============================================================================
# Fold #8 — matured-rate block N<5 suppression
# ============================================================================
def test_matured_rate_block_nulled_below_5(audit, db):
    path, conn = db
    # 3 matured classifiable rows (< 5) => whole block null.
    for cid in ["m1", "m2", "m3"]:
        for i, p in enumerate([100.0, 101.0, 99.0, 100.5, 100.0]):
            _insert_point(conn, cid, p, i * 2)
    conn.commit()
    rows = [_cohort_row(c, 100.0) for c in ["m1", "m2", "m3"]]
    ro = _ro_conn(path)
    try:
        report = audit.build_report(rows, ro, **DEFAULTS)
    finally:
        ro.close()
    assert report["bucket_rates_matured"] is None
    assert report["matured_denominator"] == 3
    assert report["bucket_rates_matured_suppressed_reason"]


def test_matured_rate_block_present_at_or_above_5(audit, db):
    path, conn = db
    for cid in ["a1", "a2", "a3", "a4", "a5"]:
        for i, p in enumerate([100.0, 101.0, 99.0, 100.5, 100.0]):
            _insert_point(conn, cid, p, i * 2)
    conn.commit()
    rows = [_cohort_row(c, 100.0) for c in ["a1", "a2", "a3", "a4", "a5"]]
    ro = _ro_conn(path)
    try:
        report = audit.build_report(rows, ro, **DEFAULTS)
    finally:
        ro.close()
    assert report["matured_denominator"] == 5
    assert report["bucket_rates_matured"] is not None
    assert report["bucket_rates_matured"]["no_significant_move"] == pytest.approx(1.0)


def test_bucket_rates_null_when_cohort_empty(audit, db):
    path, _ = db
    ro = _ro_conn(path)
    try:
        report = audit.build_report([], ro, **DEFAULTS)
    finally:
        ro.close()
    assert report["total_cohort"] == 0
    assert report["bucket_rates_gross"] is None
    assert report["bucket_rates_matured"] is None


# ============================================================================
# Fold #7 — sensitivity sweep (via main with --sensitivity)
# ============================================================================
def _seed_one_runner(conn, cid):
    _insert_point(conn, cid, 100.0, 0)
    _insert_point(conn, cid, 95.0, 2)
    _insert_point(conn, cid, 110.0, 4)
    _insert_point(conn, cid, 125.0, 6)
    _insert_point(conn, cid, 140.0, 8)


def test_sensitivity_grid_has_nine_cells(audit, db, monkeypatch, capsys):
    path, conn = db
    _seed_one_runner(conn, "s1")
    conn.commit()
    monkeypatch.setattr(
        audit,
        "_build_cohort",
        lambda conn, cohort, lookback_days, now: [_cohort_row("s1", 100.0)],
    )
    monkeypatch.setattr(
        sys, "argv", ["audit", "--db", str(path), "--sensitivity", "--json"]
    )
    rc = audit.main()
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert "sensitivity" in payload
    assert len(payload["sensitivity"]["grid"]) == 9
    assert "per_bucket_count_range" in payload["sensitivity"]


def test_sensitivity_block_absent_without_flag(audit, db, monkeypatch, capsys):
    path, conn = db
    _seed_one_runner(conn, "s2")
    conn.commit()
    monkeypatch.setattr(
        audit,
        "_build_cohort",
        lambda conn, cohort, lookback_days, now: [_cohort_row("s2", 100.0)],
    )
    monkeypatch.setattr(sys, "argv", ["audit", "--db", str(path), "--json"])
    rc = audit.main()
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert "sensitivity" not in payload


# ============================================================================
# main() exit-code discipline
# ============================================================================
def test_main_rejects_window_above_ceiling(audit, db, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit", "--db", str(path), "--window-hours", "200", "--json"],
    )
    rc = audit.main()
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["stage"] == "args"


def test_main_rejects_nonpositive_run_threshold(audit, db, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit", "--db", str(path), "--run-threshold", "0", "--json"],
    )
    rc = audit.main()
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["stage"] == "args"


def test_main_rejects_flat_band_ge_run_threshold(audit, db, monkeypatch, capsys):
    """Fold #6: flat_band_pct >= run_threshold => exit 2."""
    path, _ = db
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit",
            "--db",
            str(path),
            "--flat-band-pct",
            "30",
            "--run-threshold",
            "30",
            "--json",
        ],
    )
    rc = audit.main()
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["stage"] == "args"


def test_main_rejects_bad_cohort(audit, db, monkeypatch):
    path, _ = db
    monkeypatch.setattr(
        sys, "argv", ["audit", "--db", str(path), "--cohort", "nonsense", "--json"]
    )
    with pytest.raises(SystemExit):
        audit.main()


def test_main_smoke_empty_cohort_returns_0(audit, db, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setattr(
        audit, "_build_cohort", lambda conn, cohort, lookback_days, now: []
    )
    monkeypatch.setattr(sys, "argv", ["audit", "--db", str(path), "--json"])
    rc = audit.main()
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["total_cohort"] == 0
    assert payload["audited_at"].endswith("Z")
    assert payload["params"]["window_hours"] == 168


def test_main_db_open_failure_returns_2(audit, tmp_path, monkeypatch, capsys):
    missing = tmp_path / "does_not_exist.db"
    monkeypatch.setattr(sys, "argv", ["audit", "--db", str(missing), "--json"])
    rc = audit.main()
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["stage"] == "db_open"


def test_main_cohort_query_failure_returns_2(audit, tmp_path, monkeypatch, capsys):
    """DB exists but cohort table missing => stage='cohort', exit 2."""
    path = tmp_path / "scout.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        sys, "argv", ["audit", "--db", str(path), "--cohort", "paper", "--json"]
    )
    rc = audit.main()
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["stage"] == "cohort"


# ============================================================================
# Contract enforcement
# ============================================================================
def test_read_only_db_blocks_writes(audit, db):
    path, _ = db
    ro = _ro_conn(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute(
                "INSERT INTO volume_history_cg (coin_id, price, recorded_at) "
                "VALUES ('x', 1.0, '2026-01-01T00:00:00')"
            )
    finally:
        ro.close()


def test_offline_banner_present_in_json_and_human(audit, db, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setattr(
        audit, "_build_cohort", lambda conn, cohort, lookback_days, now: []
    )
    # JSON
    monkeypatch.setattr(sys, "argv", ["audit", "--db", str(path), "--json"])
    assert audit.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert "OFFLINE-ONLY" in payload["offline_only_banner"]
    # Human
    monkeypatch.setattr(sys, "argv", ["audit", "--db", str(path)])
    assert audit.main() == 0
    human = capsys.readouterr().out
    assert "OFFLINE-ONLY" in human


def test_offline_banner_in_module_docstring(audit):
    assert "OFFLINE-ONLY" in (audit.__doc__ or "")


def test_no_business_logic_imports(audit):
    """Scan SOURCE TEXT (not __dict__) for scout business-logic imports."""
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "import scout." not in source
    assert "from scout." not in source
    assert "from scout import" not in source
