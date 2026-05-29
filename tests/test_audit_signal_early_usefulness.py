"""Tests for the signal early tradable usefulness scorecard (offline audit).

Mirrors the conventions of ``tests/test_audit_price_path_coverage.py``:
importlib module loading, ``tmp_path`` sqlite, a ``FIXED_NOW`` injected into
``build_report`` for determinism, and read-only connections via a ``mode=ro``
URI. This audit is DB-only / no-network, so there is no ``_fetch_*`` mock.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 5, 29, 22, 0, 0, tzinfo=timezone.utc)

# Allow-list of permitted top-level report keys (review fix 12: includes total_rows).
ALLOWED_TOP_LEVEL_KEYS = {
    "audited_at",
    "params",
    "total_rows",
    "signals",
    "schema_findings",
}

# Anything matching this regex anywhere in the (recursive) key space is a
# descriptive-only contract violation: no ranking / alerting / verdict surface.
FORBIDDEN_KEY_RE = re.compile(
    r"rank|score|order|label|alert|urgency|priorit", re.IGNORECASE
)


@pytest.fixture(scope="module")
def audit():
    spec = importlib.util.spec_from_file_location(
        "audit_signal_early_usefulness",
        ROOT / "scripts" / "audit_signal_early_usefulness.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_signal_early_usefulness"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# Schema helpers — create the real tables with the columns the audit reads.
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE paper_trades (
    id INTEGER PRIMARY KEY,
    token_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    chain TEXT NOT NULL,
    entry_price REAL NOT NULL,
    actionable INTEGER,
    actionability_reason TEXT,
    UNIQUE(token_id, signal_type, opened_at)
);
CREATE TABLE volume_history_cg (
    id INTEGER PRIMARY KEY,
    coin_id TEXT NOT NULL,
    price REAL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE gainers_comparisons (
    id INTEGER PRIMARY KEY,
    coin_id TEXT NOT NULL,
    appeared_on_gainers_at TEXT NOT NULL,
    detected_price REAL,
    peak_price REAL,
    peak_gain_pct REAL
);
CREATE TABLE paper_trade_entry_snapshots (
    paper_trade_id INTEGER PRIMARY KEY,
    liquidity_usd_at_entry REAL,
    actionability_reason_at_entry TEXT,
    actionable_at_entry INTEGER,
    FOREIGN KEY (paper_trade_id) REFERENCES paper_trades(id)
);
"""


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "scout.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    yield path, conn
    conn.close()


def _ro_conn(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _iso(hours_after_now_negative):
    """Return an ISO timestamp ``hours`` AFTER FIXED_NOW (negative = before)."""
    return (FIXED_NOW + timedelta(hours=hours_after_now_negative)).isoformat()


def _insert_trade(
    conn,
    *,
    trade_id,
    token_id,
    signal_type,
    hours_before_now,
    entry_price,
    symbol="SYM",
    chain="coingecko",
    actionable=None,
    actionability_reason=None,
):
    opened_at = (FIXED_NOW - timedelta(hours=hours_before_now)).isoformat()
    conn.execute(
        "INSERT INTO paper_trades "
        "(id, token_id, symbol, signal_type, opened_at, chain, entry_price, "
        "actionable, actionability_reason) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            trade_id,
            token_id,
            symbol,
            signal_type,
            opened_at,
            chain,
            entry_price,
            actionable,
            actionability_reason,
        ),
    )


def _insert_point(conn, coin_id, price, t0_hours_before_now, minutes_after_t0):
    """Insert a volume point at (t0 + minutes_after_t0). t0 = now - t0_hours."""
    t0 = FIXED_NOW - timedelta(hours=t0_hours_before_now)
    ts = (t0 + timedelta(minutes=minutes_after_t0)).isoformat()
    conn.execute(
        "INSERT INTO volume_history_cg (coin_id, price, recorded_at) VALUES (?,?,?)",
        (coin_id, price, ts),
    )


def _insert_point_abs(conn, coin_id, price, ts_iso):
    conn.execute(
        "INSERT INTO volume_history_cg (coin_id, price, recorded_at) VALUES (?,?,?)",
        (coin_id, price, ts_iso),
    )


