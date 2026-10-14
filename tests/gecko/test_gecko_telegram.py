import os
import sqlite3
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from gecko.__main__ import main
from gecko.telegram_reader import initial_marks, poll_once, run_reader, session_problem

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
    written, failed = await poll_once(store, c, settings, {"@a": "@a"}, marks)
    assert (written, failed) == (2, 0) and marks == {"@a": 3}
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
    settings.tg_channels, settings.tg_session_path = ["@a"], str(
        tmp_path / "gk.session"
    )
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
    with pytest.raises(ValueError):
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
