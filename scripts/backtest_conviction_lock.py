"""BL-067 backtest: Conviction-lock simulation.

Per backlog.md:367 BL-067 spec, this script:

A. Computes the signal-stack distribution across closed paper trades in
   the last 30d (descriptive context for threshold choice).

B. Simulates conviction-locked exit gates against actual closed trades.
   For each trade with stack >= threshold (N=2 AND N=3 swept), replays
   the exit logic with extended max_duration / trail_pct / sl_pct per
   the BL-067 spec table. Compute simulated PnL delta vs baseline (also
   simulated, for apples-to-apples) AND vs actual production PnL.

B2. First-entry hold simulation: for each token where N>=2 over its life,
    simulate holding ONLY THE FIRST paper trade (skipping subsequent
    ones). Reproduces operator's mental model (LAB +$531 hypothetical).

C. Replays BIO + LAB case studies — per-trade actual vs simulated.

D. BIO-like cohort survey: how many tokens hit N>=3 stacked signals
    over a TRUE 7d rolling window (1-hour step, MF1 fix).

Run on VPS (or against a copied snapshot):
    cd /root/gecko-alpha && uv run python scripts/backtest_conviction_lock.py \
        --db /root/gecko-alpha/scout.db \
        --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --days 30

No production changes. Pure analysis. Per backlog.md:412 resume protocol,
the conviction-lock production code (`scout/trading/conviction.py`) is
NOT in scope of this PR — it's gated on this backtest's output passing
the decision gate (lift >= 15% AND |delta_vs_baseline| >= $100 AND
locked_count >= 5 AND delta_vs_actual >= 0).

# TODO: dedupe _count_stacked_signals_in_window via scripts/_backtest_lib.py
# if a 3rd backtest needs it. For now, copied from
# scripts/backtest_v1_signal_stacking.py:68-159 verbatim with M3 baseline
# fix applied here only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("scout.db")
TRADE_SIZE_USD = 300.0
MAX_LOCKED_HOURS = 504  # stack=4 ceiling

# Mirrors PAPER_MOONSHOT_* constants in scout/config.py:267-272
_MOONSHOT_THRESHOLD_PCT = 40.0
_MOONSHOT_TRAIL_DRAWDOWN_PCT = 30.0

# MF2 fix: peak-fade single-threshold approximation. Production has more
# nuanced logic; this is the residual-asymmetry-reducing version. Ladder
# remains skipped + documented in the F2 failure mode.
_PEAK_FADE_THRESHOLD_PCT = 60.0
_PEAK_FADE_RETRACE_PCT = 30.0


def _h(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    return c


def _parse_iso(ts: str) -> datetime:
    """Parse ISO-8601 timestamp; tolerate trailing 'Z' and '+00:00'.
    Also tolerates SQLite's `'2026-05-04 12:00:00'` space-separated form."""
    s = ts.replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    if "+" not in s and "-" not in s[10:]:
        s = s + "+00:00"
    return datetime.fromisoformat(s)


def _min_iso_ts(a: str, b: str) -> str:
    """N3 fix: lex-min on datetime strings is undefined when formats differ.
    Parse both → min → isoformat."""
    return min(_parse_iso(a), _parse_iso(b)).isoformat()


# ---------------------------------------------------------------------------
# Helpers (M3, S4, A1)
# ---------------------------------------------------------------------------


# SF-M1 (PR #68 silent-failure-hunter): track signal_types that fell back
# to defaults so the JSON + markdown surface this state. Without this,
# baseline_total can be computed against the wrong baseline silently.
_signal_params_fallback_seen: set[str] = set()


def _load_signal_params(
    conn: sqlite3.Connection, signal_type: str
) -> dict:
    """M3 fix: load per-signal-type baseline from signal_params table.
    Falls back to settings defaults if signal_params missing or row absent.
    SF-M1 fix: track + warn on fallback so operator sees if defaults were used."""
    try:
        cur = conn.execute(
            "SELECT trail_pct, sl_pct, max_duration_hours "
            "FROM signal_params WHERE signal_type = ?",
            (signal_type,),
        )
        row = cur.fetchone()
        if row:
            return {
                "trail_pct": float(row[0]),
                "sl_pct": float(row[1]),
                "max_duration_hours": int(row[2]),
            }
    except sqlite3.OperationalError:
        pass
    # Fallback: post-paper-lifecycle-widening defaults (memory
    # project_paper_lifecycle_widen_2026_04_27.md): max=168h, sl=25%,
    # trail=20%.
    if signal_type not in _signal_params_fallback_seen:
        _signal_params_fallback_seen.add(signal_type)
        print(
            f"WARN: signal_params fallback for {signal_type!r} → defaults "
            f"(trail=20, sl=25, max=168). Baseline simulation may diverge "
            f"from production for this signal_type.",
            file=sys.stderr,
        )
    return {"trail_pct": 20.0, "sl_pct": 25.0, "max_duration_hours": 168}


def _path_density_score(
    price_path: list[tuple[str, float]],
    *,
    opened_at: str,
    end_at: str,
) -> float:
    """S4 fix: density = samples / expected_hourly_samples in window.
    Trades with density < 0.2 flagged + excluded from headline lift."""
    if not price_path:
        return 0.0
    span_hours = max(
        (_parse_iso(end_at) - _parse_iso(opened_at)).total_seconds() / 3600.0,
        1.0,
    )
    return len(price_path) / span_hours


# ---------------------------------------------------------------------------
# Stack-count helper — see TODO above re: dedupe
# ---------------------------------------------------------------------------

_SIGNAL_SOURCES = [
    ("gainers_snapshots", "snapshot_at", "gainers"),
    ("losers_snapshots", "snapshot_at", "losers"),
    ("trending_snapshots", "snapshot_at", "trending"),
    ("chain_matches", "completed_at", "chains"),
]


