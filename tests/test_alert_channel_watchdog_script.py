"""Tests for the alert-channel + digest freshness watchdog cron entrypoint.

Mirrors tests/test_source_call_coverage_watchdog_script.py: the disabled path
and the enabled/dry-run evaluation paths are aiohttp-free by design, so they
run on Windows via subprocess. The real Telegram send (aiohttp + alerter) is
exercised only on CI/VPS; here every breach case uses --dry-run so the
composed message is returned in stdout without touching the network.

The tmp_path DB is created with a MINIMAL schema — only tg_alert_log and
paper_daily_summary, with the CREATE statements copied from scout/db.py.
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "alert_channel_watchdog.py"

# --- CREATE statements copied from scout/db.py -----------------------------
# tg_alert_log: db.py:4214 (FK to paper_trades dropped for the minimal schema;
# SQLite does not enforce it by default and paper_trades is out of scope here).
_CREATE_TG_ALERT_LOG = """
CREATE TABLE tg_alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_trade_id INTEGER,
    signal_type TEXT NOT NULL,
    token_id    TEXT NOT NULL,
    alerted_at  TEXT NOT NULL,
    outcome     TEXT NOT NULL CHECK (outcome IN (
        'sent','blocked_eligibility',
        'blocked_cooldown','dispatch_failed',
        'announcement_sent'
    )),
    detail      TEXT
)
"""

# paper_daily_summary: db.py:1666
_CREATE_PAPER_DAILY_SUMMARY = """
CREATE TABLE paper_daily_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    trades_opened INTEGER NOT NULL DEFAULT 0,
    trades_closed INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    total_pnl_usd REAL NOT NULL DEFAULT 0,
    best_trade_pnl REAL,
    worst_trade_pnl REAL,
    avg_pnl_pct REAL,
    win_rate_pct REAL,
    by_signal_type TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def _make_db(
    tmp_path,
    *,
    alert_rows=None,
    digest_rows=None,
    create_alert=True,
    create_digest=True,
):
    """Build a minimal SQLite DB and return its path.

    alert_rows: list of (alerted_at_iso, outcome) tuples.
    digest_rows: list of date-strings ('YYYY-MM-DD').
    create_alert / create_digest: when False, that table is not created at all
    (exercises the missing-table breach path).
    """
    dbp = tmp_path / "wd.db"
    conn = sqlite3.connect(dbp)
    try:
        if create_alert:
            conn.execute(_CREATE_TG_ALERT_LOG)
            for alerted_at, outcome in alert_rows or []:
                conn.execute(
                    "INSERT INTO tg_alert_log "
                    "(signal_type, token_id, alerted_at, outcome) "
                    "VALUES ('gainers_early', 'tok', ?, ?)",
                    (alerted_at, outcome),
                )
        if create_digest:
            conn.execute(_CREATE_PAPER_DAILY_SUMMARY)
            for d in digest_rows or []:
                conn.execute("INSERT INTO paper_daily_summary (date) VALUES (?)", (d,))
        # Guarantee at least one table exists so the DB file is non-trivial.
        if not create_alert and not create_digest:
            conn.execute("CREATE TABLE _placeholder (x INTEGER)")
        conn.commit()
    finally:
        conn.close()
    return dbp


def _run(dbp, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(dbp), *extra],
        capture_output=True,
        text=True,
    )


def _iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _day(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()


# --- disabled no-op --------------------------------------------------------


def test_watchdog_script_disabled_is_inert(tmp_path):
    # No --enabled and no --dry-run -> inert no-op before even opening the DB.
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(tmp_path / "absent.db")],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["ok"] is True
    assert body["skipped"] == "watchdog_disabled"


def test_missing_db_file_is_error(tmp_path):
    res = _run(tmp_path / "absent.db", "--enabled", "true")
    assert res.returncode == 1, res.stderr
    body = json.loads(res.stdout)
    assert body["ok"] is False
    assert body["error"] == "db_not_found"


# --- both fresh -> no breach ----------------------------------------------


def test_both_fresh_no_breach(tmp_path):
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(1), "sent")],
        digest_rows=[_day(1)],
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["ok"] is True
    assert body["breaches"] == 0
    assert body["checks"]["alert_sent_rate"]["status"] == "ok"
    assert body["checks"]["digest_write_rate"]["status"] == "ok"
    assert "message" not in body


# --- alert-channel breach --------------------------------------------------


def test_alert_sent_stale_is_breach(tmp_path):
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(100), "sent")],  # > 48h SLO
        digest_rows=[_day(1)],
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["ok"] is False
    assert body["breaches"] == 1
    assert body["checks"]["alert_sent_rate"]["status"] == "breach"
    assert body["checks"]["alert_sent_rate"]["reason"] == "stale"
    assert body["checks"]["digest_write_rate"]["status"] == "ok"
    msg = body["message"]
    assert "tg_alert_log" in msg
    assert "48h" in msg  # SLO named
    assert "*" not in msg  # plain text, no Markdown bold


def test_only_sent_outcome_counts(tmp_path):
    # A FRESH non-'sent' row must NOT rescue the check; only 'sent' rows count.
    dbp = _make_db(
        tmp_path,
        alert_rows=[
            (_iso(100), "sent"),  # last real send: stale
            (_iso(1), "blocked_cooldown"),  # fresh but wrong outcome
        ],
        digest_rows=[_day(1)],
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["checks"]["alert_sent_rate"]["status"] == "breach"
    assert body["checks"]["alert_sent_rate"]["reason"] == "stale"


# --- digest breach ---------------------------------------------------------


def test_digest_stale_is_breach(tmp_path):
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(1), "sent")],
        digest_rows=[_day(10)],  # > 2d SLO
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 1
    assert body["checks"]["digest_write_rate"]["status"] == "breach"
    assert body["checks"]["digest_write_rate"]["reason"] == "stale"
    assert body["checks"]["alert_sent_rate"]["status"] == "ok"
    msg = body["message"]
    assert "paper_daily_summary" in msg
    assert "2d" in msg


def test_both_stale_is_double_breach(tmp_path):
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(100), "sent")],
        digest_rows=[_day(10)],
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 2
    msg = body["message"]
    assert "tg_alert_log" in msg
    assert "paper_daily_summary" in msg


# --- empty tables ----------------------------------------------------------


def test_empty_tables_are_breach(tmp_path):
    dbp = _make_db(tmp_path, alert_rows=[], digest_rows=[])
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 2
    assert body["checks"]["alert_sent_rate"]["reason"] == "no_sent_rows"
    assert body["checks"]["digest_write_rate"]["reason"] == "no_summary_rows"
    msg = body["message"]
    assert "NO 'sent' rows" in msg
    assert "NO rows" in msg


# --- missing tables --------------------------------------------------------


def test_missing_tables_are_breach(tmp_path):
    dbp = _make_db(tmp_path, create_alert=False, create_digest=False)
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 2
    assert body["checks"]["alert_sent_rate"]["reason"] == "table_absent"
    assert body["checks"]["digest_write_rate"]["reason"] == "table_absent"
    msg = body["message"]
    assert "tg_alert_log" in msg
    assert "paper_daily_summary" in msg
    assert "missing/absent" in msg


# --- dry-run suppresses the send ------------------------------------------


def test_dry_run_suppresses_send(tmp_path):
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(100), "sent")],
        digest_rows=[_day(10)],
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["dry_run"] is True
    assert body["sent"] is False
    # No aiohttp/network noise should appear on stderr for the dry-run path.
    assert "alert_channel_watchdog_alert_dispatched" not in res.stderr
