import os
import sqlite3
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from gecko.__main__ import main
from gecko.telegram_reader import (
    NoChannelsResolved,
    initial_marks,
    poll_once,
    run_reader,
    session_problem,
)

SOL = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"


def msg(i, text, day=14):
    return SimpleNamespace(
        id=i,
        date=datetime(2026, 10, day, 11, i % 60, tzinfo=timezone.utc),
        message=text,
    )


class FakeClient:
    def __init__(self, chans: dict[str, list], fail: set[str] = frozenset()):
        self.chans, self.fail = chans, set(fail)
        self.resolved: list[str] = []
        self.requests: list[tuple[str, int, int, bool]] = []

    async def get_input_entity(self, handle):
        self.resolved.append(handle)
        if handle.lower() not in self.chans:
            raise ValueError(f"No user has {handle} as username")
        return handle.lower()

    async def get_messages(self, entity, limit, min_id, reverse=False):
        # Telethon: newest-first by default; reverse=True is oldest-first above min_id
        self.requests.append((entity, limit, min_id, reverse))
        if entity in self.fail:
            raise ConnectionError("boom")
        ms = [m for m in self.chans[entity] if m.id > min_id]
        return sorted(ms, key=lambda m: m.id if reverse else -m.id)[:limit]


async def _runs(store):
    return await (
        await store.conn.execute(
            "SELECT ok, rows_written, error FROM runs WHERE job='telegram-reader' ORDER BY id"
        )
    ).fetchall()


async def test_poll_ingests_calls_and_advances_marks(store, settings):
    c = FakeClient({"@a": [msg(1, f"ape {SOL}"), msg(2, "gm"), msg(3, "$WIF")]})
    marks = {}
    written, failed, wait = await poll_once(store, c, settings, {"@a": "@a"}, marks)
    assert (written, failed, wait) == (2, 0, None) and marks == {"@a": 3}
    await poll_once(store, c, settings, {"@a": "@a"}, marks)
    assert c.requests == [
        ("@a", settings.tg_catchup_limit, 0, False),  # first start: newest only
        ("@a", settings.tg_catchup_limit, 3, True),  # resume: oldest above mark
    ]


async def test_catchup_beyond_limit_loses_no_call(store, settings):
    settings.tg_catchup_limit = 2
    c = FakeClient({"@a": [msg(i, f"${chr(64 + i) * 3}") for i in range(2, 9)]})
    marks = {"@a": 1}  # resumed after downtime: 7 calls above the mark
    with capture_logs() as logs:
        for _ in range(4):
            await poll_once(store, c, settings, {"@a": "@a"}, marks)
    ids = await (
        await store.conn.execute("SELECT event_id FROM source_calls ORDER BY event_id")
    ).fetchall()
    assert [r[0] for r in ids] == [f"tg:@a:{i}" for i in range(2, 9)]
    assert marks == {"@a": 8}
    assert sum(e["event"] == "tg_catchup_truncated" for e in logs) == 3


async def test_tg_login_creates_session_under_umask_077(
    tmp_path, settings, monkeypatch
):
    from pydantic import SecretStr

    import gecko.telegram_reader as tr

    settings.tg_api_id, settings.tg_api_hash = 1, SecretStr("h")
    # H1: login comes before GECKO_TG_CHANNELS is set; it must not require them
    settings.tg_channels, settings.tg_session_path = [], str(tmp_path / "gk.session")
    mask, seen = [0o022], []

    def umask(m):  # a recorder: the Windows CRT keeps only the write bit
        old, mask[0] = mask[0], m
        return old

    class Login:
        async def start(self):
            seen.append(mask[0])
            raise RuntimeError("login aborted")

    monkeypatch.setattr(tr.os, "umask", umask)
    monkeypatch.setattr(tr, "_client", lambda s: Login())
    with pytest.raises(RuntimeError):
        await tr.tg_login_job(settings, None)
    assert seen == [0o077] and mask == [0o022]


async def test_reader_without_channels_still_exits_2(tmp_path, settings, monkeypatch):
    # H1: only tg-login is exempt; the reader refuses before any client starts
    from pydantic import SecretStr

    import gecko.telegram_reader as tr

    sess = tmp_path / "gk.session"
    sess.write_text("x")
    os.chmod(sess, 0o600)
    settings.tg_api_id, settings.tg_api_hash = 1, SecretStr("h")
    settings.tg_session_path, settings.tg_channels = str(sess), []
    monkeypatch.setattr(tr, "_client", lambda s: pytest.fail("client started"))
    assert await tr.telegram_reader_job(settings, SimpleNamespace(as_of=None)) == 2


