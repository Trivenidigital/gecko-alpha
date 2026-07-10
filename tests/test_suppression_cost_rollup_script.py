"""Tests for scripts/suppression_cost_rollup.py — the weekly suppression-cost
rollup over the #421 dispatcher-layer gated_out_sample lane.

The health block (sampling coverage + label maturation) is always computed;
the cost block is n-gated on matured (r7d-resolved) rows so no dollar number is
ever emitted below the sample floor. The minimal ``signal_outcome_ledger`` /
``trade_decision_events`` schemas below are copied from the real CREATE
statements (scout/db.py) so the test doubles as a schema-contract lock.

The analysis is read-only + aiohttp-free, so it runs in-process on Windows; the
no-send default is additionally exercised via subprocess.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "suppression_cost_rollup.py"

# Fixed clock so window / lookback bounds and maturation are deterministic.
NOW = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "suppression_cost_rollup", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()

# --- minimal schema, copied verbatim from scout/db.py CREATE statements -------
_CREATE_LEDGER = """
CREATE TABLE signal_outcome_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL
        CHECK(kind IN ('alert','dispatch','gated_out_sample')),
    token_id TEXT NOT NULL,
    surface TEXT NOT NULL,
    price_at_emission REAL,
    anchor_cache_age_seconds REAL,
    liquidity_at_emission REAL,
    liquidity_source TEXT,
    gate_verdicts TEXT,
    enrollment_status TEXT
        CHECK(enrollment_status IN ('not_needed','enrolled','skipped_cap')),
    emitted_at TEXT NOT NULL,
    r15m REAL, r1h REAL, r4h REAL, r24h REAL, r7d REAL, peak7d REAL,
    label_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(label_status IN ('pending','partial','complete','unlabelable')),
    labeled_at TEXT
)
"""
# FOREIGN KEY(paper_trade_id) dropped: paper_trades is out of this test's scope
# and SQLite does not enforce FKs by default.
_CREATE_TDE = """
CREATE TABLE trade_decision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_module TEXT NOT NULL,
    signal_combo TEXT,
    paper_trade_id INTEGER,
    event_data TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


async def _build_db(path):
    conn = await aiosqlite.connect(path)
    await conn.execute(_CREATE_LEDGER)
    await conn.execute(_CREATE_TDE)
    await conn.commit()
    return conn


async def _add_suppressed_row(
    conn,
    *,
    token_id,
    surface="gainers_early",
    days_ago=10,
    r24h=None,
    r7d=None,
    label_status="pending",
    reason="suppressed",
    source_layer="dispatcher",
    price=1.0,
):
    verdicts = json.dumps(
        {
            "reason": reason,
            "source_layer": source_layer,
            "combo_key": surface,
            "suppression_reason": "combo_suppressed",
        },
        sort_keys=True,
    )
    emitted = (NOW - timedelta(days=days_ago)).isoformat()
    await conn.execute(
        """INSERT INTO signal_outcome_ledger
           (kind, token_id, surface, price_at_emission, gate_verdicts,
            enrollment_status, emitted_at, r24h, r7d, label_status)
           VALUES ('gated_out_sample', ?, ?, ?, ?, 'enrolled', ?, ?, ?, ?)""",
        (token_id, surface, price, verdicts, emitted, r24h, r7d, label_status),
    )


async def _add_suppressed_block(conn, *, token_id="blk", days_ago=1):
    created = (NOW - timedelta(days=days_ago)).isoformat()
    await conn.execute(
        """INSERT INTO trade_decision_events
           (token_id, signal_type, decision, reason, source_module,
            event_data, created_at)
           VALUES (?, 'gainers_early', 'blocked', 'suppressed',
                   'scout.trading.signals', '{}', ?)""",
        (token_id, created),
    )


