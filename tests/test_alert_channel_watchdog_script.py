"""Tests for the alert-channel + digest freshness watchdog cron entrypoint.

Mirrors tests/test_source_call_coverage_watchdog_script.py: the disabled path
and the enabled/dry-run evaluation paths are aiohttp-free by design, so they
run on Windows via subprocess. The real Telegram SEND path (aiohttp + alerter)
cannot import aiohttp on Windows, so it is exercised IN-PROCESS with the
module-level send primitive (`_SEND`) monkeypatched — this covers the §12b
dispatched/delivered/failed logging + exit codes (S2-1) and the per-table
cooldown dedup (S2-2) without touching the network. A focused unit test also
stubs aiohttp + the alerter in sys.modules to assert `_send_via_alerter`
passes `raise_on_failure=True`.

The tmp_path DB is created with a MINIMAL schema — tg_alert_log,
paper_daily_summary, narrative_alerts_inbound (NAR-02), and tg_social_health
(NAR-07), with the CREATE statements copied from scout/db.py.
"""

import asyncio
import importlib.util
import json
import sqlite3
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import structlog

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "alert_channel_watchdog.py"


@pytest.fixture(autouse=True)
def _preserve_structlog_config():
    """Belt-and-suspenders: snapshot structlog's global config before each test
    and restore it after. Importing the watchdog module in-process must not
    leak logging config into the rest of the pytest session (the module-level
    `structlog.configure` bug that emptied other tests' captured logs). Fix #1
    makes import side-effect-free; this fixture guarantees it even if a future
    change reintroduces a mutation."""
    saved = structlog.get_config()
    try:
        yield
    finally:
        structlog.configure(**saved)


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