# SF-M2 fix: track signal sources that ARE genuinely missing (table-not-found)
# vs sources that errored on schema drift / connectivity. Sources in
# _signal_sources_missing are logged once at startup; sources NOT in the set
# but still raising OperationalError re-raise to surface real bugs.
_signal_sources_missing: set[str] = set()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """SF-M2: probe table presence so we can narrow OperationalError handling
    in _count_stacked_signals_in_window. Cached via _signal_sources_missing
    on first call per table."""
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        return cur.fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _count_stacked_signals_in_window(
    conn: sqlite3.Connection,
    token_id: str,
    opened_at: str,
    end_at: str,
) -> tuple[int, list[str]]:
    """Count DISTINCT signal-source firings on token_id within the window.
    Each source contributes at most 1 to the stack count. BIO/LAB principle:
    class diversity, not event volume.

    SF-M2 fix (PR #68 silent-failure-hunter): per-table OperationalError
    swallow now distinguishes missing-table (acceptable, logged once) from
    schema-drift / runtime errors (re-raised). _signal_sources_missing
    cached at module level."""
    sources: list[str] = []

    sources_to_check = list(_SIGNAL_SOURCES) + [
        ("predictions", "predicted_at", "narrative"),
        ("velocity_alerts", "detected_at", "velocity"),
        ("volume_spikes", "detected_at", "volume_spike"),
        ("tg_social_signals", "created_at", "tg_social"),
    ]
    for table, ts_col, label in sources_to_check:
        if table == "chain_matches":
            token_col = "token_id"
        elif table == "tg_social_signals":
            token_col = "token_id"
        else:
            token_col = "coin_id"
        if table in _signal_sources_missing:
            continue
        if not _table_exists(conn, table):
            if table not in _signal_sources_missing:
                _signal_sources_missing.add(table)
                print(
                    f"WARN: signal source {table!r} not found in DB; "
                    f"stack count will not include {label!r} contributions.",
                    file=sys.stderr,
                )
            continue
        try:
            cur = conn.execute(
                f"""SELECT 1 FROM {table}
                    WHERE {token_col} = ?
                      AND datetime({ts_col}) >= datetime(?)
                      AND datetime({ts_col}) <= datetime(?)
                    LIMIT 1""",
                (token_id, opened_at, end_at),
            )
            if cur.fetchone() is not None:
                sources.append(label)
        except sqlite3.OperationalError as exc:
            # SF-M2: schema drift / column rename — re-raise so operator
            # sees the real bug instead of getting silent zero.
            raise RuntimeError(
                f"OperationalError on {table}.{ts_col} (column may have "
                f"been renamed; backtest cannot silently continue): {exc}"
            ) from exc

    if "paper_trades" not in _signal_sources_missing and _table_exists(conn, "paper_trades"):
        try:
            cur = conn.execute(
                """SELECT DISTINCT signal_type FROM paper_trades
                   WHERE token_id = ?
                     AND datetime(opened_at) >= datetime(?)
                     AND datetime(opened_at) <= datetime(?)""",
                (token_id, opened_at, end_at),
            )
            for r in cur.fetchall():
                sources.append(f"trade:{r[0]}")
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                f"OperationalError on paper_trades stack scan: {exc}"
            ) from exc

    return len(sources), sources


# ---------------------------------------------------------------------------
# Conviction-lock param composition
# ---------------------------------------------------------------------------

_CONVICTION_LOCK_DELTAS = {
    1: {"max_duration_hours": 0, "trail_pct": 0, "sl_pct": 0,
        "trail_cap": 35, "sl_cap": 25},
    2: {"max_duration_hours": 72, "trail_pct": 5, "sl_pct": 5,
        "trail_cap": 35, "sl_cap": 35},
    3: {"max_duration_hours": 168, "trail_pct": 10, "sl_pct": 10,
        "trail_cap": 35, "sl_cap": 40},
    4: {"max_duration_hours": 336, "trail_pct": 15, "sl_pct": 15,
        "trail_cap": 35, "sl_cap": 40},
}


def conviction_locked_params(stack: int, base: dict) -> dict:
    """Return base params with BL-067 conviction-lock deltas applied.
    Saturates at stack=4."""
    bucket = min(max(stack, 1), 4)
    delta = _CONVICTION_LOCK_DELTAS[bucket]
    return {
        "max_duration_hours": base["max_duration_hours"] + delta["max_duration_hours"],
        "trail_pct": min(base["trail_pct"] + delta["trail_pct"], delta["trail_cap"]),
        "sl_pct": min(base["sl_pct"] + delta["sl_pct"], delta["sl_cap"]),
    }


# ---------------------------------------------------------------------------
# Price-path reconstruction
# ---------------------------------------------------------------------------


def _reconstruct_price_path(
    conn: sqlite3.Connection,
    coin_id: str,
    *,
    start: str,
    end: str,
) -> list[tuple[str, float]]:
    """Reconstruct (timestamp, price) chronologically from snapshot tables.
    UNION over 5 sources, prices > 0, within [start, end].

    SF-M3 fix (PR #68 silent-failure-hunter): same narrowing as SF-M2 —
    table-missing (cached via _signal_sources_missing) is acceptable;
    schema-drift OperationalError re-raises to surface real bugs.
    Sort uses _parse_iso for mixed-format timestamps."""
    rows: list[tuple[str, float]] = []
    # trending_snapshots intentionally NOT included — verified against
    # prod schema: it has no price column (just market_cap_rank +
    # trending_score). Plan/design v3 listed it incorrectly; backtest
    # run on 2026-05-04 surfaced the schema mismatch via the SF-M3
    # narrowing (re-raise on schema drift). Real fix: drop the query.
    queries = [
        ("gainers_snapshots",
         "SELECT snapshot_at, price_at_snapshot FROM gainers_snapshots "
         "WHERE coin_id = ? AND price_at_snapshot > 0 "
         "AND datetime(snapshot_at) >= datetime(?) "
         "AND datetime(snapshot_at) <= datetime(?)"),
        ("losers_snapshots",
         "SELECT snapshot_at, price_at_snapshot FROM losers_snapshots "
         "WHERE coin_id = ? AND price_at_snapshot > 0 "
         "AND datetime(snapshot_at) >= datetime(?) "
         "AND datetime(snapshot_at) <= datetime(?)"),
        ("volume_history_cg",
         "SELECT recorded_at, price FROM volume_history_cg "
         "WHERE coin_id = ? AND price > 0 "
         "AND datetime(recorded_at) >= datetime(?) "
         "AND datetime(recorded_at) <= datetime(?)"),
        ("volume_spikes",
         "SELECT detected_at, price FROM volume_spikes "
         "WHERE coin_id = ? AND price > 0 "
         "AND datetime(detected_at) >= datetime(?) "
         "AND datetime(detected_at) <= datetime(?)"),
    ]
    for table, q in queries:
        if table in _signal_sources_missing:
            continue
        if not _table_exists(conn, table):
            if table not in _signal_sources_missing:
                _signal_sources_missing.add(table)
                print(
                    f"WARN: price-path source {table!r} not found in DB; "
                    f"path may be sparse for some tokens.",
                    file=sys.stderr,
                )
            continue
        try:
            cur = conn.execute(q, (coin_id, start, end))
            for ts, price in cur.fetchall():
                if ts and price and price > 0:
                    rows.append((ts, float(price)))
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                f"OperationalError on price-path source {table!r} "
                f"(column may have been renamed): {exc}"
            ) from exc
    rows.sort(key=lambda r: _parse_iso(r[0]))  # CR-M2: parsed-datetime sort
    return rows


