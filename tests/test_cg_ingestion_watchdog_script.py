"""Tests for the CoinGecko-ingestion freshness + persistent-outage watchdog.

Mirrors tests/test_alert_channel_watchdog_script.py: the disabled path and the
enabled/dry-run evaluation paths are aiohttp-free by design, so they run on
Windows via subprocess. The real Telegram SEND path (aiohttp + alerter) cannot
import aiohttp on Windows, so it is exercised IN-PROCESS with the module-level
send primitive (`_SEND`) monkeypatched — this covers the §12b
dispatched/delivered/failed logging + exit codes and the per-check cooldown
dedup without touching the network. A focused unit test also stubs aiohttp + the
alerter in sys.modules to assert `_send_via_alerter` passes
`raise_on_failure=True` and `parse_mode=None`.

The tmp_path DB is created with a MINIMAL schema — trending_snapshots,
gainers_snapshots, losers_snapshots — with the CREATE statements copied from
scout/db.py.
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
SCRIPT = REPO_ROOT / "scripts" / "cg_ingestion_watchdog.py"


@pytest.fixture(autouse=True)
def _preserve_structlog_config():
    """Snapshot structlog's global config before each test and restore it after.
    Importing the watchdog module in-process must not leak logging config into
    the rest of the pytest session (the module-level `structlog.configure` bug
    that empties other tests' captured logs). Import is side-effect-free; this
    fixture guarantees it even if a future change reintroduces a mutation."""
    saved = structlog.get_config()
    try:
        yield
    finally:
        structlog.configure(**saved)


# --- CREATE statements copied from scout/db.py -----------------------------
# trending_snapshots: db.py:1581
_CREATE_TRENDING = """
CREATE TABLE trending_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    market_cap_rank INTEGER,
    trending_score REAL,
    snapshot_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# gainers_snapshots: db.py:1676
_CREATE_GAINERS = """
CREATE TABLE gainers_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    price_change_24h REAL NOT NULL,
    market_cap REAL,
    volume_24h REAL,
    price_at_snapshot REAL,
    snapshot_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# losers_snapshots: db.py:1782
_CREATE_LOSERS = """
CREATE TABLE losers_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    price_change_24h REAL NOT NULL,
    market_cap REAL,
    volume_24h REAL,
    price_at_snapshot REAL,
    snapshot_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def _make_db(
    tmp_path,
    *,
    trending_rows=None,
    gainers_rows=None,
    losers_rows=None,
    create_trending=True,
    create_gainers=True,
    create_losers=True,
):
    """Build a minimal SQLite DB and return its path.

    *_rows: list of snapshot_at ISO strings for that table. ``None`` (default)
      seeds ONE fresh row (30 min ago); ``[]`` creates an empty table.
    create_*: when False, that table is not created at all (missing-table path).
    """
    dbp = tmp_path / "wd.db"
    conn = sqlite3.connect(dbp)
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    try:
        if create_trending:
            conn.execute(_CREATE_TRENDING)
            rows = [fresh] if trending_rows is None else trending_rows
            for i, snap in enumerate(rows):
                conn.execute(
                    "INSERT INTO trending_snapshots "
                    "(coin_id, symbol, name, market_cap_rank, trending_score, "
                    " snapshot_at) VALUES (?, 'SYM', 'Name', 100, 1.0, ?)",
                    (f"coin-{i}", snap),
                )
        if create_gainers:
            conn.execute(_CREATE_GAINERS)
            rows = [fresh] if gainers_rows is None else gainers_rows
            for i, snap in enumerate(rows):
                conn.execute(
                    "INSERT INTO gainers_snapshots "
                    "(coin_id, symbol, name, price_change_24h, snapshot_at) "
                    "VALUES (?, 'SYM', 'Name', 50.0, ?)",
                    (f"coin-{i}", snap),
                )
        if create_losers:
            conn.execute(_CREATE_LOSERS)
            rows = [fresh] if losers_rows is None else losers_rows
            for i, snap in enumerate(rows):
                conn.execute(
                    "INSERT INTO losers_snapshots "
                    "(coin_id, symbol, name, price_change_24h, snapshot_at) "
                    "VALUES (?, 'SYM', 'Name', -30.0, ?)",
                    (f"coin-{i}", snap),
                )
        if not create_trending and not create_gainers and not create_losers:
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
    dbp = _make_db(tmp_path)  # all three fresh (30 min ago)
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["ok"] is True
    assert body["breaches"] == 0
    assert body["checks"]["trending_freshness"]["status"] == "ok"
    assert body["checks"]["cg_outage"]["status"] == "ok"
    assert "message" not in body


# --- trending freshness breach --------------------------------------------


def test_trending_stale_is_breach(tmp_path):
    # trending stale (> 3h SLO) but gainers/losers fresh -> only check 1 breaches;
    # check 2 (freshest CG writer) is rescued by the fresh gainers row.
    dbp = _make_db(tmp_path, trending_rows=[_iso(10)])
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 1
    assert body["checks"]["trending_freshness"]["status"] == "breach"
    assert body["checks"]["trending_freshness"]["reason"] == "stale"
    assert body["checks"]["cg_outage"]["status"] == "ok"
    msg = body["message"]
    assert "trending_snapshots" in msg
    assert "3h" in msg  # SLO named
    assert "*" not in msg  # plain text, no Markdown bold


def test_trending_empty_is_breach(tmp_path):
    dbp = _make_db(tmp_path, trending_rows=[])
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["checks"]["trending_freshness"]["status"] == "breach"
    assert body["checks"]["trending_freshness"]["reason"] == "no_snapshot_rows"
    assert "NO rows" in body["message"]


def test_trending_missing_table_is_breach(tmp_path):
    dbp = _make_db(tmp_path, create_trending=False)
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["checks"]["trending_freshness"]["reason"] == "table_absent"
    msg = body["message"]
    assert "trending_snapshots" in msg
    assert "missing/absent" in msg


# --- persistent CG-outage breach ------------------------------------------


def test_cg_outage_all_stale_is_double_breach(tmp_path):
    # The motivating incident: ALL CG writers dark for > outage window.
    dbp = _make_db(
        tmp_path,
        trending_rows=[_iso(50)],
        gainers_rows=[_iso(50)],
        losers_rows=[_iso(50)],
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 2
    o = body["checks"]["cg_outage"]
    assert o["status"] == "breach"
    assert o["reason"] == "stale"
    msg = body["message"]
    assert "coingecko ingestion" in msg
    assert "quota exhaustion" in msg
    assert "last successful CG fetch" in msg
    assert "*" not in msg  # plain text


def test_cg_outage_freshest_writer_rescues(tmp_path):
    # trending + losers stale but gainers FRESH -> the freshest CG writer proves
    # CG is alive; check 2 must NOT fire (trending check 1 still breaches).
    dbp = _make_db(
        tmp_path,
        trending_rows=[_iso(50)],
        gainers_rows=[_iso(1)],  # 1h ago, within 2h window
        losers_rows=[_iso(50)],
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 1
    assert body["checks"]["trending_freshness"]["status"] == "breach"
    assert body["checks"]["cg_outage"]["status"] == "ok"
    assert body["checks"]["cg_outage"]["reason"] == "fresh"


def test_cg_outage_disabled_empty_table_does_not_trip(tmp_path):
    # losers table EXISTS but is empty (feature flag off): it must contribute
    # nothing. With trending + gainers fresh, check 2 stays ok.
    dbp = _make_db(tmp_path, losers_rows=[])
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["breaches"] == 0
    assert body["checks"]["cg_outage"]["status"] == "ok"
    assert "losers_snapshots" in body["checks"]["cg_outage"]["tables_present"]


def test_cg_outage_all_tables_empty_is_breach(tmp_path):
    dbp = _make_db(tmp_path, trending_rows=[], gainers_rows=[], losers_rows=[])
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    o = body["checks"]["cg_outage"]
    assert o["status"] == "breach"
    assert o["reason"] == "no_cg_output_rows"
    assert "all EMPTY" in body["message"]


def test_cg_outage_all_tables_absent_is_breach(tmp_path):
    dbp = _make_db(
        tmp_path,
        create_trending=False,
        create_gainers=False,
        create_losers=False,
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    o = body["checks"]["cg_outage"]
    assert o["status"] == "breach"
    assert o["reason"] == "table_absent"
    assert o["tables_present"] == []
    assert "NONE of the CG snapshot tables" in body["message"]


# --- flag knobs ------------------------------------------------------------


def test_trending_slo_hours_flag(tmp_path):
    # trending 5h old: SLO 8h -> fresh; SLO 2h -> stale.
    dbp = _make_db(tmp_path, trending_rows=[_iso(5)])
    res_ok = _run(dbp, "--enabled", "true", "--dry-run", "--trending-slo-hours", "8")
    assert res_ok.returncode == 0, res_ok.stderr
    assert json.loads(res_ok.stdout)["checks"]["trending_freshness"]["status"] == "ok"

    res_breach = _run(
        dbp, "--enabled", "true", "--dry-run", "--trending-slo-hours", "2"
    )
    assert res_breach.returncode == 5, res_breach.stderr
    assert (
        json.loads(res_breach.stdout)["checks"]["trending_freshness"]["status"]
        == "breach"
    )


def test_cg_outage_hours_flag(tmp_path):
    # All CG writers 5h old: window 8h -> ok; window 1h -> outage breach.
    dbp = _make_db(
        tmp_path,
        trending_rows=[_iso(5)],
        gainers_rows=[_iso(5)],
        losers_rows=[_iso(5)],
    )
    res_ok = _run(dbp, "--enabled", "true", "--dry-run", "--cg-outage-hours", "8")
    body_ok = json.loads(res_ok.stdout)
    assert body_ok["checks"]["cg_outage"]["status"] == "ok"

    res_breach = _run(dbp, "--enabled", "true", "--dry-run", "--cg-outage-hours", "1")
    assert res_breach.returncode == 5, res_breach.stderr
    assert json.loads(res_breach.stdout)["checks"]["cg_outage"]["status"] == "breach"


# --- dry-run suppresses the send ------------------------------------------


def test_dry_run_suppresses_send(tmp_path):
    dbp = _make_db(
        tmp_path,
        trending_rows=[_iso(50)],
        gainers_rows=[_iso(50)],
        losers_rows=[_iso(50)],
    )
    res = _run(dbp, "--enabled", "true", "--dry-run")
    assert res.returncode == 5, res.stderr
    body = json.loads(res.stdout)
    assert body["dry_run"] is True
    assert body["sent"] is False
    assert "cg_ingestion_watchdog_alert_dispatched" not in res.stderr


# ---------------------------------------------------------------------------
# In-process real-send path (§12b truthfulness + cooldown dedup).
# The send primitive `_SEND` is monkeypatched so aiohttp is never imported.
# ---------------------------------------------------------------------------


def _load_module():
    """Fresh import of the watchdog script as a module (per-test isolation)."""
    spec = importlib.util.spec_from_file_location("_cgw_mod", SCRIPT)
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
    """All CG writers stale -> both checks breach."""
    return _make_db(
        tmp_path,
        trending_rows=[_iso(50)],
        gainers_rows=[_iso(50)],
        losers_rows=[_iso(50)],
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


# --- §12b truthfulness -----------------------------------------------------


def test_real_send_success_logs_delivered_and_exits_5(tmp_path):
    mod = _load_module()
    rec = _Recorder()
    sink = []
    mod._log = rec
    mod._SEND = _fake_send(sink)

    rc = _main(mod, _breach_db(tmp_path), tmp_path / "state")

    assert rc == 5
    assert len(sink) == 1  # exactly one page dispatched (both checks in one msg)
    names = rec.names()
    assert "cg_ingestion_watchdog_alert_dispatched" in names
    assert "cg_ingestion_watchdog_alert_delivered" in names
    assert "cg_ingestion_watchdog_alert_failed" not in names


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
    assert "cg_ingestion_watchdog_alert_dispatched" in names
    assert "cg_ingestion_watchdog_alert_failed" in names
    # NEVER report delivered on a rejected page.
    assert "cg_ingestion_watchdog_alert_delivered" not in names
    # A failed send must NOT persist cooldown state (next run re-alerts).
    assert not (tmp_path / "state" / "last_alert_trending_freshness").exists()
    assert not (tmp_path / "state" / "last_alert_cg_outage").exists()


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
    assert captured["source"] == "cg_ingestion_watchdog"
    assert captured["text"] == "body-text"


# --- per-check cooldown dedup ---------------------------------------------


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
    assert (sd / "last_alert_trending_freshness").exists()
    assert (sd / "last_alert_cg_outage").exists()


def test_cooldown_second_run_suppressed_still_exit_5(tmp_path):
    dbp = _breach_db(tmp_path)
    sd = tmp_path / "state"

    mod1 = _load_module()
    sink1 = []
    mod1._log = _Recorder()
    mod1._SEND = _fake_send(sink1)
    assert _main(mod1, dbp, sd) == 5
    assert len(sink1) == 1

    mod2 = _load_module()
    rec2 = _Recorder()
    sink2 = []
    mod2._log = rec2
    mod2._SEND = _fake_send(sink2)
    rc = _main(mod2, dbp, sd)

    assert rc == 5  # detection is NEVER suppressed
    assert len(sink2) == 0  # but the page is
    assert "cg_ingestion_watchdog_alert_suppressed_by_cooldown" in rec2.names()
    assert "cg_ingestion_watchdog_alert_dispatched" not in rec2.names()


def test_cooldown_expired_redispatches(tmp_path):
    dbp = _breach_db(tmp_path)
    sd = tmp_path / "state"
    sd.mkdir(parents=True)
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    (sd / "last_alert_trending_freshness").write_text(old)
    (sd / "last_alert_cg_outage").write_text(old)

    mod = _load_module()
    sink = []
    mod._log = _Recorder()
    mod._SEND = _fake_send(sink)

    assert _main(mod, dbp, sd) == 5
    assert len(sink) == 1  # cooldown expired -> re-dispatched


def test_cooldown_per_check_independence(tmp_path):
    # Only cg_outage is inside its cooldown window; trending is a NEW breach ->
    # the page must cover trending alone.
    dbp = _breach_db(tmp_path)
    sd = tmp_path / "state"
    sd.mkdir(parents=True)
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    (sd / "last_alert_cg_outage").write_text(fresh)

    mod = _load_module()
    rec = _Recorder()
    sink = []
    mod._log = rec
    mod._SEND = _fake_send(sink)

    rc = _main(mod, dbp, sd)

    assert rc == 5
    assert len(sink) == 1
    body = sink[0]
    assert "trending_snapshots" in body
    assert "coingecko ingestion" not in body  # in cooldown -> excluded from page
    suppressed = rec.kwargs_for("cg_ingestion_watchdog_alert_suppressed_by_cooldown")
    assert any(kw.get("check") == "cg_outage" for kw in suppressed)


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


# --- structlog global-config leak regression ------------------------------


def test_importing_module_does_not_reconfigure_structlog():
    """Importing this module (as the in-process tests do) must NOT mutate
    structlog's global config, else every later test in the session that captures
    log events via the default config gets ''. Compares the configured
    logger_factory identity across a fresh import — `configure()` would swap it."""
    before = structlog.get_config()["logger_factory"]
    sys.modules.pop("_cgw_leakcheck", None)
    spec = importlib.util.spec_from_file_location("_cgw_leakcheck", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    after = structlog.get_config()["logger_factory"]
    assert after is before, (
        "importing scripts/cg_ingestion_watchdog.py reconfigured structlog's "
        "logger_factory at import time — a global side effect that empties other "
        "tests' captured logs (move the configure into __main__)"
    )