# --------------------------------------------------------------------------
# (a) healthy + matured -> cost block with dollar figure + sorted top movers
# --------------------------------------------------------------------------
async def test_healthy_matured_emits_cost_block_with_top_movers(tmp_path):
    conn = await _build_db(tmp_path / "s.db")
    returns = [2.5, 0.9, -0.3, 1.2, 0.1, -0.5, 3.0, 0.4, -0.1, 0.8, 1.5, -0.2]
    for i, r in enumerate(returns):
        await _add_suppressed_row(
            conn,
            token_id=f"mat-{i}",
            surface="gainers_early" if i % 2 == 0 else "volume_spike",
            days_ago=8 + i,
            r24h=r / 2,
            r7d=r,
            label_status="complete",
        )
    # fresh window rows (still pending; count toward window sampling, not matured)
    for i in range(6):
        await _add_suppressed_row(
            conn, token_id=f"fresh-{i}", days_ago=1 + i, label_status="pending"
        )
    # decoy rows that MUST be excluded from the suppression cohort
    await _add_suppressed_row(
        conn,
        token_id="engine-block",
        days_ago=9,
        r7d=9.9,
        reason="daily_cap",
        source_layer="engine",
        label_status="complete",
    )
    for i in range(30):
        await _add_suppressed_block(conn, token_id=f"b{i}", days_ago=1 + (i % 6))
    await conn.commit()
    await conn.close()

    res = await mod.analyze(
        str(tmp_path / "s.db"),
        window_days=7,
        min_sample=10,
        notional_usd=1000.0,
        now=NOW,
    )

    # cohort excludes the engine decoy -> 12 matured, not 13
    assert res["cost"]["gated"] is False
    assert res["cost"]["n_matured"] == 12
    assert res["cost"]["wins"] == 8  # positive r7d count
    # PnL = sum(r7d) * notional; sum(returns) = 9.3 -> $9,300
    assert round(res["cost"]["est_pnl_usd"], 2) == 9300.00
    top = res["cost"]["top_movers"]
    assert [t["token_id"] for t in top][:2] == ["mat-6", "mat-0"]  # 3.0, 2.5
    assert top[0]["r7d"] == 3.0
    # health: 12 matured r7d all-time; 6 pending window rows sampled
    assert res["health"]["matured_r7d"] == 12
    assert res["health"]["sampling_dead"] is False
    assert res["health"]["suppressed_blocks_in_window"] == 30

    text = mod.format_summary(res)
    assert "est counterfactual PnL" in text
    assert "$9,300.00" in text
    assert "win-rate 66.7% (8/12)" in text
    assert text.count("\n") + 1 <= 6  # <= 6 lines


# --------------------------------------------------------------------------
# (b) immature / small-n -> INSUFFICIENT_DATA, and NO dollar figure emitted
# --------------------------------------------------------------------------
async def test_small_n_is_insufficient_data_no_dollar_figure(tmp_path):
    conn = await _build_db(tmp_path / "s.db")
    for i in range(3):  # < MIN_SAMPLE
        await _add_suppressed_row(
            conn,
            token_id=f"mat-{i}",
            days_ago=9 + i,
            r24h=1.0,
            r7d=2.0,
            label_status="complete",
        )
    for i in range(4):
        await _add_suppressed_block(conn, token_id=f"b{i}", days_ago=1 + i)
    await conn.commit()
    await conn.close()

    res = await mod.analyze(
        str(tmp_path / "s.db"), window_days=7, min_sample=10, now=NOW
    )
    assert res["cost"]["gated"] is True
    assert res["cost"]["n_matured"] == 3
    assert res["cost"]["est_pnl_usd"] is None

    text = mod.format_summary(res)
    assert "INSUFFICIENT_DATA" in text
    assert "n=3 matured" in text
    assert "first meaningful read expected ~2026-07-31" in text
    # Hard gate: absolutely no dollar number leaks below the sample floor.
    assert "$" not in text
    assert "est counterfactual PnL" not in text


# --------------------------------------------------------------------------
# (c) zero sampled rows but suppressed blocks exist -> sampling-dead warning
#     (this doubles as a watchdog on the #421 lane itself)
# --------------------------------------------------------------------------
async def test_zero_sampled_with_blocks_flags_sampling_dead(tmp_path):
    conn = await _build_db(tmp_path / "s.db")
    for i in range(20):
        await _add_suppressed_block(conn, token_id=f"b{i}", days_ago=1 + (i % 6))
    # an engine-level gated_out_sample row exists but is NOT dispatcher-suppression
    await _add_suppressed_row(
        conn, token_id="engine", days_ago=2, reason="daily_cap", source_layer="engine"
    )
    await conn.commit()
    await conn.close()

    res = await mod.analyze(
        str(tmp_path / "s.db"), window_days=7, min_sample=10, now=NOW
    )
    assert res["health"]["sampling_dead"] is True
    assert res["health"]["sampled_in_window"] == 0
    assert res["health"]["suppressed_blocks_in_window"] == 20
    assert res["cost"]["gated"] is True
    assert res["cost"]["n_matured"] == 0

    text = mod.format_summary(res)
    assert "SAMPLING APPEARS DEAD" in text
    assert "421" in text  # names the lane that may be down
    assert "INSUFFICIENT_DATA" in text


# --------------------------------------------------------------------------
# (d) CLI dry-run: no --send default prints the summary and never sends
# --------------------------------------------------------------------------
async def test_cli_no_send_default_prints_summary(tmp_path):
    dbp = tmp_path / "s.db"
    conn = await _build_db(dbp)
    for i in range(12):
        await _add_suppressed_row(
            conn,
            token_id=f"mat-{i}",
            days_ago=8 + i,
            r24h=0.5,
            r7d=1.0,
            label_status="complete",
        )
    for i in range(10):
        await _add_suppressed_block(conn, token_id=f"b{i}", days_ago=1 + (i % 6))
    await conn.commit()
    await conn.close()

    res = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--db", str(dbp), "--window-days", "7"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert "[suppression-cost-rollup]" in res.stdout
    assert "HEALTH" in res.stdout
    assert "COST" in res.stdout
    # no-send default: no dispatch/delivery log line should appear
    assert "alert_dispatched" not in res.stderr
    assert "alert_delivered" not in res.stderr
