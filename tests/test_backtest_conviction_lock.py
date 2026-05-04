"""BL-067 backtest tests."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Add scripts/ to sys.path so the test can import the script as a module.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


@pytest.fixture
def db(tmp_path):
    """In-memory sqlite with schema seeded for backtest tests."""
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            token_id TEXT, signal_type TEXT, signal_data TEXT,
            entry_price REAL, amount_usd REAL, quantity REAL,
            tp_pct REAL, sl_pct REAL, tp_price REAL, sl_price REAL,
            status TEXT, opened_at TEXT, closed_at TEXT,
            pnl_usd REAL, pnl_pct REAL, peak_pct REAL, peak_price REAL,
            exit_reason TEXT, signal_combo TEXT,
            symbol TEXT, name TEXT, chain TEXT
        );
        CREATE TABLE gainers_snapshots (
            coin_id TEXT, symbol TEXT, name TEXT, price_at_snapshot REAL,
            market_cap REAL, price_change_24h REAL, snapshot_at TEXT
        );
        CREATE TABLE losers_snapshots (
            coin_id TEXT, symbol TEXT, name TEXT, price_at_snapshot REAL,
            market_cap REAL, price_change_24h REAL, snapshot_at TEXT
        );
        CREATE TABLE trending_snapshots (
            coin_id TEXT, symbol TEXT, name TEXT, price_at_snapshot REAL,
            snapshot_at TEXT
        );
        CREATE TABLE chain_matches (
            id INTEGER PRIMARY KEY, token_id TEXT, pattern_name TEXT,
            outcome_change_pct REAL, completed_at TEXT
        );
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY, coin_id TEXT, predicted_at TEXT
        );
        CREATE TABLE velocity_alerts (
            id INTEGER PRIMARY KEY, coin_id TEXT, detected_at TEXT
        );
        CREATE TABLE volume_spikes (
            id INTEGER PRIMARY KEY, coin_id TEXT, symbol TEXT, name TEXT,
            price REAL, detected_at TEXT
        );
        CREATE TABLE tg_social_signals (
            id INTEGER PRIMARY KEY, token_id TEXT, created_at TEXT
        );
        CREATE TABLE volume_history_cg (
            coin_id TEXT, symbol TEXT, name TEXT, price REAL, recorded_at TEXT
        );
        CREATE TABLE price_cache (
            coin_id TEXT PRIMARY KEY, current_price REAL, market_cap REAL,
            updated_at TEXT
        );
        CREATE TABLE signal_params (
            signal_type TEXT PRIMARY KEY,
            trail_pct REAL, sl_pct REAL, max_duration_hours INTEGER
        );
    """)
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# T1: stack-count helper
# ---------------------------------------------------------------------------


def test_count_stacked_signals_returns_zero_for_isolated_token(db):
    from backtest_conviction_lock import _count_stacked_signals_in_window
    n, sources = _count_stacked_signals_in_window(
        db, "lonely-coin",
        "2026-05-01T00:00:00+00:00", "2026-05-02T00:00:00+00:00"
    )
    assert n == 0
    assert sources == []


def test_count_stacked_signals_counts_distinct_sources(db):
    from backtest_conviction_lock import _count_stacked_signals_in_window
    db.executescript("""
        INSERT INTO gainers_snapshots VALUES ('multi', 'M', 'Multi', 1.0, 1e6, 12.0, '2026-05-01T01:00:00+00:00');
        INSERT INTO trending_snapshots VALUES ('multi', 'M', 'Multi', 1.0, '2026-05-01T02:00:00+00:00');
        INSERT INTO volume_spikes VALUES (1, 'multi', 'M', 'Multi', 1.0, '2026-05-01T03:00:00+00:00');
    """)
    db.commit()
    n, sources = _count_stacked_signals_in_window(
        db, "multi",
        "2026-05-01T00:00:00+00:00", "2026-05-02T00:00:00+00:00"
    )
    assert n == 3
    assert "gainers" in sources
    assert "trending" in sources
    assert "volume_spike" in sources


# ---------------------------------------------------------------------------
# T2: conviction-lock param composition
# ---------------------------------------------------------------------------


def test_conviction_locked_params_for_stack_count():
    from backtest_conviction_lock import conviction_locked_params

    base = {"max_duration_hours": 168, "trail_pct": 20, "sl_pct": 25}
    p = conviction_locked_params(stack=1, base=base)
    assert p["max_duration_hours"] == 168
    assert p["trail_pct"] == 20
    assert p["sl_pct"] == 25

    p = conviction_locked_params(stack=2, base=base)
    assert p["max_duration_hours"] == 240
    assert p["trail_pct"] == 25
    assert p["sl_pct"] == 30

    p = conviction_locked_params(stack=3, base=base)
    assert p["max_duration_hours"] == 336
    assert p["trail_pct"] == 30
    assert p["sl_pct"] == 35

    p = conviction_locked_params(stack=4, base=base)
    assert p["max_duration_hours"] == 504
    assert p["trail_pct"] == 35  # cap
    assert p["sl_pct"] == 40

    p = conviction_locked_params(stack=10, base=base)
    assert p["max_duration_hours"] == 504  # saturated
    assert p["trail_pct"] == 35
    assert p["sl_pct"] == 40