# ---------------------------------------------------------------------------
# Exit simulator (with moonshot per A2 + peak-fade per MF2)
# ---------------------------------------------------------------------------


def _simulate_conviction_locked_exit(
    *,
    entry_price: float,
    opened_at: str,
    params: dict,
    price_path: list[tuple[str, float]],
    moonshot_enabled: bool = True,
    peak_fade_enabled: bool = True,
) -> dict:
    """Replay exit logic against price_path. Includes moonshot trail and
    peak-fade. Skips ladder + production peak-fade-time-window (residual
    asymmetry documented in F2)."""
    if not price_path:
        return {
            "exit_price": entry_price, "exit_reason": "no_data",
            "hold_hours": 0.0, "peak_pct": 0.0, "pnl_pct": 0.0,
            "moonshot_armed": False,
        }
    # SF-S3 fix: 1-2 sample paths can't drive a meaningful exit decision —
    # peak never gets a chance to develop, trail never arms. Distinct from
    # "no_data" so operator can filter / debug separately.
    if len(price_path) <= 2:
        last_ts, last_price = price_path[-1]
        open_dt_chk = _parse_iso(opened_at)
        last_hours = (_parse_iso(last_ts) - open_dt_chk).total_seconds() / 3600.0
        return {
            "exit_price": last_price, "exit_reason": "insufficient_path",
            "hold_hours": last_hours, "peak_pct": 0.0,
            "pnl_pct": (last_price - entry_price) / entry_price * 100.0,
            "moonshot_armed": False,
        }
    open_dt = _parse_iso(opened_at)
    sl_price = entry_price * (1 - params["sl_pct"] / 100.0)
    base_trail_pct = params["trail_pct"]
    max_hours = params["max_duration_hours"]
    peak_price = entry_price
    peak_pct = 0.0
    trail_armed = False
    trail_stop_price = 0.0
    moonshot_armed = False

    for ts, price in price_path:
        cur_dt = _parse_iso(ts)
        hours = (cur_dt - open_dt).total_seconds() / 3600.0
        if hours > max_hours:
            return {
                "exit_price": price, "exit_reason": "expired",
                "hold_hours": hours, "peak_pct": peak_pct,
                "pnl_pct": (price - entry_price) / entry_price * 100.0,
                "moonshot_armed": moonshot_armed,
            }
        if not trail_armed and price <= sl_price:
            return {
                "exit_price": price, "exit_reason": "stop_loss",
                "hold_hours": hours, "peak_pct": peak_pct,
                "pnl_pct": (price - entry_price) / entry_price * 100.0,
                "moonshot_armed": moonshot_armed,
            }
        if price > peak_price:
            peak_price = price
            peak_pct = (peak_price - entry_price) / entry_price * 100.0
            if moonshot_enabled and peak_pct >= _MOONSHOT_THRESHOLD_PCT:
                moonshot_armed = True
            effective_trail_pct = (
                max(base_trail_pct, _MOONSHOT_TRAIL_DRAWDOWN_PCT)
                if moonshot_armed else base_trail_pct
            )
            if peak_pct >= effective_trail_pct:
                trail_armed = True
                trail_stop_price = peak_price * (1 - effective_trail_pct / 100.0)
        if trail_armed and price <= trail_stop_price:
            return {
                "exit_price": price, "exit_reason": "trailing_stop",
                "hold_hours": hours, "peak_pct": peak_pct,
                "pnl_pct": (price - entry_price) / entry_price * 100.0,
                "moonshot_armed": moonshot_armed,
            }
        # MF2 fix: peak-fade
        if peak_fade_enabled and peak_pct >= _PEAK_FADE_THRESHOLD_PCT:
            current_pct = (price - entry_price) / entry_price * 100.0
            if current_pct < peak_pct - _PEAK_FADE_RETRACE_PCT:
                return {
                    "exit_price": price, "exit_reason": "peak_fade",
                    "hold_hours": hours, "peak_pct": peak_pct,
                    "pnl_pct": current_pct,
                    "moonshot_armed": moonshot_armed,
                }
    last_ts, last_price = price_path[-1]
    last_hours = (_parse_iso(last_ts) - open_dt).total_seconds() / 3600.0
    return {
        "exit_price": last_price, "exit_reason": "held_to_window_end",
        "hold_hours": last_hours, "peak_pct": peak_pct,
        "pnl_pct": (last_price - entry_price) / entry_price * 100.0,
        "moonshot_armed": moonshot_armed,
    }


# ---------------------------------------------------------------------------
# Section A — Stack distribution
# ---------------------------------------------------------------------------