def _insert_gainer(conn, coin_id, appeared_at_iso, detected_price=None):
    conn.execute(
        "INSERT INTO gainers_comparisons "
        "(coin_id, appeared_on_gainers_at, detected_price) VALUES (?,?,?)",
        (coin_id, appeared_at_iso, detected_price),
    )


def _insert_snapshot(
    conn, paper_trade_id, *, liquidity=None, actionable=None, reason=None
):
    conn.execute(
        "INSERT INTO paper_trade_entry_snapshots "
        "(paper_trade_id, liquidity_usd_at_entry, actionability_reason_at_entry, "
        "actionable_at_entry) VALUES (?,?,?,?)",
        (paper_trade_id, liquidity, reason, actionable),
    )


def _run(audit, db, **overrides):
    path, conn = db
    conn.commit()
    kwargs = dict(
        horizons_h=[1, 4, 24],
        min_n=5,
        min_n_dist=10,
        fav_eps=0.01,
        lookback_days=7,
        dedup=True,
        now=FIXED_NOW,
    )
    kwargs.update(overrides)
    ro = _ro_conn(path)
    try:
        return audit.build_report(ro, **kwargs)
    finally:
        ro.close()


def _populate(conn, token_id, signal_type, n, *, base_id=0, chain="coingecko"):
    """Insert n joinable trades for a signal, each with one in-window peak point."""
    for i in range(n):
        tid = base_id + i + 1
        tok = f"{token_id}-{i}"
        _insert_trade(
            conn,
            trade_id=tid,
            token_id=tok,
            signal_type=signal_type,
            hours_before_now=48,  # mature for all horizons
            entry_price=100.0,
            chain=chain,
        )
        # one favorable point at +30m within all horizons
        _insert_point(conn, tok, 110.0, 48, 30)


# --------------------------------------------------------------------------
# 1. Per-metric correctness (P0 = entry_price)
# --------------------------------------------------------------------------


def test_entry_price_is_p0_for_all_signals(audit, db):
    path, conn = db
    # chain row: entry_price 100, peak point 150 => mfe 0.5 (NOT relative to first point)
    _insert_trade(
        conn,
        trade_id=1,
        token_id="0xabc",
        signal_type="chain_completed",
        hours_before_now=48,
        entry_price=100.0,
        chain="ethereum",
    )
    _insert_point(conn, "0xabc", 120.0, 48, 10)  # first point 120 (not P0)
    _insert_point(conn, "0xabc", 150.0, 48, 40)
    # gainers row: entry_price 10, peak 12 => mfe 0.2
    _insert_trade(
        conn,
        trade_id=2,
        token_id="ethereum",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=10.0,
        chain="coingecko",
    )
    _insert_point(conn, "ethereum", 11.0, 48, 5)
    _insert_point(conn, "ethereum", 12.0, 48, 50)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    chain = report["signals"]["chain_completed"]["metrics"]
    gain = report["signals"]["gainers_early"]["metrics"]
    # mfe relative to entry_price, not first volume point
    assert chain["mfe_1h"]["dist"]["max"] == pytest.approx(0.5)
    assert gain["mfe_1h"]["dist"]["max"] == pytest.approx(0.2)


def test_time_to_peak_picks_argmax_within_max_horizon(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "tok", 105.0, 48, 10)
    _insert_point(conn, "tok", 130.0, 48, 90)  # peak at +90m
    _insert_point(conn, "tok", 120.0, 48, 200)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    m = report["signals"]["gainers_early"]["metrics"]
    assert m["time_to_peak_within_max_horizon_minutes"]["max"] == pytest.approx(90.0)


def test_peak_at_window_edge_flag(audit, db):
    path, conn = db
    # detection 2h ago, max horizon 24h => immature window; peak on last point => edge
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=2,
        entry_price=100.0,
    )
    _insert_point(conn, "tok", 110.0, 2, 30)
    _insert_point(conn, "tok", 140.0, 2, 110)  # last point, still rising
    report = _run(audit, db, min_n=1, min_n_dist=1)
    m = report["signals"]["gainers_early"]["metrics"]
    # window not mature (t0+24h > now) and peak is last point => edge rate 1.0
    assert m["peak_at_window_edge_rate"] == pytest.approx(1.0)