# ---------------------------------------------------------------------------
# T3: price-path reconstruction
# ---------------------------------------------------------------------------


def test_reconstruct_price_path_returns_chronological_prices(db):
    from backtest_conviction_lock import _reconstruct_price_path

    db.executescript("""
        INSERT INTO gainers_snapshots VALUES ('coin', 'C', 'Coin', 1.0, 1e6, 5.0, '2026-05-01T01:00:00+00:00');
        INSERT INTO gainers_snapshots VALUES ('coin', 'C', 'Coin', 1.5, 1e6, 5.0, '2026-05-01T05:00:00+00:00');
        INSERT INTO volume_history_cg VALUES ('coin', 'C', 'Coin', 1.2, '2026-05-01T03:00:00+00:00');
        INSERT INTO volume_spikes VALUES (1, 'coin', 'C', 'Coin', 0.9, '2026-04-30T23:00:00+00:00');
    """)
    db.commit()

    path = _reconstruct_price_path(
        db, "coin",
        start="2026-05-01T00:00:00+00:00",
        end="2026-05-01T06:00:00+00:00",
    )
    # 3 in-window samples; volume_spike at -1h is out
    assert len(path) == 3
    prices = [p[1] for p in path]
    assert 1.0 in prices
    assert 1.2 in prices
    assert 1.5 in prices


# ---------------------------------------------------------------------------
# T4: simulator
# ---------------------------------------------------------------------------


def test_simulate_exit_hits_stop_loss():
    from backtest_conviction_lock import _simulate_conviction_locked_exit
    path = [
        ("2026-05-01T00:30:00+00:00", 0.95),
        ("2026-05-01T01:00:00+00:00", 0.85),
        ("2026-05-01T02:00:00+00:00", 0.79),
    ]
    result = _simulate_conviction_locked_exit(
        entry_price=1.0,
        opened_at="2026-05-01T00:00:00+00:00",
        params={"max_duration_hours": 168, "trail_pct": 20, "sl_pct": 20},
        price_path=path,
    )
    assert result["exit_reason"] == "stop_loss"
    # N4 fix: exit at observed price (0.79), not at threshold (0.80)
    assert result["exit_price"] == pytest.approx(0.79, rel=0.01)


def test_simulate_exit_hits_trailing_stop():
    from backtest_conviction_lock import _simulate_conviction_locked_exit
    path = [
        ("2026-05-01T01:00:00+00:00", 1.30),
        ("2026-05-01T02:00:00+00:00", 1.50),  # peak +50%
        ("2026-05-01T03:00:00+00:00", 1.20),  # = 1.50 * 0.80
    ]
    result = _simulate_conviction_locked_exit(
        entry_price=1.0,
        opened_at="2026-05-01T00:00:00+00:00",
        params={"max_duration_hours": 168, "trail_pct": 20, "sl_pct": 20},
        price_path=path,
        moonshot_enabled=False,  # disable to isolate trail-only behavior
        peak_fade_enabled=False,
    )
    assert result["exit_reason"] == "trailing_stop"
    assert result["peak_pct"] == pytest.approx(50.0, abs=0.5)


def test_simulate_exit_max_duration():
    from backtest_conviction_lock import _simulate_conviction_locked_exit
    path = [
        ("2026-05-01T01:00:00+00:00", 1.05),
        ("2026-05-01T02:00:00+00:00", 1.10),
        ("2026-05-08T00:01:00+00:00", 1.15),  # 168h+ later (slightly past)
    ]
    result = _simulate_conviction_locked_exit(
        entry_price=1.0,
        opened_at="2026-05-01T00:00:00+00:00",
        params={"max_duration_hours": 168, "trail_pct": 20, "sl_pct": 20},
        price_path=path,
    )
    assert result["exit_reason"] == "expired"
    assert result["pnl_pct"] == pytest.approx(15.0, abs=0.5)