def section_a(conn: sqlite3.Connection, *, as_of: str, days: int = 30) -> dict:
    _h(f"SECTION A — Stack distribution (closed paper trades, last {days}d)")
    cur = conn.execute(
        f"""SELECT id, token_id, signal_type, status, opened_at, closed_at,
                   pnl_usd, pnl_pct, peak_pct, exit_reason
            FROM paper_trades
            WHERE status LIKE 'closed_%'
              AND datetime(opened_at) >= datetime(?, '-{days} days')
              AND datetime(opened_at) <= datetime(?)
            ORDER BY opened_at""",
        (as_of, as_of),
    )
    trades = cur.fetchall()
    as_of_dt_a = _parse_iso(as_of)
    by_stack: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for t in trades:
        # CR-SF1 fix: Section A used closed_at, Section B uses opened+504h.
        # Operator confusion if histograms disagree. Align Section A to the
        # same M1 max-bucket window so distributions match.
        end_at_dt = min(
            _parse_iso(t["opened_at"]) + timedelta(hours=MAX_LOCKED_HOURS),
            as_of_dt_a,
        )
        n, _ = _count_stacked_signals_in_window(
            conn, t["token_id"], t["opened_at"], end_at_dt.isoformat(),
        )
        by_stack[n].append(t)

    print(f"Total closed trades: {len(trades)}")
    print()
    print(f"{'Stack':<7} {'n':<5} {'avg_pnl_usd':<14} {'avg_peak%':<12} "
          f"{'win%':<8} {'expired%':<10}")
    print("-" * 60)
    summary: dict[int, dict] = {}
    for stack in sorted(by_stack):
        ts = by_stack[stack]
        n = len(ts)
        if n == 0:
            continue
        avg_pnl = sum(t["pnl_usd"] or 0 for t in ts) / n
        avg_peak = sum(t["peak_pct"] or 0 for t in ts) / n
        wins = sum(1 for t in ts if (t["pnl_usd"] or 0) > 0)
        expired = sum(1 for t in ts if t["status"] == "closed_expired")
        print(f"{stack:<7} {n:<5} ${avg_pnl:>10.2f}    "
              f"{avg_peak:>8.2f}%    {100*wins/n:>5.1f}%   "
              f"{100*expired/n:>5.1f}%")
        summary[stack] = {
            "n": n, "avg_pnl_usd": avg_pnl, "avg_peak_pct": avg_peak,
            "win_pct": 100 * wins / n, "expired_pct": 100 * expired / n,
        }
    return {"section_a": summary, "trade_count": len(trades), "window_days": days}


# ---------------------------------------------------------------------------
# Section B — Conviction-lock simulation (delta-of-deltas)
# ---------------------------------------------------------------------------


