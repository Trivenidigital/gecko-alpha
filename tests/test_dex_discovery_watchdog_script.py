"""PR-C — DEX-discovery poll-liveness watchdog (reviewer semantics matrix).

Liveness pages on the durable successful-poll heartbeat only; discovery age
is diagnostic context. Disablement (lane off, or watchdog gate off) is a
clean exit, never a page. Cooldown gates the SEND only (breach still exits
5); send failure does not record cooldown; dry-run mutates nothing; a held
lock prevents double-send.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scripts.dex_discovery_watchdog import main as watchdog_main


def _mkdb(tmp_path, heartbeat_at=None, discovery_at=None):
    db = tmp_path / "scout.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE ingest_watchdog_state ("
        "source TEXT PRIMARY KEY, consecutive_misses INTEGER NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE dex_pool_discoveries (id INTEGER PRIMARY KEY, "
        "network TEXT, pool_address TEXT, base_token_address TEXT, "
        "first_seen_at TEXT)"
    )
    if heartbeat_at is not None:
        conn.execute(
            "INSERT INTO ingest_watchdog_state VALUES ('dex_discovery', 0, ?)",
            (heartbeat_at.isoformat(),),
        )
    if discovery_at is not None:
        conn.execute(
            "INSERT INTO dex_pool_discoveries "
            "(network, pool_address, base_token_address, first_seen_at) "
            "VALUES ('solana', 'P', 'M', ?)",
            (discovery_at.isoformat(),),
        )
    conn.commit()
    conn.close()
    return str(db)


def _run(
    db,
    tmp_path,
    *,
    enabled="true",
    discovery="true",
    dry_run=False,
    staleness=2.0,
    skew=300.0,
    cooldown=24.0,
    capsys=None,
):
    argv = [
        "--db",
        db,
        "--enabled",
        enabled,
        "--discovery-enabled",
        discovery,
        "--staleness-hours",
        str(staleness),
        "--clock-skew-seconds",
        str(skew),
        "--cooldown-hours",
        str(cooldown),
        "--state-dir",
        str(tmp_path / "state"),
    ]
    if dry_run:
        argv.append("--dry-run")
    return watchdog_main(argv)


def _out(capsys):
    # In-process structlog (only reconfigured under __main__) prints to
    # stdout ahead of the JSON result — parse the first JSON line.
    lines = [l for l in capsys.readouterr().out.strip().splitlines() if l]
    for i, line in enumerate(lines):
        if line.startswith("{"):
            return json.loads(line), lines[i + 1 :]
    raise AssertionError(f"no JSON result line in stdout: {lines!r}")


NOW = datetime.now(timezone.utc)


# ------------------------------------------------------------------ gates


def test_watchdog_gate_off_is_clean_noop(tmp_path, capsys):
    db = _mkdb(tmp_path)  # no heartbeat at all — would breach if armed
    rc = _run(db, tmp_path, enabled="false", dry_run=True)
    assert rc == 0
    payload, _ = _out(capsys)
    assert payload["status"] == "disabled_noop"


def test_discovery_disabled_exits_clean_without_paging(tmp_path, capsys):
    db = _mkdb(tmp_path)  # no heartbeat — but the lane is intentionally OFF
    rc = _run(db, tmp_path, discovery="false", dry_run=True)
    assert rc == 0
    payload, _ = _out(capsys)
    assert payload["status"] == "not_armed_discovery_disabled"


def test_db_missing_exits_1(tmp_path, capsys):
    rc = _run(str(tmp_path / "absent.db"), tmp_path, dry_run=True)
    assert rc == 1


# ------------------------------------------------------------------ liveness


def test_healthy_recent_poll_with_old_discoveries_is_ok(tmp_path, capsys):
    """The reviewer's core case: fresh heartbeat + ancient first_seen_at =
    healthy quiet market; discovery age is context, not a paging signal."""
    db = _mkdb(
        tmp_path,
        heartbeat_at=NOW - timedelta(minutes=10),
        discovery_at=NOW - timedelta(days=9),
    )
    rc = _run(db, tmp_path, dry_run=True)
    assert rc == 0
    payload, _ = _out(capsys)
    assert payload["status"] == "ok"
    assert payload["check"]["discovery_age_hours"] > 200  # logged as context


def test_stale_poll_breaches_despite_recent_discovery_timestamp(tmp_path, capsys):
    """A recent/future-corrupted discovery row must NOT mask a dead poller."""
    db = _mkdb(
        tmp_path,
        heartbeat_at=NOW - timedelta(hours=7),
        discovery_at=NOW + timedelta(hours=1),  # future-corrupted discovery ts
    )
    rc = _run(db, tmp_path, dry_run=True)
    assert rc == 5
    payload, _ = _out(capsys)
    assert payload["check"]["reason"] == "stale"


def test_missing_heartbeat_breaches_when_armed(tmp_path, capsys):
    db = _mkdb(tmp_path)  # empty tables, armed
    rc = _run(db, tmp_path, dry_run=True)
    assert rc == 5
    payload, _ = _out(capsys)
    assert payload["check"]["reason"] == "heartbeat_absent"


def test_empty_database_missing_tables_breaches_when_armed(tmp_path, capsys):
    db = str(tmp_path / "bare.db")
    sqlite3.connect(db).close()  # exists, but no tables at all
    rc = _run(db, tmp_path, dry_run=True)
    assert rc == 5
    payload, _ = _out(capsys)
    assert payload["check"]["reason"] == "heartbeat_absent"


def test_future_heartbeat_beyond_skew_is_invalid_breach(tmp_path, capsys):
    db = _mkdb(tmp_path, heartbeat_at=NOW + timedelta(hours=2))
    rc = _run(db, tmp_path, dry_run=True)
    assert rc == 5
    payload, _ = _out(capsys)
    assert payload["check"]["reason"] == "future_invalid"
    assert payload["check"]["poll_age_seconds_signed"] < 0  # signed age logged
    assert payload["check"]["last_successful_poll_at"]  # raw timestamp logged


def test_future_heartbeat_within_named_skew_is_healthy(tmp_path, capsys):
    db = _mkdb(tmp_path, heartbeat_at=NOW + timedelta(seconds=60))
    rc = _run(db, tmp_path, dry_run=True, skew=300.0)
    assert rc == 0


# ------------------------------------------------------- send/cooldown/lock


def test_dry_run_sends_nothing_and_mutates_no_state(tmp_path, capsys, monkeypatch):
    import scripts.dex_discovery_watchdog as wd

    def _no_send(*a, **k):
        raise AssertionError("dry-run must not send")

    monkeypatch.setattr(wd, "_send_via_alerter", _no_send)
    db = _mkdb(tmp_path)
    rc = _run(db, tmp_path, dry_run=True)
    assert rc == 5
    assert not (tmp_path / "state").exists()  # no cooldown/lock files created


def test_breach_pages_and_records_cooldown(tmp_path, capsys, monkeypatch):
    import scripts.dex_discovery_watchdog as wd

    sent = []

    async def _fake_send(text):
        sent.append(text)

    monkeypatch.setattr(wd, "_send_via_alerter", _fake_send)
    db = _mkdb(tmp_path)
    rc = _run(db, tmp_path)
    assert rc == 5
    assert len(sent) == 1
    assert "diagnostic only" in sent[0]
    assert (tmp_path / "state" / "last_alert_poll_liveness").exists()


def test_cooled_breach_still_exits_5_without_second_send(tmp_path, capsys, monkeypatch):
    import scripts.dex_discovery_watchdog as wd

    sent = []

    async def _fake_send(text):
        sent.append(text)

    monkeypatch.setattr(wd, "_send_via_alerter", _fake_send)
    db = _mkdb(tmp_path)
    assert _run(db, tmp_path) == 5  # pages, records cooldown
    assert _run(db, tmp_path) == 5  # cooled: NO second send, still exit 5
    assert len(sent) == 1


def test_send_failure_exits_1_and_does_not_record_cooldown(
    tmp_path, capsys, monkeypatch
):
    import scripts.dex_discovery_watchdog as wd

    async def _boom(text):
        raise RuntimeError("telegram 502")

    monkeypatch.setattr(wd, "_send_via_alerter", _boom)
    db = _mkdb(tmp_path)
    rc = _run(db, tmp_path)
    assert rc == 1
    # cooldown NOT recorded → next run re-alerts instead of going quiet
    assert not (tmp_path / "state" / "last_alert_poll_liveness").exists()


def test_held_lock_prevents_double_send(tmp_path, capsys, monkeypatch):
    import fcntl as _fcntl

    import scripts.dex_discovery_watchdog as wd

    sent = []

    async def _fake_send(text):
        sent.append(text)

    monkeypatch.setattr(wd, "_send_via_alerter", _fake_send)
    db = _mkdb(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    holder = open(state / "lock", "w")
    _fcntl.flock(holder, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    try:
        rc = _run(db, tmp_path)
    finally:
        _fcntl.flock(holder, _fcntl.LOCK_UN)
        holder.close()
    assert rc == 0  # loser exits clean without sending
    assert sent == []