def test_mfe_per_horizon_uses_only_in_window_points(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "tok", 105.0, 48, 30)  # +5% at 30m (in 1h,4h,24h)
    _insert_point(conn, "tok", 112.0, 48, 180)  # +12% at 3h (in 4h,24h)
    _insert_point(conn, "tok", 140.0, 48, 20 * 60)  # +40% at 20h (in 24h only)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    m = report["signals"]["gainers_early"]["metrics"]
    assert m["mfe_1h"]["dist"]["max"] == pytest.approx(0.05)
    assert m["mfe_4h"]["dist"]["max"] == pytest.approx(0.12)
    assert m["mfe_24h"]["dist"]["max"] == pytest.approx(0.40)


def test_mfe_horizon_with_no_points_in_window_is_none(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    # only a point at +3h => nothing in the 1h window
    _insert_point(conn, "tok", 130.0, 48, 180)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    m = report["signals"]["gainers_early"]["metrics"]
    # 1h has zero in-window observations -> n 0, dist None (not a 0 value)
    assert m["mfe_1h"]["n"] == 0
    assert m["mfe_1h"]["dist"] is None
    assert m["mfe_4h"]["dist"]["max"] == pytest.approx(0.30)


def test_mae_before_favorable_respects_fav_eps(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    # +0.5% blip (NOT favorable at eps 0.01), then dip -8%, then +2% favorable
    _insert_point(conn, "tok", 100.5, 48, 5)
    _insert_point(conn, "tok", 92.0, 48, 20)
    _insert_point(conn, "tok", 102.0, 48, 40)  # first > +1%
    report = _run(audit, db, min_n=1, min_n_dist=1)
    m = report["signals"]["gainers_early"]["metrics"]
    # mae over pre-favorable window = -8%
    assert m["mae_before_favorable"]["dist"]["min"] == pytest.approx(-0.08)


def test_mae_zero_when_favorable_on_first_point(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "tok", 105.0, 48, 5)  # first point already favorable
    _insert_point(conn, "tok", 90.0, 48, 30)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    m = report["signals"]["gainers_early"]["metrics"]
    assert m["mae_before_favorable"]["dist"]["min"] == pytest.approx(0.0)
    assert m["mae_before_favorable"]["dist"]["max"] == pytest.approx(0.0)


def test_mae_full_window_when_never_favorable(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "tok", 98.0, 48, 5)
    _insert_point(conn, "tok", 80.0, 48, 30)  # worst -20%, never favorable
    report = _run(audit, db, min_n=1, min_n_dist=1)
    m = report["signals"]["gainers_early"]["metrics"]
    assert m["mae_before_favorable"]["dist"]["min"] == pytest.approx(-0.20)
    assert m["favorable_reached_rate"] == pytest.approx(0.0)


# --------------------------------------------------------------------------
# 2. Metric 4 — at-detection fact flags + venue permanently None
# --------------------------------------------------------------------------


def test_fact_flags_true_when_snapshot_present(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "tok", 110.0, 48, 30)
    _insert_snapshot(conn, 1, liquidity=50000.0, actionable=1, reason="ok")
    report = _run(audit, db, min_n=1, min_n_dist=1)
    facts = report["signals"]["gainers_early"]["metrics"]["at_detection_facts"]
    assert facts["fresh_price_rate"] == pytest.approx(1.0)
    assert facts["liquidity_fact_rate"] == pytest.approx(1.0)
    assert facts["actionable_rate"] == pytest.approx(1.0)


def test_fact_flags_none_when_snapshot_table_absent(audit, db):
    path, conn = db
    conn.execute("DROP TABLE paper_trade_entry_snapshots")
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "tok", 110.0, 48, 30)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    facts = report["signals"]["gainers_early"]["metrics"]["at_detection_facts"]
    # no snapshot table => cohort-neutral flags collapse to None (not False), and
    # surfaced in schema_findings.
    assert facts["fresh_price_rate"] is None
    assert facts["liquidity_fact_rate"] is None
    assert report["schema_findings"]["paper_trade_entry_snapshots_present"] is False


def test_venue_route_flag_permanently_none(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "tok", 110.0, 48, 30)
    _insert_snapshot(conn, 1, liquidity=50000.0, actionable=1, reason="ok")
    report = _run(audit, db, min_n=1, min_n_dist=1)
    facts = report["signals"]["gainers_early"]["metrics"]["at_detection_facts"]
    # venue route is dropped: ALWAYS null, never True/False even with full snapshot.
    assert facts["venue_route_rate"] is None
    assert "venue_route_unsupported_reason" in report["schema_findings"]


def test_correct_sidecar_table_name_pragma(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "tok", 110.0, 48, 30)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    sf = report["schema_findings"]
    assert "paper_trade_entry_snapshots_present" in sf
    assert "actionability_entry_snapshot_present" not in sf
    assert sf["paper_trade_entry_snapshots_present"] is True


def test_fresh_price_cohort_neutral_not_from_detected_price_for_non_gainers(audit, db):
    path, conn = db
    # chain row WITH a snapshot but NO gainers_comparisons join
    _insert_trade(
        conn,
        trade_id=1,
        token_id="0xchain",
        signal_type="chain_completed",
        hours_before_now=48,
        entry_price=100.0,
        chain="ethereum",
    )
    _insert_point(conn, "0xchain", 110.0, 48, 30)
    _insert_snapshot(conn, 1, liquidity=1000.0, actionable=1)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    facts = report["signals"]["chain_completed"]["metrics"]["at_detection_facts"]
    # fresh price True via snapshot presence, not via detected_price (which is absent)
    assert facts["fresh_price_rate"] == pytest.approx(1.0)
    assert report["signals"]["chain_completed"]["metric5_data_path_available"] is False


# --------------------------------------------------------------------------
# 3. Metric 5 — appeared-on-gainers timing + unsupported_for_signal
# --------------------------------------------------------------------------


def test_metric5_before_peak(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="ethereum",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "ethereum", 110.0, 48, 30)
    _insert_point(conn, "ethereum", 130.0, 48, 120)  # peak at +120m
    # appeared on gainers at +30m, before the peak
    _insert_gainer(conn, "ethereum", _iso(-48 + 0.5), detected_price=100.0)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    timing = report["signals"]["gainers_early"]["metrics"]["appeared_on_gainers_timing"]
    assert timing["before_peak"] == 1
    assert timing["after_peak"] == 0


def test_metric5_after_peak(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="ethereum",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "ethereum", 130.0, 48, 30)  # peak at +30m
    _insert_point(conn, "ethereum", 110.0, 48, 120)
    # appeared at +60m, AFTER the peak
    _insert_gainer(conn, "ethereum", _iso(-48 + 1.0), detected_price=100.0)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    timing = report["signals"]["gainers_early"]["metrics"]["appeared_on_gainers_timing"]
    assert timing["after_peak"] == 1
    assert timing["before_peak"] == 0


def test_metric5_surfaced_no_move(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="ethereum",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    # No volume points => no observed move, but gainers row exists.
    _insert_gainer(conn, "ethereum", _iso(-48 + 0.5), detected_price=100.0)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    sig = report["signals"]["gainers_early"]
    # row unjoinable to volume_history_cg; metric5 still supported (gainers join).
    assert sig["metric5_data_path_available"] is True


def test_metric5_unsupported_for_non_gainers_signal(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="0xchain",
        signal_type="chain_completed",
        hours_before_now=48,
        entry_price=100.0,
        chain="ethereum",
    )
    _insert_point(conn, "0xchain", 110.0, 48, 30)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    sig = report["signals"]["chain_completed"]
    assert sig["metrics"]["appeared_on_gainers_timing"] == "unsupported_for_signal"
    assert sig["metric5_data_path_available"] is False
    # explicitly NOT a false "not_surfaced" / 0
    assert sig["metrics"]["appeared_on_gainers_timing"] != "not_surfaced"
    assert sig["metrics"]["appeared_on_gainers_timing"] != 0


def test_metric5_not_surfaced_in_supported_cohort(audit, db):
    path, conn = db
    # Two gainers tokens: one joins gainers_comparisons, the other does not.
    _insert_trade(
        conn,
        trade_id=1,
        token_id="ethereum",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "ethereum", 110.0, 48, 30)
    _insert_gainer(conn, "ethereum", _iso(-48 + 0.5), detected_price=100.0)
    _insert_trade(
        conn,
        trade_id=2,
        token_id="bitcoin",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "bitcoin", 110.0, 48, 30)
    # bitcoin NOT in gainers_comparisons
    report = _run(audit, db, min_n=1, min_n_dist=1)
    timing = report["signals"]["gainers_early"]["metrics"]["appeared_on_gainers_timing"]
    # supported cohort, but one token has no surface ts -> not_surfaced bucket
    assert timing["not_surfaced"] == 1


# --------------------------------------------------------------------------
# 4. n-gate INSUFFICIENT_DATA + LOW_CONFIDENCE
# --------------------------------------------------------------------------


def test_signal_below_binary_floor_emits_insufficient_data(audit, db):
    path, conn = db
    _populate(conn, "tok", "gainers_early", 4)  # 4 joinable < min_n 5
    report = _run(audit, db, min_n=5, min_n_dist=10)
    sig = report["signals"]["gainers_early"]
    assert sig["status"] == "INSUFFICIENT_DATA"
    assert "metrics" not in sig
    assert sig["n_joinable"] == 4


def test_signal_at_binary_floor_emits_metrics(audit, db):
    path, conn = db
    _populate(conn, "tok", "gainers_early", 5)  # exactly 5
    report = _run(audit, db, min_n=5, min_n_dist=10)
    sig = report["signals"]["gainers_early"]
    assert sig["status"] == "OK"
    assert "metrics" in sig


def test_distribution_below_min_n_dist_marks_low_confidence(audit, db):
    path, conn = db
    _populate(conn, "tok", "gainers_early", 7)  # 7 joinable, >=5 binary, <10 dist
    report = _run(audit, db, min_n=5, min_n_dist=10)
    m = report["signals"]["gainers_early"]["metrics"]
    assert m["mfe_1h"]["low_confidence"] is True
    assert m["mfe_1h"]["dist"] is None  # no false percentiles on n=7


def test_distribution_at_min_n_dist_emits_dist(audit, db):
    path, conn = db
    _populate(conn, "tok", "gainers_early", 10)  # exactly 10
    report = _run(audit, db, min_n=5, min_n_dist=10)
    m = report["signals"]["gainers_early"]["metrics"]
    assert m["mfe_1h"]["low_confidence"] is False
    assert m["mfe_1h"]["dist"] is not None


def test_custom_min_n_and_min_n_dist_via_args(audit, db):
    path, conn = db
    _populate(conn, "tok", "gainers_early", 3)
    report = _run(audit, db, min_n=2, min_n_dist=3)
    m = report["signals"]["gainers_early"]["metrics"]
    assert m["mfe_1h"]["low_confidence"] is False
    assert m["mfe_1h"]["dist"] is not None


# --------------------------------------------------------------------------
# 5. Immature / no-lookahead gating
# --------------------------------------------------------------------------


def test_immature_24h_window_excluded_from_mfe_aggregate(audit, db):
    path, conn = db
    # detection 2h ago: 1h horizon mature, 24h horizon immature
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=2,
        entry_price=100.0,
    )
    _insert_point(conn, "tok", 110.0, 2, 30)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    m = report["signals"]["gainers_early"]["metrics"]
    assert m["mfe_24h"]["immature_excluded"] == 1
    assert m["mfe_24h"]["n"] == 0
    # present in 1h (mature)
    assert m["mfe_1h"]["n"] == 1


def test_immature_max_horizon_excludes_time_to_peak_and_mae(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=2,  # max horizon 24h immature
        entry_price=100.0,
    )
    _insert_point(conn, "tok", 110.0, 2, 30)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    m = report["signals"]["gainers_early"]["metrics"]
    assert m["time_to_peak_immature_excluded"] == 1
    assert m["mae_immature_excluded"] == 1
    assert m["time_to_peak_within_max_horizon_minutes"] is None
    assert m["mae_before_favorable"]["n"] == 0


def test_window_elapsed_fraction_reported(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=12,  # 12h elapsed of 24h => 0.5
        entry_price=100.0,
    )
    _insert_point(conn, "tok", 110.0, 12, 30)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    m = report["signals"]["gainers_early"]["metrics"]
    assert m["mfe_24h"]["window_elapsed_fraction"] == pytest.approx(0.5)
    assert m["mfe_1h"]["window_elapsed_fraction"] == pytest.approx(1.0)


def test_detection_at_or_after_now_has_empty_path(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=0,  # detected exactly at now
        entry_price=100.0,
    )
    # a point "after" now should not be reachable (cutoff capped at now)
    _insert_point(conn, "tok", 200.0, 0, 60)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    sig = report["signals"]["gainers_early"]
    assert sig["n_joinable"] == 0
    assert sig["n_unjoinable"] == 1


# --------------------------------------------------------------------------
# 6. Intra-signal dedup
# --------------------------------------------------------------------------


def test_dedup_collapses_repeat_fires_to_earliest(audit, db):
    path, conn = db
    # same (token, signal) fired at -48h and -45h
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_trade(
        conn,
        trade_id=2,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=45,
        entry_price=200.0,
    )
    _insert_point(conn, "tok", 110.0, 48, 30)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    sig = report["signals"]["gainers_early"]
    assert sig["n_total"] == 1  # collapsed
    assert sig["multi_fire_rows"] == 1
    # earliest opened_at (entry_price 100) used: mfe 0.10
    assert sig["metrics"]["mfe_1h"]["dist"]["max"] == pytest.approx(0.10)


def test_dedup_does_not_collapse_across_signals(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_trade(
        conn,
        trade_id=2,
        token_id="tok",
        signal_type="chain_completed",
        hours_before_now=48,
        entry_price=100.0,
        chain="ethereum",
    )
    _insert_point(conn, "tok", 110.0, 48, 30)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    assert report["signals"]["gainers_early"]["n_total"] == 1
    assert report["signals"]["chain_completed"]["n_total"] == 1


def test_no_dedup_flag_keeps_all_rows(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_trade(
        conn,
        trade_id=2,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=45,
        entry_price=100.0,
    )
    _insert_point(conn, "tok", 110.0, 48, 30)
    _insert_point(conn, "tok", 110.0, 45, 30)
    report = _run(audit, db, dedup=False, min_n=1, min_n_dist=1)
    sig = report["signals"]["gainers_early"]
    assert sig["n_total"] == 2
    assert sig["multi_fire_rows"] == 1


# --------------------------------------------------------------------------
# 7. Corpus tag + join framing
# --------------------------------------------------------------------------


def test_corpus_tag_present_per_signal(audit, db):
    path, conn = db
    # chain-sourced (contract addr) => micro-cap
    _insert_trade(
        conn,
        trade_id=1,
        token_id="0xdeadbeef",
        signal_type="chain_completed",
        hours_before_now=48,
        entry_price=100.0,
        chain="ethereum",
    )
    _insert_point(conn, "0xdeadbeef", 110.0, 48, 30)
    # CG-watcher (slug) => cg-watcher
    _insert_trade(
        conn,
        trade_id=2,
        token_id="ethereum",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
        chain="coingecko",
    )
    _insert_point(conn, "ethereum", 110.0, 48, 30)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    assert report["signals"]["chain_completed"]["corpus"] == "micro-cap"
    assert report["signals"]["gainers_early"]["corpus"] == "cg-watcher"


def test_n_joinable_unjoinable_present_in_every_metric_block(audit, db):
    path, conn = db
    _populate(conn, "tok", "gainers_early", 5)
    report = _run(audit, db, min_n=5, min_n_dist=1)
    sig = report["signals"]["gainers_early"]
    assert "n_joinable" in sig
    assert "n_unjoinable" in sig
    assert "n_total" in sig


def test_comparability_warning_present(audit, db):
    path, conn = db
    _populate(conn, "tok", "gainers_early", 5)
    report = _run(audit, db, min_n=5, min_n_dist=1)
    assert "comparability_warning" in report["signals"]["gainers_early"]


# --------------------------------------------------------------------------
# 8. Joinable vs unjoinable
# --------------------------------------------------------------------------


def test_contract_address_token_id_unjoinable_reported(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="0xnotinvolhist",
        signal_type="chain_completed",
        hours_before_now=48,
        entry_price=100.0,
        chain="ethereum",
    )
    # no volume_history_cg row for that address
    report = _run(audit, db, min_n=1, min_n_dist=1)
    sig = report["signals"]["chain_completed"]
    assert sig["n_unjoinable"] == 1
    assert sig["n_joinable"] == 0


def test_cg_slug_token_id_joins_directly(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="ethereum",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "ethereum", 110.0, 48, 30)
    report = _run(audit, db, min_n=1, min_n_dist=1)
    assert report["signals"]["gainers_early"]["n_joinable"] == 1


# --------------------------------------------------------------------------
# 9. Null / zero / negative price exclusion
# --------------------------------------------------------------------------


def test_null_zero_negative_price_excluded(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    _insert_point(conn, "tok", None, 48, 5)
    _insert_point(conn, "tok", 0.0, 48, 6)
    _insert_point(conn, "tok", -5.0, 48, 7)
    _insert_point(conn, "tok", 1e309, 48, 8)  # +inf-ish, guarded out
    _insert_point(conn, "tok", 110.0, 48, 30)  # only valid point
    report = _run(audit, db, min_n=1, min_n_dist=1)
    m = report["signals"]["gainers_early"]["metrics"]
    assert m["mfe_1h"]["dist"]["max"] == pytest.approx(0.10)


# --------------------------------------------------------------------------
# 10. Boundary inclusivity
# --------------------------------------------------------------------------


def test_boundary_points_at_t0_and_t0_plus_h_counted(audit, db):
    path, conn = db
    _insert_trade(
        conn,
        trade_id=1,
        token_id="tok",
        signal_type="gainers_early",
        hours_before_now=48,
        entry_price=100.0,
    )
    # exactly at t0 (+0m) and exactly at t0+1h (+60m) both count for 1h horizon
    _insert_point(conn, "tok", 101.0, 48, 0)
    _insert_point(conn, "tok", 105.0, 48, 60)
    # just outside: t0 - 1s and t0 + 1h + 1s
    t0 = FIXED_NOW - timedelta(hours=48)
    _insert_point_abs(conn, "tok", 999.0, (t0 - timedelta(seconds=1)).isoformat())
    _insert_point_abs(
        conn, "tok", 999.0, (t0 + timedelta(hours=1, seconds=1)).isoformat()
    )
    report = _run(audit, db, min_n=1, min_n_dist=1)
    m = report["signals"]["gainers_early"]["metrics"]
    # only the +1% and +5% are in the 1h window; the 999.0 outliers excluded
    assert m["mfe_1h"]["dist"]["max"] == pytest.approx(0.05)


# --------------------------------------------------------------------------
# 11. main() exit paths
# --------------------------------------------------------------------------


def test_main_rejects_bad_horizons(audit, db, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setattr(
        sys, "argv", ["audit", "--db", str(path), "--horizons", "0,abc", "--json"]
    )
    rc = audit.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert json.loads(out)["stage"] == "args"


def test_main_rejects_horizon_above_168(audit, db, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setattr(
        sys, "argv", ["audit", "--db", str(path), "--horizons", "200", "--json"]
    )
    rc = audit.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert json.loads(out)["stage"] == "args"


def test_main_rejects_min_n_below_1(audit, db, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setattr(
        sys, "argv", ["audit", "--db", str(path), "--min-n", "0", "--json"]
    )
    rc = audit.main()
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["stage"] == "args"


def test_main_rejects_min_n_dist_below_1(audit, db, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setattr(
        sys, "argv", ["audit", "--db", str(path), "--min-n-dist", "0", "--json"]
    )
    rc = audit.main()
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["stage"] == "args"


def test_main_rejects_negative_fav_eps(audit, db, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setattr(
        sys, "argv", ["audit", "--db", str(path), "--fav-eps", "-0.1", "--json"]
    )
    rc = audit.main()
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["stage"] == "args"


def test_main_db_open_failure_returns_2(audit, tmp_path, monkeypatch, capsys):
    missing = tmp_path / "does_not_exist.db"
    monkeypatch.setattr(sys, "argv", ["audit", "--db", str(missing), "--json"])
    rc = audit.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert json.loads(out)["stage"] == "db_open"


def test_main_query_failure_returns_2(audit, tmp_path, monkeypatch, capsys):
    # A valid DB file that lacks the paper_trades table -> query failure.
    path = tmp_path / "scout.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(sys, "argv", ["audit", "--db", str(path), "--json"])
    rc = audit.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert json.loads(out)["stage"] == "query"


def test_main_smoke_empty_db_returns_0(audit, db, monkeypatch, capsys):
    path, _ = db  # tables exist, no rows
    monkeypatch.setattr(sys, "argv", ["audit", "--db", str(path), "--json"])
    rc = audit.main()
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["audited_at"].endswith("Z")
    assert payload["params"]["min_n_dist"] == 10
    assert payload["params"]["fav_eps"] == 0.01
    assert payload["total_rows"] == 0


# --------------------------------------------------------------------------
# 12. Read-only / no-network enforcement
# --------------------------------------------------------------------------


def test_ro_connection_blocks_writes(audit, db):
    path, _ = db
    ro = _ro_conn(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute(
                "INSERT INTO paper_trades "
                "(token_id, symbol, signal_type, opened_at, chain, entry_price) "
                "VALUES ('x','X','s','2026-01-01T00:00:00+00:00','c',1.0)"
            )
    finally:
        ro.close()


def test_module_imports_no_network(audit):
    src = (ROOT / "scripts" / "audit_signal_early_usefulness.py").read_text(
        encoding="utf-8"
    )
    assert "urllib" not in src
    assert "aiohttp" not in src
    assert "import requests" not in src
    # build_report takes no url argument
    import inspect

    params = inspect.signature(audit.build_report).parameters
    assert "url" not in params
    assert "endpoint_url" not in params


def test_module_imports_no_business_modules(audit):
    src = (ROOT / "scripts" / "audit_signal_early_usefulness.py").read_text(
        encoding="utf-8"
    )
    assert "import scout." not in src
    assert "from scout" not in src


# --------------------------------------------------------------------------
# 13. schema_findings PRAGMA runtime
# --------------------------------------------------------------------------


def test_schema_findings_pragma_runtime_missing_entry_price(audit, db):
    path, conn = db
    conn.execute("DROP TABLE paper_trades")
    conn.execute(
        "CREATE TABLE paper_trades ("
        "id INTEGER PRIMARY KEY, token_id TEXT NOT NULL, symbol TEXT NOT NULL, "
        "signal_type TEXT NOT NULL, opened_at TEXT NOT NULL, chain TEXT NOT NULL)"
    )
    conn.commit()
    report = _run(audit, db, min_n=1, min_n_dist=1)
    assert report["schema_findings"]["paper_trades_has_entry_price"] is False


def test_schema_findings_pragma_runtime_missing_price(audit, db):
    path, conn = db
    conn.execute("DROP TABLE volume_history_cg")
    conn.execute("CREATE TABLE volume_history_cg (coin_id TEXT, recorded_at TEXT)")
    conn.commit()
    report = _run(audit, db, min_n=1, min_n_dist=1)
    assert report["schema_findings"]["volume_history_cg_has_price"] is False
    assert report["schema_findings"]["volume_history_cg_has_recorded_at"] is True


# --------------------------------------------------------------------------
# 14. _float_distribution fork
# --------------------------------------------------------------------------


def test_float_distribution_handles_floats(audit):
    vals = [-0.4, -0.12, -0.03, 0.0, 0.0, 0.05, 0.11, 0.3, 0.9, -0.06]
    dist = audit._float_distribution(vals, min_samples=10)
    assert dist is not None
    assert isinstance(dist["min"], float)
    assert dist["min"] == pytest.approx(-0.4)
    assert dist["max"] == pytest.approx(0.9)
    # not the int-typed reference helper
    assert audit._float_distribution is not getattr(audit, "_points_distribution", None)
    assert not hasattr(audit, "_points_distribution")


def test_float_distribution_respects_configurable_floor(audit):
    nine = [float(i) / 10 for i in range(9)]
    assert audit._float_distribution(nine, min_samples=10) is None
    ten = [float(i) / 10 for i in range(10)]
    assert audit._float_distribution(ten, min_samples=10) is not None


# --------------------------------------------------------------------------
# 15. Output contract
# --------------------------------------------------------------------------


def _walk_keys(obj):
    keys = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.append(k)
            keys.extend(_walk_keys(v))
    elif isinstance(obj, list):
        for item in obj:
            keys.extend(_walk_keys(item))
    return keys


def test_top_level_keys_allowlist_includes_total_rows(audit, db):
    path, conn = db
    _populate(conn, "tok", "gainers_early", 5)
    report = _run(audit, db, min_n=5, min_n_dist=1)
    assert set(report.keys()) <= ALLOWED_TOP_LEVEL_KEYS
    assert "total_rows" in report
    # No key anywhere in the tree matches the forbidden ranking/alert regex.
    offenders = [k for k in _walk_keys(report) if FORBIDDEN_KEY_RE.search(k)]
    assert offenders == [], f"forbidden keys present: {offenders}"