def _section_b_for_threshold(
    conn: sqlite3.Connection, *, threshold: int, as_of: str, days: int,
) -> dict:
    _h(f"SECTION B — Conviction-lock simulation (last {days}d, threshold N>={threshold})")
    cur = conn.execute(
        f"""SELECT id, token_id, symbol, signal_type, status, opened_at,
                   closed_at, entry_price, pnl_usd, pnl_pct, peak_pct,
                   exit_reason, tp_pct, sl_pct
            FROM paper_trades
            WHERE status LIKE 'closed_%'
              AND datetime(opened_at) >= datetime(?, '-{days} days')
              AND datetime(opened_at) <= datetime(?)
            ORDER BY opened_at""",
        (as_of, as_of),
    )
    trades = cur.fetchall()
    as_of_dt = _parse_iso(as_of)
    deltas: list[dict] = []
    by_signal: dict[str, dict] = defaultdict(
        lambda: {"n_locked": 0, "actual_pnl": 0.0, "sim_pnl": 0.0,
                 "baseline_pnl": 0.0, "truncated_count": 0}
    )

    for t in trades:
        # M1: stack count over MAX possible window
        stack_window_end_dt = min(
            _parse_iso(t["opened_at"]) + timedelta(hours=MAX_LOCKED_HOURS),
            as_of_dt,
        )
        n, _ = _count_stacked_signals_in_window(
            conn, t["token_id"], t["opened_at"], stack_window_end_dt.isoformat(),
        )
        base_params = _load_signal_params(conn, t["signal_type"])
        locked = conviction_locked_params(stack=n, base=base_params)
        sim_end_dt = min(
            _parse_iso(t["opened_at"]) + timedelta(hours=locked["max_duration_hours"]),
            as_of_dt,
        )
        # S6: truncated when locked.max_duration would extend past as_of
        truncated = (
            _parse_iso(t["opened_at"]) + timedelta(hours=locked["max_duration_hours"])
        ) > as_of_dt
        path = _reconstruct_price_path(
            conn, t["token_id"], start=t["opened_at"], end=sim_end_dt.isoformat(),
        )
        density = _path_density_score(
            path, opened_at=t["opened_at"], end_at=sim_end_dt.isoformat()
        )
        baseline_sim = _simulate_conviction_locked_exit(
            entry_price=float(t["entry_price"]), opened_at=t["opened_at"],
            params=base_params, price_path=path,
        )
        locked_sim = _simulate_conviction_locked_exit(
            entry_price=float(t["entry_price"]), opened_at=t["opened_at"],
            params=locked, price_path=path,
        )
        baseline_pnl = TRADE_SIZE_USD * baseline_sim["pnl_pct"] / 100.0
        sim_pnl = TRADE_SIZE_USD * locked_sim["pnl_pct"] / 100.0
        is_locked = n >= threshold
        actual_pnl = t["pnl_usd"] or 0.0
        deltas.append({
            "id": t["id"], "token_id": t["token_id"],
            "signal_type": t["signal_type"], "stack": n,
            "is_locked": is_locked, "truncated_window": truncated,
            "path_density": density, "actual_pnl": actual_pnl,
            "actual_pnl_pct": t["pnl_pct"], "actual_peak_pct": t["peak_pct"],
            "actual_exit_reason": t["exit_reason"], "baseline_pnl": baseline_pnl,
            "baseline_pnl_pct": baseline_sim["pnl_pct"],
            "baseline_exit_reason": baseline_sim["exit_reason"],
            "sim_pnl": sim_pnl, "sim_pnl_pct": locked_sim["pnl_pct"],
            "sim_peak_pct": locked_sim["peak_pct"],
            "sim_exit_reason": locked_sim["exit_reason"],
            "sim_hold_hours": locked_sim["hold_hours"],
            "moonshot_armed": locked_sim["moonshot_armed"],
        })
        if is_locked:
            by_signal[t["signal_type"]]["n_locked"] += 1
            by_signal[t["signal_type"]]["actual_pnl"] += actual_pnl
            by_signal[t["signal_type"]]["baseline_pnl"] += baseline_pnl
            by_signal[t["signal_type"]]["sim_pnl"] += sim_pnl
            if truncated:
                by_signal[t["signal_type"]]["truncated_count"] += 1

    locked_dense = [d for d in deltas if d["is_locked"] and d["path_density"] >= 0.2]
    actual_total = sum(d["actual_pnl"] for d in locked_dense)
    baseline_total = sum(d["baseline_pnl"] for d in locked_dense)
    sim_total = sum(d["sim_pnl"] for d in locked_dense)
    delta_vs_baseline = sim_total - baseline_total
    delta_vs_actual = sim_total - actual_total
    locked_count = len(locked_dense)
    lift_pct = (
        100 * delta_vs_baseline / abs(baseline_total) if baseline_total else 0.0
    )
    # A1+A3: compound gate
    gate_passed = (
        lift_pct >= 15
        and abs(delta_vs_baseline) >= 100
        and locked_count >= 5
        and delta_vs_actual >= 0
    )
    print(f"Closed trades in window:           {len(trades)}")
    print(f"Locked (stack >= {threshold}, density >=0.2): {locked_count}")
    print(f"  excluded by density <0.2:        "
          f"{sum(1 for d in deltas if d['is_locked'] and d['path_density'] < 0.2)}")
    print(f"  truncated-window subset:         "
          f"{sum(1 for d in locked_dense if d['truncated_window'])}")
    print()
    print(f"Actual aggregate PnL  (locked):    ${actual_total:>10.2f}")
    print(f"Baseline simulated PnL (locked):   ${baseline_total:>10.2f}")
    print(f"Locked simulated PnL (locked):     ${sim_total:>10.2f}")
    print(f"Delta vs baseline (apples-apples): ${delta_vs_baseline:>+10.2f}")
    print(f"Delta vs actual (vs production):   ${delta_vs_actual:>+10.2f}")
    print(f"Lift vs baseline:                  {lift_pct:>+6.1f}%")
    print(f"Decision gate (lift>=15% AND |delta|>=$100 AND locked>=5 "
          f"AND delta_vs_actual>=0): {'PASS' if gate_passed else 'FAIL'}")
    if locked_dense:
        sorted_deltas = sorted(
            locked_dense, key=lambda d: -(d["sim_pnl"] - d["baseline_pnl"])
        )
        print()
        print("Top 10 simulated lifts (vs baseline, locked + dense):")
        for d in sorted_deltas[:10]:
            print(f"  trade #{d['id']:<5} {d['token_id']:<22} "
                  f"stack={d['stack']} actual={d['actual_pnl']:>+8.2f} "
                  f"baseline={d['baseline_pnl']:>+8.2f} "
                  f"locked={d['sim_pnl']:>+8.2f} "
                  f"Δ=${d['sim_pnl'] - d['baseline_pnl']:>+7.2f}")
    print()
    print("Per-signal-type lift (locked subset):")
    print(f"  {'signal_type':<25} {'n':<5} {'actual':>10} "
          f"{'baseline':>10} {'locked':>10} {'lift%':>8} {'trunc%':>8}")
    print("  " + "-" * 85)
    for st, agg in sorted(by_signal.items(), key=lambda kv: -kv[1]["sim_pnl"]):
        b = agg["baseline_pnl"]
        s = agg["sim_pnl"]
        lp = (100 * (s - b) / abs(b)) if b else 0.0
        tr_pct = (
            100 * agg["truncated_count"] / agg["n_locked"]
            if agg["n_locked"] else 0.0
        )
        warn = "  ⚠️ BIASED LOW" if tr_pct > 30 else ""
        print(f"  {st:<25} {agg['n_locked']:<5} ${agg['actual_pnl']:>+8.2f}  "
              f"${b:>+8.2f}  ${s:>+8.2f}  {lp:>+6.1f}%  {tr_pct:>6.1f}%{warn}")

    print()
    print("Hold-time histogram (locked subset, sim hours):")
    buckets = [0, 24, 72, 168, 336, 504]
    bins = [0] * len(buckets)
    for d in locked_dense:
        h = d["sim_hold_hours"]
        for i, edge in enumerate(buckets):
            if h <= edge:
                bins[i] += 1
                break
        else:
            bins[-1] += 1
    print(f"  {'<=24h':<8} {'<=72h':<8} {'<=168h':<8} {'<=336h':<8} {'<=504h':<8}")
    print(f"  {bins[1]:<8} {bins[2]:<8} {bins[3]:<8} {bins[4]:<8} {bins[5]:<8}")

    print()
    print("Exit-reason transition matrix (actual → locked sim, locked subset):")
    matrix: dict[tuple[str, str], int] = defaultdict(int)
    for d in locked_dense:
        a = (d["actual_exit_reason"] or "?").replace("closed_", "")
        s = d["sim_exit_reason"]
        matrix[(a, s)] += 1
    if matrix:
        for (a, s), c in sorted(matrix.items()):
            print(f"  {a:<22} → {s:<22} : {c}")

    return {
        "threshold": threshold, "actual_total": actual_total,
        "baseline_total": baseline_total, "sim_total": sim_total,
        "delta_vs_baseline": delta_vs_baseline,
        "delta_vs_actual": delta_vs_actual,
        "lift_pct": lift_pct, "gate_passed": gate_passed,
        "locked_count": locked_count, "by_signal": dict(by_signal),
        "deltas": deltas,
    }


def section_b(conn: sqlite3.Connection, *, as_of: str, days: int = 30) -> dict:
    return {
        "section_b_n2": _section_b_for_threshold(
            conn, threshold=2, as_of=as_of, days=days
        ),
        "section_b_n3": _section_b_for_threshold(
            conn, threshold=3, as_of=as_of, days=days
        ),
    }


# ---------------------------------------------------------------------------
# Section B2 — First-entry hold simulation
# ---------------------------------------------------------------------------