def test_simulate_exit_arms_moonshot_at_40pct():
    """A2: peak >= 40 + moonshot_enabled → trail switches to max(base, 30)."""
    from backtest_conviction_lock import _simulate_conviction_locked_exit
    path = [
        ("2026-05-01T01:00:00+00:00", 1.20),  # +20% — would arm trail at 20%
        ("2026-05-01T02:00:00+00:00", 1.45),  # +45% — moonshot arms (>= 40)
        # With base trail 20% but moonshot active, effective trail = 30%
        # Trail stop = 1.45 * 0.70 = 1.015 — price drops to 1.05 below 1.015
        ("2026-05-01T03:00:00+00:00", 1.00),  # below 1.015 → trail fires
    ]
    result = _simulate_conviction_locked_exit(
        entry_price=1.0,
        opened_at="2026-05-01T00:00:00+00:00",
        params={"max_duration_hours": 168, "trail_pct": 20, "sl_pct": 20},
        price_path=path,
        peak_fade_enabled=False,  # isolate moonshot vs peak-fade
    )
    assert result["moonshot_armed"] is True
    assert result["exit_reason"] == "trailing_stop"
    # Peak was 45%, exit at 1.00 (below moonshot trail at 1.015)
    assert result["peak_pct"] == pytest.approx(45.0, abs=0.5)


def test_simulate_exit_peak_fade_fires_after_60pct_peak():
    """MF2: peak >= 60% + retraces 30pp → close at observed price."""
    from backtest_conviction_lock import _simulate_conviction_locked_exit
    path = [
        ("2026-05-01T01:00:00+00:00", 1.20),
        ("2026-05-01T02:00:00+00:00", 1.65),  # +65% — peak-fade armed
        ("2026-05-01T03:00:00+00:00", 1.30),  # +30% — retraced 35pp from peak
    ]
    result = _simulate_conviction_locked_exit(
        entry_price=1.0,
        opened_at="2026-05-01T00:00:00+00:00",
        # Wide trail (40%) so trailing_stop doesn't fire first at 1.65*0.60=0.99
        params={"max_duration_hours": 168, "trail_pct": 40, "sl_pct": 25},
        price_path=path,
        moonshot_enabled=False,  # isolate peak-fade
    )
    # Either trailing_stop or peak_fade is acceptable depending on order;
    # peak-fade check happens after trail-arm but before next iteration.
    # With wide trail (40%) trail_stop_price = 1.65 * 0.60 = 0.99; price 1.30 > 0.99
    # so trail does NOT fire. Peak-fade fires (peak 65 - retrace 30 = threshold 35;
    # current 30 < 35).
    assert result["exit_reason"] == "peak_fade"
    assert result["peak_pct"] == pytest.approx(65.0, abs=0.5)


# ---------------------------------------------------------------------------
# T4b: helper tests
# ---------------------------------------------------------------------------


def test_load_signal_params_returns_row_when_present(db):
    from backtest_conviction_lock import _load_signal_params
    db.execute(
        "INSERT INTO signal_params VALUES "
        "('gainers_early', 14.0, 20.0, 96)"
    )
    db.commit()
    p = _load_signal_params(db, "gainers_early")
    assert p["trail_pct"] == 14.0
    assert p["sl_pct"] == 20.0
    assert p["max_duration_hours"] == 96


def test_load_signal_params_falls_back_when_missing(db):
    from backtest_conviction_lock import _load_signal_params
    p = _load_signal_params(db, "unknown_signal")
    # Defaults: trail=20, sl=25, max=168
    assert p["trail_pct"] == 20.0
    assert p["sl_pct"] == 25.0
    assert p["max_duration_hours"] == 168


def test_path_density_returns_zero_for_empty_path():
    from backtest_conviction_lock import _path_density_score
    assert _path_density_score(
        [],
        opened_at="2026-05-01T00:00:00+00:00",
        end_at="2026-05-02T00:00:00+00:00",
    ) == 0.0


def test_path_density_returns_one_for_hourly_samples():
    from backtest_conviction_lock import _path_density_score
    # 24 samples over 24 hours = density 1.0
    path = [
        (f"2026-05-01T{h:02d}:00:00+00:00", 1.0)
        for h in range(24)
    ]
    d = _path_density_score(
        path,
        opened_at="2026-05-01T00:00:00+00:00",
        end_at="2026-05-02T00:00:00+00:00",
    )
    assert d == pytest.approx(1.0, abs=0.05)


def test_min_iso_ts_handles_mixed_formats():
    """N3: lex-min on different datetime string formats is undefined."""
    from backtest_conviction_lock import _min_iso_ts
    # T-form vs space-form, same instant + 1 hour
    a = "2026-05-01T13:00:00+00:00"
    b = "2026-05-01 14:00:00"
    result = _min_iso_ts(a, b)
    # 13:00 should win
    assert "13:00:00" in result


def test_resolve_as_of_flags_default(db):
    """ASF2: default usage flagged for non-reproducibility warning."""
    from backtest_conviction_lock import _resolve_as_of
    as_of, was_default = _resolve_as_of(None, db)
    assert was_default is True
    assert as_of  # non-empty

    as_of2, was_default2 = _resolve_as_of("2026-05-04T12:00:00+00:00", db)
    assert was_default2 is False
    assert as_of2 == "2026-05-04T12:00:00+00:00"