async def test_failed_channel_marks_run_not_ok(store, settings):
    settings.tg_channels = ["@a", "@b"]
    c = FakeClient({"@a": [msg(1, SOL)], "@b": []}, fail={"@b"})
    await run_reader(store, c, settings, cycles=1)
    ((ok, rows, err),) = await _runs(store)
    assert ok == 0 and rows == 1 and "1 channel" in err


async def test_entities_resolved_once(store, settings):
    settings.tg_channels = ["@A"]
    c = FakeClient({"@a": [msg(1, SOL)]})
    await run_reader(store, c, settings, cycles=3, sleep=_nosleep)
    assert c.resolved == ["@A"]
    assert [r[0] for r in await _runs(store)] == [1, 1, 1]


async def test_unresolvable_channel_fails_fast(store, settings):
    settings.tg_channels = ["@missing"]
    with pytest.raises(NoChannelsResolved):
        await run_reader(store, FakeClient({}), settings, cycles=1)


async def test_restart_resumes_from_stored_calls(store, settings):
    c = FakeClient({"@a": [msg(5, SOL), msg(9, "$ABC")], "@a_b": [msg(4, SOL)]})
    await poll_once(store, c, settings, {"@a": "@a", "@a_b": "@a_b"}, {})
    # "@a_b" must not match "@a" (an "_" is a LIKE wildcard; the query must not use LIKE)
    assert await initial_marks(store, ["@a", "@a_b", "@b"]) == {
        "@a": 9,
        "@a_b": 4,
        "@b": 0,
    }
    assert await initial_marks(store, ["@aXb"]) == {"@aXb": 0}


async def test_poll_resolves_calls_to_known_tokens(store, settings):
    await store.upsert_token(
        f"solana:{SOL}",
        "solana",
        SOL,
        "WIF",
        "wif",
        "2026-10-01T00:00:00Z",
        "dexscreener",
    )
    c = FakeClient({"@a": [msg(1, SOL)]})
    await poll_once(store, c, settings, {"@a": "@a"}, {}, now="2026-10-14T12:00:00Z")
    row = await (
        await store.conn.execute("SELECT token_id FROM source_calls")
    ).fetchone()
    assert row == (f"solana:{SOL}",)


def test_session_problems(tmp_path, settings):
    from pydantic import SecretStr

    settings.tg_api_id, settings.tg_api_hash = 1, SecretStr("h")
    settings.tg_channels = ["@a"]
    ok = tmp_path / "gk.session"
    settings.tg_session_path = str(ok)
    assert "missing" in session_problem(settings, must_exist=True)
    assert session_problem(settings, must_exist=False) is None
    ok.write_text("x")
    os.chmod(ok, 0o600)
    assert session_problem(settings, must_exist=True) is None
    settings.tg_session_path = "rel.session"
    assert "absolute" in session_problem(settings, must_exist=False)
    from pathlib import Path

    settings.tg_session_path = str(Path(__file__).resolve().parents[2] / "x.session")
    assert "inside the repo" in session_problem(settings, must_exist=False)
    settings.tg_session_path = str(tmp_path / "gk.txt")
    assert ".session" in session_problem(settings, must_exist=False)
    settings.tg_session_path = str(ok)
    settings.tg_channels = []
    assert "GECKO_TG_CHANNELS" in session_problem(settings, must_exist=True)
    settings.tg_channels, settings.tg_api_id = ["@a"], 0
    assert "API" in session_problem(settings, must_exist=True)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_session_readable_by_others_refused(tmp_path, settings):
    from pydantic import SecretStr

    settings.tg_api_id, settings.tg_api_hash, settings.tg_channels = (
        1,
        SecretStr("h"),
        ["@a"],
    )
    p = tmp_path / "gk.session"
    p.write_text("x")
    os.chmod(p, 0o644)
    settings.tg_session_path = str(p)
    assert "0600" in session_problem(settings, must_exist=True)


async def _nosleep(_):
    return None