def section_b2_first_entry_hold(
    conn: sqlite3.Connection, *, as_of: str, days: int = 30
) -> dict:
    """A1: per-token, simulate holding ONLY first paper trade vs sum of
    actual trades across that token. Reproduces operator's LAB +$531
    mental model. MF3: augment with price_cache anchor."""
    _h(f"SECTION B2 — First-entry hold simulation (last {days}d)")
    as_of_dt = _parse_iso(as_of)
    cur = conn.execute(
        f"""SELECT DISTINCT token_id FROM paper_trades
            WHERE status LIKE 'closed_%'
              AND datetime(opened_at) >= datetime(?, '-{days} days')
              AND datetime(opened_at) <= datetime(?)""",
        (as_of, as_of),
    )
    tokens = [r[0] for r in cur.fetchall() if r[0]]
    rows: list[dict] = []
    for token_id in tokens:
        cur2 = conn.execute(
            """SELECT id, signal_type, opened_at, closed_at, entry_price,
                      pnl_usd, peak_pct, exit_reason
               FROM paper_trades
               WHERE token_id = ? AND status LIKE 'closed_%'
                 AND datetime(opened_at) <= datetime(?)
               ORDER BY opened_at""",
            (token_id, as_of),
        )
        ts = cur2.fetchall()
        if not ts:
            continue
        first = ts[0]
        stack_end_dt = min(
            _parse_iso(first["opened_at"]) + timedelta(hours=MAX_LOCKED_HOURS),
            as_of_dt,
        )
        n, _ = _count_stacked_signals_in_window(
            conn, token_id, first["opened_at"], stack_end_dt.isoformat()
        )
        if n < 2:
            continue
        base_params = _load_signal_params(conn, first["signal_type"])
        locked = conviction_locked_params(stack=n, base=base_params)
        sim_end_dt = min(
            _parse_iso(first["opened_at"]) + timedelta(
                hours=locked["max_duration_hours"]
            ),
            as_of_dt,
        )
        path = _reconstruct_price_path(
            conn, token_id,
            start=first["opened_at"], end=sim_end_dt.isoformat(),
        )
        # MF3: append price_cache anchor at as_of.
        # CR-M2 fix: sort by parsed datetime (not lex) — mixed-format
        # timestamps (space vs T separator) sort incorrectly under lex.
        # SF-S8 fix: only append if as_of is at/after path's last sample;
        # otherwise the anchor would slot into the middle and the simulator
        # would fire exits prematurely against the "current" price.
        try:
            cur_pc = conn.execute(
                "SELECT current_price FROM price_cache WHERE coin_id = ?",
                (token_id,),
            )
            pc_row = cur_pc.fetchone()
            if pc_row and pc_row[0] and pc_row[0] > 0:
                path_list = list(path)
                # SF-S8: skip anchor when as_of is before path tail (e.g.,
                # operator passed historical --as-of for replay).
                if not path_list or _parse_iso(as_of) >= _parse_iso(path_list[-1][0]):
                    path_list.append((as_of, float(pc_row[0])))
                path = sorted(path_list, key=lambda r: _parse_iso(r[0]))
        except sqlite3.OperationalError:
            pass
        density = _path_density_score(
            path, opened_at=first["opened_at"], end_at=sim_end_dt.isoformat()
        )
        sim = _simulate_conviction_locked_exit(
            entry_price=float(first["entry_price"]),
            opened_at=first["opened_at"], params=locked, price_path=path,
        )
        first_entry_pnl = TRADE_SIZE_USD * sim["pnl_pct"] / 100.0
        actual_sum = sum(t["pnl_usd"] or 0 for t in ts)
        rows.append({
            "token_id": token_id, "trade_count": len(ts),
            "first_signal_type": first["signal_type"], "stack": n,
            "path_density": density, "actual_sum_pnl": actual_sum,
            "first_entry_hold_pnl": first_entry_pnl,
            "first_entry_pnl_pct": sim["pnl_pct"],
            "first_entry_peak": sim["peak_pct"],
            "first_entry_exit_reason": sim["exit_reason"],
            "first_entry_hold_hours": sim["hold_hours"],
            "delta": first_entry_pnl - actual_sum,
        })
    rows.sort(key=lambda r: -r["delta"])
    print(f"Tokens with N>=2 + locked first-entry hold: {len(rows)}")
    if rows:
        agg_actual = sum(r["actual_sum_pnl"] for r in rows)
        agg_first = sum(r["first_entry_hold_pnl"] for r in rows)
        agg_delta = agg_first - agg_actual
        lift = (100 * agg_delta / abs(agg_actual)) if agg_actual else 0.0
        print(f"Aggregate actual (sum-of-trades):    ${agg_actual:>+10.2f}")
        print(f"Aggregate first-entry hold:          ${agg_first:>+10.2f}")
        print(f"Delta:                                ${agg_delta:>+10.2f}")
        print(f"Lift vs sum-of-trades:                {lift:>+6.1f}%")
        print()
        print("Top 10 first-entry hold lifts (density flag):")
        for r in rows[:10]:
            flag = " ⚠️ low-density" if r["path_density"] < 0.2 else ""
            print(f"  {r['token_id']:<28} stack={r['stack']} "
                  f"trades={r['trade_count']} "
                  f"actual_sum=${r['actual_sum_pnl']:>+8.2f} "
                  f"first_hold=${r['first_entry_hold_pnl']:>+8.2f} "
                  f"Δ=${r['delta']:>+7.2f}{flag}")
    return {"section_b2": rows}


# ---------------------------------------------------------------------------
# Section C — BIO + LAB case studies
# ---------------------------------------------------------------------------


def section_c(conn: sqlite3.Connection, *, as_of: str) -> dict:
    _h("SECTION C — BIO + LAB case studies")
    as_of_dt = _parse_iso(as_of)
    case_studies: dict[str, list[dict]] = {}
    for token_id in ("bio-protocol", "lab"):
        cur = conn.execute(
            """SELECT id, signal_type, status, entry_price, exit_price,
                      pnl_usd, pnl_pct, peak_pct, opened_at, closed_at,
                      exit_reason
               FROM paper_trades
               WHERE token_id = ? AND status LIKE 'closed_%'
                 AND datetime(opened_at) <= datetime(?)
               ORDER BY opened_at""",
            (token_id, as_of),
        )
        trades = cur.fetchall()
        print()
        print(f"-- {token_id} ({len(trades)} closed paper trades) --")
        rows: list[dict] = []
        for t in trades:
            stack_end_dt = min(
                _parse_iso(t["opened_at"]) + timedelta(hours=MAX_LOCKED_HOURS),
                as_of_dt,
            )
            # token_id is the outer loop variable; the SELECT didn't include it
            n, _ = _count_stacked_signals_in_window(
                conn, token_id, t["opened_at"], stack_end_dt.isoformat(),
            )
            # N2 fix: use _load_signal_params not hardcoded
            base_params = _load_signal_params(conn, t["signal_type"])
            locked = conviction_locked_params(stack=max(n, 1), base=base_params)
            end_window_dt = _parse_iso(t["opened_at"]) + timedelta(
                hours=locked["max_duration_hours"]
            )
            # N3 fix: was lex-min on different datetime string formats
            end_window = _min_iso_ts(end_window_dt.isoformat(), as_of)
            path = _reconstruct_price_path(
                conn, token_id, start=t["opened_at"], end=end_window,
            )
            sim = _simulate_conviction_locked_exit(
                entry_price=float(t["entry_price"]),
                opened_at=t["opened_at"], params=locked, price_path=path,
            )
            sim_pnl = TRADE_SIZE_USD * sim["pnl_pct"] / 100.0
            print(f"  #{t['id']:<5} {t['signal_type']:<22} "
                  f"actual {t['pnl_pct']:>+7.2f}% (${t['pnl_usd'] or 0:>+8.2f}) | "
                  f"stack={n} sim {sim['pnl_pct']:>+7.2f}% "
                  f"(${sim_pnl:>+8.2f}) {sim['exit_reason']}")
            rows.append({
                "id": t["id"], "signal_type": t["signal_type"],
                "actual_pnl_usd": t["pnl_usd"], "actual_pnl_pct": t["pnl_pct"],
                "stack": n, "sim_pnl_usd": sim_pnl,
                "sim_pnl_pct": sim["pnl_pct"],
                "sim_exit_reason": sim["exit_reason"],
                "sim_hold_hours": sim["hold_hours"],
            })
        case_studies[token_id] = rows
    return {"section_c": case_studies}