# narrative_alerts_inbound: db.py:4756 (NAR-02). The watchdog only reads
# received_at; the other NOT NULL columns are carried for schema fidelity.
_CREATE_NARRATIVE_INBOUND = """
CREATE TABLE narrative_alerts_inbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    tweet_id TEXT NOT NULL,
    tweet_author TEXT NOT NULL,
    tweet_ts TEXT NOT NULL,
    tweet_text TEXT NOT NULL,
    tweet_text_hash TEXT NOT NULL,
    extracted_cashtag TEXT,
    extracted_ca TEXT,
    extracted_chain TEXT,
    resolved_coin_id TEXT,
    narrative_theme TEXT,
    urgency_signal TEXT,
    classifier_confidence REAL,
    classifier_version TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# tg_social_health: db.py:2077 (NAR-07). Per-channel rows use component
# 'channel:<@handle>' — the format scout/social/telegram/listener.py writes.
_CREATE_TG_SOCIAL_HEALTH = """
CREATE TABLE tg_social_health (
    component        TEXT PRIMARY KEY,
    listener_state   TEXT NOT NULL,
    last_message_at  TEXT,
    updated_at       TEXT NOT NULL,
    detail           TEXT
)
"""

# trade_decision_events: db.py:796 (ALR-08). The watchdog only reads
# decision + created_at; the other NOT NULL columns are carried for schema
# fidelity (FK + nullable columns dropped for the minimal schema). A
# decision='opened' row is a dispatch that SHOULD have produced a tg_alert
# 'sent' row; decision='blocked' rows (universe-filtered / deduped / quarantined
# skips) must NOT count as dispatch activity.
_CREATE_TRADE_DECISION_EVENTS = """
CREATE TABLE trade_decision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_module TEXT NOT NULL,
    event_data TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def _make_db(
    tmp_path,
    *,
    alert_rows=None,
    digest_rows=None,
    narrative_rows=None,
    channel_rows=None,
    create_alert=True,
    create_digest=True,
    create_narrative=True,
    create_dispatch=True,
    dispatch_opens=None,
    dispatch_blocked=0,
):
    """Build a minimal SQLite DB and return its path.

    alert_rows: list of (alerted_at_iso, outcome) tuples.
    digest_rows: list of date-strings ('YYYY-MM-DD').
    narrative_rows: list of received_at ISO strings. ``None`` (the default)
      seeds ONE fresh row so the NAR-02 check stays green for the pre-existing
      tests; ``[]`` creates an empty table (no-rows breach).
    channel_rows: list of (handle, last_message_at_iso) tuples for
      tg_social_health. ``None`` (the default) does NOT create the table — an
      absent tg_social_health is ``ok`` (NAR-07 is a set-scan, not a freshness
      gate), so the pre-existing tests are unaffected.
    create_alert / create_digest / create_narrative: when False, that table is
    not created at all (exercises the missing-table breach path).
    create_dispatch: when True (default) create trade_decision_events (ALR-08
      dispatch-activity qualifier). When False the table is absent -> the
      qualifier counts 0 opens -> a stale/empty alert channel is quiet-legit.
    dispatch_opens: number of fresh ``decision='opened'`` rows to seed. ``None``
      (the default) seeds 10 recent opens so a stale/empty alert channel reads
      as a REAL send-path death (the pre-existing stale-alert tests). ``0`` seeds
      none -> quiet-legitimate.
    dispatch_blocked: number of ``decision='blocked'`` rows to seed. These are
      skips (universe-filtered / deduped / quarantined) and must NOT count as
      dispatch activity — used to prove the all-blocked quiet case stays silent.
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
        if create_narrative:
            conn.execute(_CREATE_NARRATIVE_INBOUND)
            rows = narrative_rows
            if rows is None:  # default: one fresh row (keeps NAR-02 check green)
                rows = [(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()]
            for i, received_at in enumerate(rows):
                conn.execute(
                    "INSERT INTO narrative_alerts_inbound "
                    "(event_id, tweet_id, tweet_author, tweet_ts, tweet_text, "
                    " tweet_text_hash, classifier_version, received_at) "
                    "VALUES (?, 't', 'a', 't', 'x', 'h', 'v1', ?)",
                    (f"evt-{i}", received_at),
                )
        if channel_rows is not None:
            conn.execute(_CREATE_TG_SOCIAL_HEALTH)
            now_iso = datetime.now(timezone.utc).isoformat()
            for handle, last_message_at in channel_rows:
                conn.execute(
                    "INSERT INTO tg_social_health "
                    "(component, listener_state, last_message_at, updated_at) "
                    "VALUES (?, 'running', ?, ?)",
                    (f"channel:{handle}", last_message_at, now_iso),
                )
        if create_dispatch:
            conn.execute(_CREATE_TRADE_DECISION_EVENTS)
            n_opens = 10 if dispatch_opens is None else dispatch_opens
            recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            for i in range(n_opens):
                conn.execute(
                    "INSERT INTO trade_decision_events "
                    "(token_id, signal_type, decision, reason, source_module, "
                    " event_data, created_at) "
                    "VALUES (?, 'volume_spike', 'opened', 'paper_trade_opened', "
                    " 'scout.trading.engine', '{}', ?)",
                    (f"op-{i}", recent),
                )
            for i in range(dispatch_blocked):
                conn.execute(
                    "INSERT INTO trade_decision_events "
                    "(token_id, signal_type, decision, reason, source_module, "
                    " event_data, created_at) "
                    "VALUES (?, 'volume_spike', 'blocked', 'quarantined', "
                    " 'scout.trading.engine', '{}', ?)",
                    (f"bl-{i}", recent),
                )
        # Guarantee at least one table exists so the DB file is non-trivial.
        if (
            not create_alert
            and not create_digest
            and not create_narrative
            and not create_dispatch
        ):
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


def _iso_days(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


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


# --- ALR-08: dispatch-activity qualifier on the alert-sent breach ----------
# A stale/empty alert channel is only paged when the pipeline demonstrably
# opened trades (decision='opened' in trade_decision_events) in the same window.
# Under universe filter + 24h dedup + quarantine everything is BLOCKED (0 opens)
# -> 48 quiet hours are legitimate and must NOT page (protects watchdog
# credibility). A real send-path death has opens but 0 'sent'.


def test_alert_quiet_legitimate_no_dispatch_activity_no_breach(tmp_path):
    # Stale alert channel, but the pipeline opened NOTHING in the window ->
    # quiet-is-legitimate: log-only, no breach, exit 0.
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(100), "sent")],  # > 48h SLO
        digest_rows=[_day(1)],
        dispatch_opens=0,
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["ok"] is True
    assert body["breaches"] == 0
    a = body["checks"]["alert_sent_rate"]
    assert a["status"] == "quiet_ok"
    assert a["reason"] == "stale"
    assert a["dispatch_opens_count"] == 0
    # The distinct quiet-legitimate note is surfaced (not a page).
    assert "quiet_legitimate" in body
    assert "LEGITIMATE" in body["quiet_legitimate"]


def test_alert_quiet_legitimate_when_dispatch_table_absent(tmp_path):
    # No trade_decision_events table at all -> cannot prove activity -> do NOT
    # page (fail safe toward silence, not toward a false page).
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(100), "sent")],
        digest_rows=[_day(1)],
        create_dispatch=False,
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 0
    assert body["checks"]["alert_sent_rate"]["status"] == "quiet_ok"
    assert body["checks"]["alert_sent_rate"]["dispatch_opens_count"] == 0


def test_blocked_rows_do_not_count_as_dispatch_activity(tmp_path):
    # The exact legitimate-quiet scenario ALR-08 targets: the table is FULL of
    # 'blocked' skips (universe-filtered / deduped / quarantined) but 0 opens.
    # A plain row-count would false-positive here; the opens-count must not.
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(100), "sent")],
        digest_rows=[_day(1)],
        dispatch_opens=0,
        dispatch_blocked=50,
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 0
    assert body["checks"]["alert_sent_rate"]["status"] == "quiet_ok"


def test_alert_real_death_with_dispatch_activity_is_breach(tmp_path):
    # Stale alert channel AND the pipeline opened trades in the window -> the
    # send path is likely broken -> REAL breach, page, exit 5.
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(100), "sent")],
        digest_rows=[_day(1)],
        dispatch_opens=5,
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 1
    a = body["checks"]["alert_sent_rate"]
    assert a["status"] == "breach"
    assert a["reason"] == "stale"
    assert a["dispatch_opens_count"] == 5
    msg = body["message"]
    assert "tg_alert_log" in msg
    assert "5" in msg  # dispatch-activity count named
    assert "*" not in msg  # plain text


def test_empty_alert_channel_real_death_when_opens_present(tmp_path):
    # No 'sent' rows EVER but the pipeline opened trades -> real death.
    dbp = _make_db(
        tmp_path,
        alert_rows=[],  # no sent rows
        digest_rows=[_day(1)],
        dispatch_opens=3,
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    a = body["checks"]["alert_sent_rate"]
    assert a["status"] == "breach"
    assert a["reason"] == "no_sent_rows"
    assert a["dispatch_opens_count"] == 3


def test_quiet_legitimate_still_pages_other_breaches(tmp_path):
    # Alert channel quiet-legit (0 opens) BUT the digest is stale -> the digest
    # still breaches and pages; the alert line is NOT in the page.
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(100), "sent")],
        digest_rows=[_day(10)],  # > 2d SLO
        dispatch_opens=0,
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 1
    assert body["checks"]["alert_sent_rate"]["status"] == "quiet_ok"
    assert body["checks"]["digest_write_rate"]["status"] == "breach"
    msg = body["message"]
    assert "paper_daily_summary" in msg
    assert "tg_alert_log" not in msg  # quiet-legit alert excluded from the page


def test_dispatch_activity_threshold_flag(tmp_path):
    # opens=3: with threshold 5 (3 <= 5) -> quiet-legit; with threshold 2
    # (3 > 2) -> real breach.
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(100), "sent")],
        digest_rows=[_day(1)],
        dispatch_opens=3,
    )
    res_quiet = _run(
        dbp, "--enabled", "true", "--dry-run", "--dispatch-activity-threshold", "5"
    )
    assert res_quiet.returncode == 0, res_quiet.stderr
    assert json.loads(res_quiet.stdout)["checks"]["alert_sent_rate"]["status"] == (
        "quiet_ok"
    )

    res_breach = _run(
        dbp, "--enabled", "true", "--dry-run", "--dispatch-activity-threshold", "2"
    )
    assert res_breach.returncode == 5, res_breach.stderr
    assert json.loads(res_breach.stdout)["checks"]["alert_sent_rate"]["status"] == (
        "breach"
    )


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


# --- NAR-02: narrative_alerts_inbound freshness ---------------------------


def test_narrative_inbound_fresh_no_breach(tmp_path):
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(1), "sent")],
        digest_rows=[_day(1)],
        narrative_rows=[_iso(1)],  # < 72h SLO
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 0
    assert body["checks"]["narrative_inbound_rate"]["status"] == "ok"


def test_narrative_inbound_stale_is_breach(tmp_path):
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(1), "sent")],
        digest_rows=[_day(1)],
        narrative_rows=[_iso(100)],  # > 72h SLO
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 1
    assert body["checks"]["narrative_inbound_rate"]["status"] == "breach"
    assert body["checks"]["narrative_inbound_rate"]["reason"] == "stale"
    assert body["checks"]["alert_sent_rate"]["status"] == "ok"
    msg = body["message"]
    assert "narrative_alerts_inbound" in msg
    assert "72h" in msg  # SLO named
    assert "*" not in msg  # plain text, no Markdown bold


def test_narrative_inbound_empty_is_breach(tmp_path):
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(1), "sent")],
        digest_rows=[_day(1)],
        narrative_rows=[],  # table created, no rows
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 1
    assert body["checks"]["narrative_inbound_rate"]["reason"] == "no_inbound_rows"
    assert "NO rows" in body["message"]


def test_narrative_inbound_missing_table_is_breach(tmp_path):
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(1), "sent")],
        digest_rows=[_day(1)],
        create_narrative=False,  # table absent
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 1
    assert body["checks"]["narrative_inbound_rate"]["reason"] == "table_absent"
    msg = body["message"]
    assert "narrative_alerts_inbound" in msg
    assert "missing/absent" in msg


# --- NAR-07: per-channel tg_social staleness ------------------------------


def test_tg_channel_stale_is_breach(tmp_path):
    # One dead channel (30d) + one healthy (2d): only the dead one is flagged.
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(1), "sent")],
        digest_rows=[_day(1)],
        channel_rows=[
            ("@alohcooks", _iso_days(30)),  # > 14d stale
            ("@lowcaphunt", _iso_days(2)),  # fresh
        ],
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 1
    tg = body["checks"]["tg_channel_staleness"]
    assert tg["status"] == "breach"
    assert tg["reason"] == "channels_stale"
    assert [c["handle"] for c in tg["stale_channels"]] == ["@alohcooks"]
    msg = body["message"]
    assert "tg_social_health" in msg
    assert "@alohcooks" in msg
    assert "@lowcaphunt" not in msg  # fresh channel excluded
    assert "14d" in msg  # stale-days threshold named
    assert "*" not in msg  # plain text


def test_tg_channel_all_fresh_no_breach(tmp_path):
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(1), "sent")],
        digest_rows=[_day(1)],
        channel_rows=[("@alohcooks", _iso_days(2)), ("@lowcaphunt", _iso_days(1))],
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 0
    assert body["checks"]["tg_channel_staleness"]["status"] == "ok"


def test_tg_channel_absent_table_is_not_breach(tmp_path):
    # tg_social is default-off: an absent tg_social_health must NOT page.
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(1), "sent")],
        digest_rows=[_day(1)],
        channel_rows=None,  # table not created
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 0
    assert body["checks"]["tg_channel_staleness"]["status"] == "ok"
    assert body["checks"]["tg_channel_staleness"]["reason"] == "table_absent"


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


# ---------------------------------------------------------------------------
# In-process real-send path (S2-1 §12b truthfulness + S2-2 cooldown dedup).
# The send primitive `_SEND` is monkeypatched so aiohttp is never imported.
# ---------------------------------------------------------------------------


def _load_module():
    """Fresh import of the watchdog script as a module (per-test isolation)."""
    spec = importlib.util.spec_from_file_location("_acw_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Recorder:
    """Stand-in for the module `_log`; records every structured log call."""

    def __init__(self):
        self.events = []  # list[(level, event, kwargs)]

    def info(self, event, **kw):
        self.events.append(("info", event, kw))

    def warning(self, event, **kw):
        self.events.append(("warning", event, kw))

    def names(self):
        return [ev for _, ev, _ in self.events]

    def kwargs_for(self, event):
        return [kw for _, ev, kw in self.events if ev == event]


def _fake_send(sink, *, raise_exc=None):
    async def _send(text):
        if raise_exc is not None:
            raise raise_exc
        sink.append(text)

    return _send


def _breach_db(tmp_path):
    """Both checks stale -> two breaches."""
    return _make_db(
        tmp_path,
        alert_rows=[(_iso(100), "sent")],
        digest_rows=[_day(10)],
    )


def _main(mod, dbp, state_dir, *extra):
    return mod.main(
        [
            "--db",
            str(dbp),
            "--enabled",
            "true",
            "--state-dir",
            str(state_dir),
            *extra,
        ]
    )


# --- S2-1: §12b truthfulness ----------------------------------------------


def test_real_send_success_logs_delivered_and_exits_5(tmp_path):
    mod = _load_module()
    rec = _Recorder()
    sink = []
    mod._log = rec
    mod._SEND = _fake_send(sink)

    rc = _main(mod, _breach_db(tmp_path), tmp_path / "state")

    assert rc == 5
    assert len(sink) == 1  # exactly one page dispatched
    names = rec.names()
    assert "alert_channel_watchdog_alert_dispatched" in names
    assert "alert_channel_watchdog_alert_delivered" in names
    assert "alert_channel_watchdog_alert_failed" not in names


def test_real_send_failure_logs_failed_no_delivered_and_exits_1(tmp_path):
    mod = _load_module()
    rec = _Recorder()
    sink = []
    mod._log = rec
    mod._SEND = _fake_send(
        sink, raise_exc=RuntimeError("telegram send failed status=403")
    )

    rc = _main(mod, _breach_db(tmp_path), tmp_path / "state")

    assert rc == 1
    names = rec.names()
    assert "alert_channel_watchdog_alert_dispatched" in names
    assert "alert_channel_watchdog_alert_failed" in names
    # The whole point of S2-1: NEVER report delivered on a rejected page.
    assert "alert_channel_watchdog_alert_delivered" not in names
    # A failed send must NOT persist cooldown state (next run re-alerts).
    assert not (tmp_path / "state" / "last_alert_tg_alert_log").exists()


def test_send_via_alerter_passes_raise_on_failure(monkeypatch):
    """The real send must pass raise_on_failure=True + parse_mode=None, else the
    alerter swallows non-200s and _alert_delivered lies (scout/alerter.py)."""
    mod = _load_module()
    captured = {}

    async def fake_send(
        text,
        session,
        settings,
        *,
        parse_mode="Markdown",
        raise_on_failure=False,
        source="unattributed",
        chat_id=None,
    ):
        captured.update(
            text=text,
            parse_mode=parse_mode,
            raise_on_failure=raise_on_failure,
            source=source,
        )

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    fake_aiohttp = types.ModuleType("aiohttp")
    fake_aiohttp.ClientSession = lambda *a, **k: _FakeSession()
    fake_alerter = types.ModuleType("scout.alerter")
    fake_alerter.send_telegram_message = fake_send
    fake_config = types.ModuleType("scout.config")
    fake_config.Settings = lambda: object()

    monkeypatch.setitem(sys.modules, "aiohttp", fake_aiohttp)
    monkeypatch.setitem(sys.modules, "scout.alerter", fake_alerter)
    monkeypatch.setitem(sys.modules, "scout.config", fake_config)

    asyncio.run(mod._send_via_alerter("body-text"))

    assert captured["raise_on_failure"] is True
    assert captured["parse_mode"] is None
    assert captured["source"] == "alert_channel_watchdog"
    assert captured["text"] == "body-text"


# --- ALR-08: quiet-legitimate never reaches the network send --------------


def test_quiet_legitimate_does_not_page_in_process(tmp_path):
    # Stale alert channel but 0 opens, everything else fresh -> no breach, no
    # send, exit 0, and the distinct quiet-legitimate log fires (not a page).
    dbp = _make_db(
        tmp_path,
        alert_rows=[(_iso(100), "sent")],
        digest_rows=[_day(1)],
        dispatch_opens=0,
    )
    mod = _load_module()
    rec = _Recorder()
    sink = []
    mod._log = rec
    mod._SEND = _fake_send(sink)

    rc = _main(mod, dbp, tmp_path / "state")

    assert rc == 0
    assert len(sink) == 0  # the send path is never reached
    assert "alert_channel_watchdog_alert_channel_quiet_legitimate" in rec.names()
    assert "alert_channel_watchdog_alert_dispatched" not in rec.names()


# --- S2-2: per-table cooldown dedup ---------------------------------------


def test_cooldown_first_breach_dispatches_and_writes_state(tmp_path):
    mod = _load_module()
    rec = _Recorder()
    sink = []
    mod._log = rec
    mod._SEND = _fake_send(sink)
    sd = tmp_path / "state"

    rc = _main(mod, _breach_db(tmp_path), sd)

    assert rc == 5
    assert len(sink) == 1
    assert (sd / "last_alert_tg_alert_log").exists()
    assert (sd / "last_alert_paper_daily_summary").exists()


def test_cooldown_second_run_suppressed_still_exit_5(tmp_path):
    dbp = _breach_db(tmp_path)
    sd = tmp_path / "state"

    # First run dispatches and writes state.
    mod1 = _load_module()
    sink1 = []
    mod1._log = _Recorder()
    mod1._SEND = _fake_send(sink1)
    assert _main(mod1, dbp, sd) == 5
    assert len(sink1) == 1

    # Immediate second run: still a breach, but cooldown suppresses the SEND.
    mod2 = _load_module()
    rec2 = _Recorder()
    sink2 = []
    mod2._log = rec2
    mod2._SEND = _fake_send(sink2)
    rc = _main(mod2, dbp, sd)

    assert rc == 5  # detection is NEVER suppressed
    assert len(sink2) == 0  # but the page is
    assert "alert_channel_watchdog_alert_suppressed_by_cooldown" in rec2.names()
    assert "alert_channel_watchdog_alert_dispatched" not in rec2.names()


def test_cooldown_expired_redispatches(tmp_path):
    dbp = _breach_db(tmp_path)
    sd = tmp_path / "state"
    sd.mkdir(parents=True)
    # Pre-seed both state files with an OLD timestamp (> default 24h cooldown).
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    (sd / "last_alert_tg_alert_log").write_text(old)
    (sd / "last_alert_paper_daily_summary").write_text(old)

    mod = _load_module()
    sink = []
    mod._log = _Recorder()
    mod._SEND = _fake_send(sink)

    assert _main(mod, dbp, sd) == 5
    assert len(sink) == 1  # cooldown expired -> re-dispatched


def test_cooldown_per_table_independence(tmp_path):
    # Only the digest table is inside its cooldown window; tg_alert_log is a
    # NEW breach with no prior state -> the page must cover tg_alert_log alone.
    dbp = _breach_db(tmp_path)
    sd = tmp_path / "state"
    sd.mkdir(parents=True)
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    (sd / "last_alert_paper_daily_summary").write_text(fresh)

    mod = _load_module()
    rec = _Recorder()
    sink = []
    mod._log = rec
    mod._SEND = _fake_send(sink)

    rc = _main(mod, dbp, sd)

    assert rc == 5
    assert len(sink) == 1
    body = sink[0]
    assert "tg_alert_log" in body
    assert "paper_daily_summary" not in body  # in cooldown -> excluded from page
    suppressed = rec.kwargs_for("alert_channel_watchdog_alert_suppressed_by_cooldown")
    assert any(kw.get("table") == "paper_daily_summary" for kw in suppressed)


def test_cooldown_hours_flag_respected(tmp_path):
    # With --cooldown-hours 0 an immediate re-run re-dispatches (window elapsed).
    dbp = _breach_db(tmp_path)
    sd = tmp_path / "state"

    mod1 = _load_module()
    sink1 = []
    mod1._log = _Recorder()
    mod1._SEND = _fake_send(sink1)
    assert _main(mod1, dbp, sd, "--cooldown-hours", "0") == 5
    assert len(sink1) == 1

    mod2 = _load_module()
    sink2 = []
    mod2._log = _Recorder()
    mod2._SEND = _fake_send(sink2)
    assert _main(mod2, dbp, sd, "--cooldown-hours", "0") == 5
    assert len(sink2) == 1  # zero-hour cooldown never suppresses


# --- structlog global-config leak regression (CI-found) --------------------


def test_importing_module_does_not_reconfigure_structlog():
    """Deterministic guard for the module-level `structlog.configure` leak that
    CI surfaced: importing this module (as the 8 in-process tests do) must NOT
    mutate structlog's global config, else every later test in the session that
    captures log events via the default config gets ''. Compares the configured
    logger_factory identity across a fresh import — `configure()` would swap it.

    The real-victim proof is running this test file in ONE pytest invocation
    with tests/test_trading_combo_key.py (green after fix; the other two victim
    files can't collect on Windows due to the aiohttp import crash — CI is the
    final validator there).
    """
    before = structlog.get_config()["logger_factory"]
    sys.modules.pop("_acw_leakcheck", None)
    spec = importlib.util.spec_from_file_location("_acw_leakcheck", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # re-runs module top-level (import side effects)
    after = structlog.get_config()["logger_factory"]
    assert after is before, (
        "importing scripts/alert_channel_watchdog.py reconfigured structlog's "
        "logger_factory at import time — a global side effect that empties other "
        "tests' captured logs (move the configure into __main__)"
    )
