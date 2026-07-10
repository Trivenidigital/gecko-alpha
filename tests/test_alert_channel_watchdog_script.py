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

The tmp_path DB is created with a MINIMAL schema — only tg_alert_log and
paper_daily_summary, with the CREATE statements copied from scout/db.py.
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