# ---------------------------------------------------------------------------
# Section D — BIO-like cohort survey (TRUE 7d rolling, 1h step per MF1)
# ---------------------------------------------------------------------------


def section_d(
    conn: sqlite3.Connection, *, as_of: str, days: int = 30
) -> dict:
    _h(f"SECTION D — BIO-like cohort survey (TRUE 7d rolling, last {days}d)")
    cur = conn.execute(
        """SELECT DISTINCT coin_id FROM gainers_snapshots
            WHERE datetime(snapshot_at) >= datetime(?, '-' || ? || ' days')
              AND datetime(snapshot_at) <= datetime(?)
            UNION
            SELECT DISTINCT coin_id FROM volume_spikes
            WHERE datetime(detected_at) >= datetime(?, '-' || ? || ' days')
              AND datetime(detected_at) <= datetime(?)
            UNION
            SELECT DISTINCT coin_id FROM losers_snapshots
            WHERE datetime(snapshot_at) >= datetime(?, '-' || ? || ' days')
              AND datetime(snapshot_at) <= datetime(?)
            UNION
            SELECT DISTINCT coin_id FROM trending_snapshots
            WHERE datetime(snapshot_at) >= datetime(?, '-' || ? || ' days')
              AND datetime(snapshot_at) <= datetime(?)""",
        (as_of, days, as_of) * 4,
    )
    candidates = [r[0] for r in cur.fetchall() if r[0]]
    print(f"Distinct tokens seen in any signal source: {len(candidates)}")
    as_of_dt = _parse_iso(as_of)
    cohort_n3: list[tuple[str, int]] = []
    cohort_n5: list[tuple[str, int]] = []
    for token_id in candidates:
        max_stack = 0
        # MF1: 1-hour step (was 24h — would miss BIO-style 6h burst)
        for hour_offset in range(days * 24):
            window_end = as_of_dt - timedelta(hours=hour_offset)
            window_start = window_end - timedelta(days=7)
            n, _ = _count_stacked_signals_in_window(
                conn, token_id,
                window_start.isoformat(), window_end.isoformat(),
            )
            if n > max_stack:
                max_stack = n
            if max_stack >= 5:
                break
        if max_stack >= 3:
            cohort_n3.append((token_id, max_stack))
        if max_stack >= 5:
            cohort_n5.append((token_id, max_stack))
    print(f"Tokens with N>=3 in any 7d window: {len(cohort_n3)}")
    print(f"Tokens with N>=5 in any 7d window: {len(cohort_n5)}")
    if cohort_n3:
        cohort_n3.sort(key=lambda x: -x[1])
        print()
        print("Top 20 most-stacked tokens (max stack seen in any 7d window):")
        for tok, n in cohort_n3[:20]:
            print(f"  {tok:<28} stack={n}")
    return {
        "section_d": {
            "candidates_count": len(candidates),
            "n3_count": len(cohort_n3), "n5_count": len(cohort_n5),
            "top_n3": cohort_n3[:20],
        }
    }


# ---------------------------------------------------------------------------
# Findings markdown generator
# ---------------------------------------------------------------------------


def _resolve_as_of(arg: str | None, conn: sqlite3.Connection) -> tuple[str, bool]:
    """Returns (as_of, was_default). Default is current DB time.

    CR-M1 fix: SQLite's `datetime('now')` returns "YYYY-MM-DD HH:MM:SS"
    (space separator). Concatenating "+00:00" gives a non-ISO-8601 string
    that some downstream callers (lex sort, third-party parsers) handle
    incorrectly. Normalize at source by replacing space with T."""
    if arg:
        _parse_iso(arg)  # validate parseable
        return arg, False
    cur = conn.execute("SELECT datetime('now')")
    raw = cur.fetchone()[0]
    return raw.replace(" ", "T") + "+00:00", True