def _legacy_db(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")  # as prod scout.db: ro-opens leave sidecars
    db.execute(
        "CREATE TABLE tg_social_messages(id INTEGER PRIMARY KEY, channel_handle TEXT, msg_id INTEGER,"
        " posted_at TEXT, sender TEXT, text TEXT, cashtags TEXT, contracts TEXT, urls TEXT, parsed_at TEXT)"
    )
    db.execute(
        "CREATE TABLE tg_social_channels(id INTEGER PRIMARY KEY, channel_handle TEXT, display_name TEXT,"
        " trade_eligible INTEGER, safety_required INTEGER, added_at TEXT, removed_at TEXT)"
    )
    db.executemany(
        "INSERT INTO tg_social_messages(channel_handle,msg_id,posted_at,text,parsed_at) VALUES(?,?,?,?,?)",
        [
            ("@Gem", 7, "2026-08-01T00:00:00", f"ape {SOL}", "x"),
            ("@Gem", 8, "2026-08-01T00:01:00", "gm", "x"),
        ],
    )
    db.executemany(
        "INSERT INTO tg_social_channels(channel_handle,display_name,added_at,removed_at) VALUES(?,?,?,?)",
        [("@Gem", "g", "x", None), ("@Old", "o", "x", "2026-07-01")],
    )
    db.commit()
    db.close()


def test_migrate_calls_is_idempotent_and_read_only(tmp_path, monkeypatch, capsys):
    legacy = tmp_path / "scout.db"
    _legacy_db(legacy)
    before = legacy.read_bytes()
    monkeypatch.setenv("GECKO_DB_PATH", str(tmp_path / "gecko.db"))
    assert main(["migrate-calls", "--history-db", str(legacy)]) == 0
    out = capsys.readouterr().out
    assert "migrated 1 call(s)" in out and 'GECKO_TG_CHANNELS=["@gem"]' in out
    assert main(["migrate-calls", "--history-db", str(legacy)]) == 0
    assert "migrated 0 call(s)" in capsys.readouterr().out
    assert legacy.read_bytes() == before
    for ext in ("-wal", "-shm", "-journal"):
        assert not (tmp_path / f"scout.db{ext}").exists()
    db = sqlite3.connect(tmp_path / "gecko.db")
    assert db.execute(
        "SELECT event_id, source_id, posted_at FROM source_calls"
    ).fetchall() == [("tg:@gem:7", "@gem", "2026-08-01T00:00:00Z")]


def test_migrate_calls_missing_db_exits_1(tmp_path, monkeypatch):
    monkeypatch.setenv("GECKO_DB_PATH", str(tmp_path / "gecko.db"))
    assert main(["migrate-calls", "--history-db", str(tmp_path / "nope.db")]) == 1


class EarlyFailClient:
    def __init__(self, authorized=True):
        self.authorized = authorized

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def is_user_authorized(self):
        return self.authorized

    async def get_input_entity(self, handle):
        raise ValueError(f"No user has {handle} as username")


@pytest.mark.parametrize(
    "case,rc,error",
    [
        ("no_session", 2, "session_problem"),
        ("unauthorized", 1, "not_authorized"),
        ("bad_handle", 1, "no_channels_resolved"),
    ],
)
async def test_reader_failing_before_first_poll_records_run_and_pages(
    tmp_path, settings, monkeypatch, case, rc, error
):
    # every exit before the first poll leaves an ok=0 row, so freshness pages
    from datetime import timedelta

    from pydantic import SecretStr

    import gecko.telegram_reader as tr
    from gecko.freshness import check_freshness
    from gecko.store import Store
    from gecko.store import parse_utc

    settings.tg_api_id, settings.tg_api_hash = 1, SecretStr("h")
    settings.tg_channels = ["@gone"]
    sess = tmp_path / "gk.session"
    settings.tg_session_path = str(sess)
    if case != "no_session":
        sess.write_text("x")
        os.chmod(sess, 0o600)
    client = EarlyFailClient(authorized=case != "unauthorized")
    monkeypatch.setattr(tr, "_client", lambda s: client)
    if rc is ValueError:
        with pytest.raises(ValueError):
            await tr.telegram_reader_job(settings, SimpleNamespace(as_of=None))
    else:
        assert await tr.telegram_reader_job(settings, SimpleNamespace(as_of=None)) == rc
    store = await Store.open(settings.db_path)
    try:
        rows = await (
            await store.conn.execute(
                "SELECT ok, error, finished_at FROM runs WHERE job='telegram-reader'"
            )
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [(0, error)]
        later = (parse_utc(rows[0][2]) + timedelta(minutes=31)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        assert "source_calls" in {b.table for b in await check_freshness(store, later)}
    finally:
        await store.close()


async def test_reader_client_construction_failure_records_run(
    tmp_path, settings, monkeypatch
):
    # a broken venv (ImportError) or a corrupt session raises in the constructor
    from pydantic import SecretStr

    import gecko.telegram_reader as tr
    from gecko.store import Store

    settings.tg_api_id, settings.tg_api_hash = 1, SecretStr("h")
    settings.tg_channels = ["@a"]
    sess = tmp_path / "gk.session"
    sess.write_text("x")
    os.chmod(sess, 0o600)
    settings.tg_session_path = str(sess)

    def broken(_settings):
        raise ImportError("No module named 'telethon'")

    monkeypatch.setattr(tr, "_client", broken)
    with pytest.raises(ImportError):
        await tr.telegram_reader_job(settings, SimpleNamespace(as_of=None))
    store = await Store.open(settings.db_path)
    try:
        rows = await (
            await store.conn.execute(
                "SELECT ok, error FROM runs WHERE job='telegram-reader'"
            )
        ).fetchall()
    finally:
        await store.close()
    assert rows == [(0, "ImportError")]


def _job_settings(tmp_path, settings, channels=("@a",), session=True):
    from pydantic import SecretStr

    settings.tg_api_id, settings.tg_api_hash = 1, SecretStr("h")
    settings.tg_channels = list(channels)
    sess = tmp_path / "gk.session"
    settings.tg_session_path = str(sess)
    if session:
        sess.write_text("x")
        os.chmod(sess, 0o600)
    return sess


async def _job_runs(settings):
    from gecko.store import Store

    store = await Store.open(settings.db_path)
    try:
        return await (
            await store.conn.execute(
                "SELECT ok, error FROM runs WHERE job='telegram-reader' ORDER BY id"
            )
        ).fetchall()
    finally:
        await store.close()


async def test_unresolved_handle_is_skipped_and_logged(store, settings):
    settings.tg_channels = ["@a", "@gone"]
    c = FakeClient({"@a": [msg(1, SOL)]})
    with capture_logs() as logs:
        await run_reader(store, c, settings, cycles=2, sleep=_nosleep)
    assert c.resolved == ["@a", "@gone"]  # one by one, each tried once
    assert {r[0] for r in c.requests} == {"@a"}
    assert await _runs(store) == [
        (1, 1, "1 handle(s) unresolved"),
        (1, 0, "1 handle(s) unresolved"),
    ]
    assert [
        (e["channel"], e["error"]) for e in logs if e["event"] == "tg_handle_unresolved"
    ] == [("@gone", "ValueError")]


async def test_all_handles_unresolved_fails_run(tmp_path, settings, monkeypatch):
    import gecko.telegram_reader as tr

    _job_settings(tmp_path, settings, channels=("@gone", "@also_gone"))
    client = EarlyFailClient()
    monkeypatch.setattr(tr, "_client", lambda s: client)
    with capture_logs() as logs:
        rc = await tr.telegram_reader_job(settings, SimpleNamespace(as_of=None))
    assert rc == 1
    assert await _job_runs(settings) == [(0, "no_channels_resolved")]
    assert sum(e["event"] == "tg_handle_unresolved" for e in logs) == 2


class FloodWait(Exception):
    def __init__(self, seconds):
        super().__init__(f"wait {seconds}")
        self.seconds = seconds


async def test_flood_wait_extends_sleep(store, settings):
    settings.tg_channels = ["@a", "@b"]
    waits = [900, 5]  # one per poll of @b; then @b reads fine

    class FloodClient(FakeClient):
        async def get_messages(self, entity, limit, min_id, reverse=False):
            if entity == "@b" and waits:
                raise FloodWait(waits.pop(0))
            return await super().get_messages(entity, limit, min_id, reverse)

    slept = []

    async def sleep(sec):
        slept.append(sec)

    c = FloodClient({"@a": [msg(1, SOL)], "@b": []})
    with capture_logs() as logs:
        await run_reader(store, c, settings, cycles=3, sleep=sleep)
    # longer than the interval: honoured; shorter: the interval still applies
    assert slept == [900, settings.tg_poll_interval_sec]
    assert [e["seconds"] for e in logs if e["event"] == "tg_flood_wait"] == [900, 5]
    assert [r[0] for r in await _runs(store)] == [0, 0, 1]


class _PosixOs:
    """tr.os seen as POSIX; everything else is the real os (files really open)."""

    name = "posix"

    def __getattr__(self, attr):
        return getattr(os, attr)


async def test_reader_refuses_when_lock_held(tmp_path, settings, monkeypatch, capsys):
    import gecko.telegram_reader as tr

    sess = _job_settings(tmp_path, settings)
    ops, held = [], [True]

    def flock(fd, op):
        ops.append(op)
        if held[0]:
            raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setitem(
        sys.modules, "fcntl", SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=flock)
    )
    monkeypatch.setattr(tr, "os", _PosixOs())
    monkeypatch.setattr(tr, "session_problem", lambda s, **kw: None)

    def no_client(_settings):
        raise AssertionError("a second client must not start on a locked session")

    monkeypatch.setattr(tr, "_client", no_client)
    lock_msg = "session lock held: another reader or tg-login is running"
    assert await tr.telegram_reader_job(settings, SimpleNamespace(as_of=None)) == 2
    assert lock_msg in capsys.readouterr().err
    assert await _job_runs(settings) == [(0, "session_locked")]
    sess.unlink()  # tg-login takes the same lock (before refusing on the file)
    assert await tr.tg_login_job(settings, None) == 2
    assert lock_msg in capsys.readouterr().err
    assert ops == [6, 6] and (tmp_path / "gk.session.lock").exists()
    held[0] = False
    fh = tr.lock_session(str(sess))
    try:
        assert ops[-1] == 6 and not fh.closed  # LOCK_EX | LOCK_NB, handle kept open
    finally:
        fh.close()


async def test_tg_login_refuses_existing_session(
    tmp_path, settings, monkeypatch, capsys
):
    import gecko.telegram_reader as tr

    sess = _job_settings(tmp_path, settings)

    def no_client(_settings):
        raise AssertionError("must not adopt an existing session")

    monkeypatch.setattr(tr, "_client", no_client)
    assert await tr.tg_login_job(settings, None) == 2
    err = capsys.readouterr().err
    assert f"session exists at {sess}" in err and "fresh device" in err
    assert sess.read_text() == "x"


class _DisconnectFails(EarlyFailClient):
    def __init__(self, authorized=True, cause=None):
        super().__init__(authorized)
        self.cause = cause

    async def is_user_authorized(self):
        if self.cause:
            raise self.cause
        return self.authorized

    async def disconnect(self):
        raise ConnectionError("socket gone")


async def test_disconnect_error_does_not_mask_cause(tmp_path, settings, monkeypatch):
    import gecko.telegram_reader as tr

    _job_settings(tmp_path, settings)
    client = _DisconnectFails(cause=RuntimeError("the real cause"))
    monkeypatch.setattr(tr, "_client", lambda s: client)
    with capture_logs() as logs:
        with pytest.raises(RuntimeError, match="the real cause"):
            await tr.telegram_reader_job(settings, SimpleNamespace(as_of=None))
    assert await _job_runs(settings) == [(0, "RuntimeError")]
    assert [e["error"] for e in logs if e["event"] == "tg_disconnect_failed"] == [
        "ConnectionError"
    ]


async def test_not_authorized_writes_one_run_row(tmp_path, settings, monkeypatch):
    import gecko.telegram_reader as tr

    _job_settings(tmp_path, settings)
    monkeypatch.setattr(tr, "_client", lambda s: _DisconnectFails(authorized=False))
    assert await tr.telegram_reader_job(settings, SimpleNamespace(as_of=None)) == 1
    assert await _job_runs(settings) == [(0, "not_authorized")]


@pytest.mark.skipif(os.name != "posix", reason="real flock contention")
def test_session_lock_contends_for_real(tmp_path):
    import stat

    import gecko.telegram_reader as tr

    p = str(tmp_path / "gk.session")
    first = tr.lock_session(p)
    try:
        with pytest.raises(tr.SessionLocked):
            tr.lock_session(p)
        assert stat.S_IMODE(os.stat(p + ".lock").st_mode) == 0o600
    finally:
        first.close()
    tr.lock_session(p).close()  # released with the handle


async def test_failed_run_record_is_retried_once(tmp_path, settings, monkeypatch):
    # a fail() whose write raised recorded nothing: the outer except writes the row
    import gecko.telegram_reader as tr
    from gecko.store import Store

    _job_settings(tmp_path, settings)
    monkeypatch.setattr(tr, "_client", lambda s: EarlyFailClient(authorized=False))
    real, calls = Store.record_run, []

    async def flaky(self, *a, **kw):
        calls.append(kw.get("error"))
        if len(calls) == 1:
            raise OSError("disk hiccup")
        return await real(self, *a, **kw)

    monkeypatch.setattr(Store, "record_run", flaky)
    with pytest.raises(OSError):
        await tr.telegram_reader_job(settings, SimpleNamespace(as_of=None))
    monkeypatch.setattr(Store, "record_run", real)
    assert calls == ["not_authorized", "OSError"]
    assert await _job_runs(settings) == [(0, "OSError")]
