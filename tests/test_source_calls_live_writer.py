"""Tests for scripts/source_calls_live_writer.py + source-calls-live-writer.sh.

Skipped on Windows: bash + the writer module imports aiosqlite which works on
Linux but the wrapper itself is bash-only. Per CLAUDE.md Windows constraint
(`feedback_windows_openssl_workaround.md`), wrapper tests run on Linux only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sqlite3
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash + Linux-only aiosqlite paths",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "scripts" / "source-calls-live-writer.sh"
WRITER_PY = REPO_ROOT / "scripts" / "source_calls_live_writer.py"


def _build_minimal_schema(db_path: Path) -> None:
    """Build the subset of tables the writer reads from / writes to.

    Mirrors the schema invoked by scout.source_quality.ledger.backfill_source_calls
    + refresh_source_call_outcomes. Kept minimal — only columns the writer
    actually touches are included.
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE tg_social_signals (
            id INTEGER PRIMARY KEY,
            message_pk INTEGER,
            source_channel_handle TEXT,
            token_id TEXT,
            symbol TEXT,
            contract_address TEXT,
            chain TEXT,
            resolution_state TEXT,
            mcap_at_sighting REAL,
            paper_trade_id INTEGER,
            created_at TEXT
        );
        CREATE TABLE tg_social_messages (
            id INTEGER PRIMARY KEY,
            posted_at TEXT
        );
        CREATE TABLE narrative_alerts_inbound (
            id INTEGER PRIMARY KEY,
            event_id TEXT,
            tweet_author TEXT,
            resolved_coin_id TEXT,
            extracted_cashtag TEXT,
            extracted_ca TEXT,
            extracted_chain TEXT,
            tweet_ts TEXT,
            received_at TEXT
        );
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            token_id TEXT,
            opened_at TEXT,
            pnl_usd REAL
        );
        CREATE TABLE gainers_snapshots (
            coin_id TEXT,
            price_at_snapshot REAL,
            snapshot_at TEXT
        );
        CREATE TABLE losers_snapshots (
            coin_id TEXT,
            price_at_snapshot REAL,
            snapshot_at TEXT
        );
        CREATE TABLE source_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL CHECK (source_type IN ('tg', 'x')),
            source_id TEXT,
            source_event_id TEXT NOT NULL,
            token_id TEXT,
            symbol TEXT,
            contract_address TEXT,
            chain TEXT,
            call_ts TEXT,
            observed_at TEXT,
            ingest_delay_sec INTEGER,
            call_kind TEXT,
            cluster_identity TEXT,
            cluster_identity_kind TEXT,
            duplicate_cluster_key TEXT,
            duplicate_rank_in_cluster INTEGER DEFAULT 1,
            resolved_state TEXT,
            mcap_at_call REAL,
            price_at_call REAL,
            price_at_call_snapshot_at TEXT,
            price_source TEXT,
            price_age_sec INTEGER,
            forward_30m_pct REAL,
            forward_30m_snapshot_at TEXT,
            forward_30m_observed_horizon_sec INTEGER,
            forward_1h_pct REAL,
            forward_1h_snapshot_at TEXT,
            forward_1h_observed_horizon_sec INTEGER,
            forward_6h_pct REAL,
            forward_6h_snapshot_at TEXT,
            forward_6h_observed_horizon_sec INTEGER,
            forward_24h_pct REAL,
            forward_24h_snapshot_at TEXT,
            forward_24h_observed_horizon_sec INTEGER,
            max_favorable_pct_24h REAL,
            max_adverse_pct_24h REAL,
            time_to_peak_min REAL,
            linked_paper_trade_id INTEGER,
            linkage_method TEXT,
            linkage_confidence TEXT,
            linkage_candidate_count INTEGER DEFAULT 0,
            linkage_conflict_count INTEGER DEFAULT 0,
            outcome_status TEXT,
            missing_fields TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE (source_type, source_event_id)
        );
        """
    )
    conn.commit()
    conn.close()


def test_writer_missing_db_exits_1(tmp_path):
    res = subprocess.run(
        [sys.executable, str(WRITER_PY), "--db", str(tmp_path / "noexist.db")],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["ok"] is False
    assert body["error"] == "db_not_found"


def test_writer_idempotent_on_empty_schema(tmp_path):
    db = tmp_path / "empty.db"
    _build_minimal_schema(db)

    res1 = subprocess.run(
        [sys.executable, str(WRITER_PY), "--db", str(db)],
        capture_output=True,
        text=True,
    )
    assert res1.returncode == 0, (res1.stdout, res1.stderr)
    body1 = json.loads(res1.stdout)
    assert body1["ok"] is True
    assert body1["backfill"]["inserted"] == 0
    assert body1["backfill"]["updated"] == 0
    assert body1["refresh"]["updated"] == 0

    # Second invocation must be idempotent.
    res2 = subprocess.run(
        [sys.executable, str(WRITER_PY), "--db", str(db)],
        capture_output=True,
        text=True,
    )
    assert res2.returncode == 0
    body2 = json.loads(res2.stdout)
    assert body2["backfill"]["inserted"] == 0
    assert body2["backfill"]["updated"] == 0


def test_writer_picks_up_new_upstream_row(tmp_path):
    db = tmp_path / "live.db"
    _build_minimal_schema(db)

    # No rows yet — first writer cycle clean.
    res = subprocess.run(
        [sys.executable, str(WRITER_PY), "--db", str(db)],
        capture_output=True,
        text=True,
    )
    body = json.loads(res.stdout)
    assert body["backfill"]["inserted"] == 0

    # Insert an upstream TG row.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO tg_social_messages (id, posted_at) VALUES (?, ?)",
        (1, "2026-05-20T17:30:00+00:00"),
    )
    conn.execute(
        "INSERT INTO tg_social_signals "
        "(id, message_pk, source_channel_handle, created_at, resolution_state) "
        "VALUES (?, ?, ?, ?, ?)",
        (1, 1, "@test_handle", "2026-05-20T17:30:01+00:00", "unresolved"),
    )
    conn.commit()
    conn.close()

    # Next writer cycle picks it up.
    res2 = subprocess.run(
        [sys.executable, str(WRITER_PY), "--db", str(db)],
        capture_output=True,
        text=True,
    )
    body2 = json.loads(res2.stdout)
    assert res2.returncode == 0
    assert body2["backfill"]["inserted"] == 1
    assert body2["backfill"]["tg_seen"] == 1
    assert body2["refresh"]["updated"] == 1

    # Verify the row landed in source_calls.
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT source_type, source_event_id FROM source_calls"
    ).fetchone()
    conn.close()
    assert row == ("tg", "1")


def test_wrapper_unknown_argument_exits_64(tmp_path):
    res = subprocess.run(
        ["bash", str(WRAPPER), "--bogus-flag"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 64, (res.stdout, res.stderr)
    assert "unknown argument" in res.stderr


def test_wrapper_delegates_to_python_cli(tmp_path):
    db = tmp_path / "wrap.db"
    _build_minimal_schema(db)

    env = os.environ.copy()
    env["GECKO_ENV_FILE"] = str(tmp_path / "missing.env")
    env["GECKO_PYTHON"] = sys.executable

    res = subprocess.run(
        ["bash", str(WRAPPER), "--db", str(db)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["ok"] is True
    assert "backfill" in body and "refresh" in body


# ----- Heartbeat-file tests (BL-NEW-SOURCE-CALL-CRON-TICK-WATCHDOG) -----


def test_writer_touches_heartbeat_on_success(tmp_path):
    """Happy path: writer succeeds → heartbeat mtime advances to now."""
    db = tmp_path / "scout.db"
    _build_minimal_schema(db)
    heartbeat = tmp_path / "writer-heartbeat"

    before = time.time()
    res = subprocess.run(
        [
            sys.executable,
            str(WRITER_PY),
            "--db",
            str(db),
            "--heartbeat-file",
            str(heartbeat),
        ],
        capture_output=True,
        text=True,
    )
    after = time.time()

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert heartbeat.exists()
    mtime = heartbeat.stat().st_mtime
    assert before <= mtime <= after + 1


def test_writer_does_not_touch_heartbeat_on_db_missing(tmp_path):
    """DB not found → exit 1 → heartbeat NOT created."""
    heartbeat = tmp_path / "writer-heartbeat"
    res = subprocess.run(
        [
            sys.executable,
            str(WRITER_PY),
            "--db",
            str(tmp_path / "nonexistent.db"),
            "--heartbeat-file",
            str(heartbeat),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1
    assert not heartbeat.exists()


def test_writer_heartbeat_arg_omitted_is_back_compat(tmp_path):
    """No --heartbeat-file → no touch, no error, exit 0."""
    db = tmp_path / "scout.db"
    _build_minimal_schema(db)
    res = subprocess.run(
        [sys.executable, str(WRITER_PY), "--db", str(db)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["ok"] is True


def test_writer_heartbeat_parent_created_recursively(tmp_path):
    """Parent dir doesn't exist yet → writer creates it via parents=True."""
    db = tmp_path / "scout.db"
    _build_minimal_schema(db)
    heartbeat = tmp_path / "nested" / "subdir" / "writer-heartbeat"
    assert not heartbeat.parent.exists()

    res = subprocess.run(
        [
            sys.executable,
            str(WRITER_PY),
            "--db",
            str(db),
            "--heartbeat-file",
            str(heartbeat),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert heartbeat.exists()


def test_writer_heartbeat_repeated_touches_advance_mtime(tmp_path):
    """Two consecutive writer runs → heartbeat mtime advances each time."""
    db = tmp_path / "scout.db"
    _build_minimal_schema(db)
    heartbeat = tmp_path / "writer-heartbeat"

    # Touch heartbeat to some old time, then run writer
    heartbeat.touch()
    old = time.time() - 3600
    os.utime(heartbeat, (old, old))
    assert heartbeat.stat().st_mtime < time.time() - 100

    res = subprocess.run(
        [
            sys.executable,
            str(WRITER_PY),
            "--db",
            str(db),
            "--heartbeat-file",
            str(heartbeat),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    # mtime advanced (within last 10s)
    assert heartbeat.stat().st_mtime > time.time() - 10


def test_wrapper_passes_heartbeat_arg_through(tmp_path):
    """WRITER_HEARTBEAT_FILE env → wrapper passes --heartbeat-file to Python."""
    db = tmp_path / "scout.db"
    _build_minimal_schema(db)
    heartbeat = tmp_path / "writer-heartbeat"

    env = os.environ.copy()
    env["GECKO_ENV_FILE"] = str(tmp_path / "missing.env")
    env["GECKO_PYTHON"] = sys.executable
    env["WRITER_HEARTBEAT_FILE"] = str(heartbeat)

    res = subprocess.run(
        ["bash", str(WRAPPER), "--db", str(db)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert heartbeat.exists()