def _emit_findings_markdown(results: dict, out_path: Path) -> None:
    """N1: auto-emit findings markdown. Operator edits §2 + §5."""
    b_n2 = results.get("section_b_n2", {})
    b_n3 = results.get("section_b_n3", {})
    b2 = results.get("section_b2", [])
    d = results.get("section_d", {})
    a = results.get("section_a", {})
    a_total = sum(b.get("n", 0) for b in a.values()) if isinstance(a, dict) else 0
    a_n2 = (
        sum(b.get("n", 0) for k, b in a.items() if int(k) >= 2)
        if a_total else 0
    )
    a_n3 = (
        sum(b.get("n", 0) for k, b in a.items() if int(k) >= 3)
        if a_total else 0
    )
    a_pct_n2 = (100 * a_n2 / a_total) if a_total else 0
    a_pct_n3 = (100 * a_n3 / a_total) if a_total else 0
    asof_warning = (
        "\n> ⚠️ **AS-OF DEFAULTED** — re-run with explicit `--as-of` for "
        "reproducible findings.\n"
        if results.get("as_of_was_default") else ""
    )
    # SF-S4 fix: preserve operator edits to §2 and §5 across re-runs.
    # If existing file has been edited (placeholder text gone), splice
    # those sections back into the new content.
    preserved_sections: dict[str, str] = {}
    if out_path.exists():
        existing = out_path.read_text(encoding="utf-8")
        for header in ("## §2 — Decision (operator-edited)",
                       "## §5 — Open design questions"):
            idx = existing.find(header)
            if idx == -1:
                continue
            # Find next ## or end-of-file
            next_idx = existing.find("\n## ", idx + 1)
            if next_idx == -1:
                next_idx = existing.find("\n---", idx + 1)
            if next_idx == -1:
                next_idx = len(existing)
            block = existing[idx:next_idx]
            if "[FILL IN" not in block:  # operator has edited
                preserved_sections[header] = block

    md = f"""# BL-067 Backtest Findings

**As-of:** `{results.get("as_of", "?")}`
**Window:** {results.get("days", 30)} days
{asof_warning}
## §1 — Section results

### Section A — Stack distribution

**N8 derived view:** threshold N=3 affects {a_pct_n3:.1f}% of trades vs
N=2's {a_pct_n2:.1f}% — operator can use this to choose which threshold
to act on if both PASS the gate.

```json
{json.dumps(a, indent=2, default=str)}
```

### Section B (threshold N>=2) — Conviction-lock simulation (delta-of-deltas)
- Locked count: {b_n2.get("locked_count", 0)}
- Actual aggregate (locked subset):  ${b_n2.get("actual_total", 0):.2f}
- Baseline simulated:                ${b_n2.get("baseline_total", 0):.2f}
- Locked simulated:                  ${b_n2.get("sim_total", 0):.2f}
- Delta vs baseline (apples-apples): ${b_n2.get("delta_vs_baseline", 0):+.2f}
- Delta vs actual (production):      ${b_n2.get("delta_vs_actual", 0):+.2f}
- Lift %:                            {b_n2.get("lift_pct", 0):+.1f}%
- Gate (lift>=15% AND |delta|>=$100 AND locked>=5 AND delta_vs_actual>=0): **{'PASS' if b_n2.get("gate_passed") else 'FAIL'}**

### Section B (threshold N>=3)
- Locked count: {b_n3.get("locked_count", 0)}
- Lift %: {b_n3.get("lift_pct", 0):+.1f}%
- Gate: **{'PASS' if b_n3.get("gate_passed") else 'FAIL'}**

### Section B2 — First-entry hold simulation (operator's mental model)
- Tokens with N>=2 + first-entry-hold: {len(b2)}
- See JSON for token-by-token detail.

> **Note:** Section B2 assumes infinite slot capacity; real-world bounded
> above by `PAPER_MAX_OPEN_TRADES=10` contention (and `LIVE_MAX_OPEN_POSITIONS=5`
> for live). Treat as upper bound, not achievable estimate. (ASF1)

### Section C — BIO + LAB case studies
See JSON for trade-by-trade replay.

### Section D — BIO-like cohort (TRUE 7d rolling, 1h step)
- Distinct candidates: {d.get("candidates_count", 0)}
- N>=3 cohort: {d.get("n3_count", 0)}
- N>=5 cohort: {d.get("n5_count", 0)}

## §2 — Decision (operator-edited)

[FILL IN: PASS/FAIL on the BL-067 production-implementation gate.
Per design Operational notes §4 decision matrix, route the result based
on B-N2/B-N3/B2 PASS pattern. Recommendation: ____. Reasoning: ____.]

## §3 — BIO + LAB case-study summary

[FILL IN: which trades simulate as kept-open vs exited; how does
chain_completed orphan rate affect the comparison?]

## §4 — Cohort size implications

[FILL IN: 1 token = poor ROI; >10 tokens = strong case. Number says ___.]

## §5 — Open design questions resolved (per backlog.md:382-394)

[FILL IN: lookback window, per-signal opt-in, etc., per the data.]

---

Generated by `scripts/backtest_conviction_lock.py` — do not edit §1.
"""
    # SF-S4: splice preserved operator edits back in
    for header, block in preserved_sections.items():
        # Find the placeholder block in the new md (template starts with header)
        idx = md.find(header)
        if idx == -1:
            continue
        next_idx = md.find("\n## ", idx + 1)
        if next_idx == -1:
            next_idx = md.find("\n---", idx + 1)
        if next_idx == -1:
            next_idx = len(md)
        md = md[:idx] + block + md[next_idx:]
    out_path.write_text(md, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db", type=Path, default=DB_PATH,
        help="Path to scout.db (default: scout.db)",
    )
    parser.add_argument(
        "--as-of", default=None,
        help="ISO-8601 snapshot timestamp; default datetime('now')",
    )
    parser.add_argument(
        "--days", type=int, default=30, choices=range(1, 91),
        metavar="[1-90]",
        help="Lookback window in days (default 30, max 90 — SF-S5 cap)",
    )
    args = parser.parse_args()
    conn = _conn(args.db)
    as_of, was_default = _resolve_as_of(args.as_of, conn)
    print(f"--as-of resolved to: {as_of}")
    if was_default:
        print(
            "WARNING: --as-of defaulted to datetime('now'); findings are "
            "NOT reproducible. Re-run with explicit --as-of for audit trail.",
            file=sys.stderr,
        )
    results: dict = {
        "as_of": as_of, "days": args.days,
        "as_of_was_default": was_default,
    }
    results.update(section_a(conn, as_of=as_of, days=args.days))
    results.update(section_b(conn, as_of=as_of, days=args.days))
    results.update(section_b2_first_entry_hold(conn, as_of=as_of, days=args.days))
    results.update(section_c(conn, as_of=as_of))
    results.update(section_d(conn, as_of=as_of, days=args.days))
    # SF-M1 surface: list signal_types that fell back to defaults so the
    # operator can see if Tier 1a baseline was actually loaded.
    results["signal_params_fallback_signal_types"] = sorted(_signal_params_fallback_seen)
    results["signal_sources_missing"] = sorted(_signal_sources_missing)
    json_path = Path("tasks/findings_bl067_backtest_conviction_lock.json")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    md_path = Path("tasks/findings_bl067_backtest_conviction_lock.md")
    _emit_findings_markdown(results, md_path)
    print()
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path} (operator edits §2 + §5)")
