import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

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
        self.requests: list[tuple[str, int, int]] = []

    async def get_input_entity(self, handle):
        self.resolved.append(handle)
        if handle.lower() not in self.chans:
            raise ValueError(f"No user has {handle} as username")
        return handle.lower()

    async def get_messages(self, entity, limit, min_id):
        self.requests.append((entity, limit, min_id))
        if entity in self.fail:
            raise ConnectionError("boom")
        ms = [m for m in self.chans[entity] if m.id > min_id]
        return sorted(ms, key=lambda m: -m.id)[:limit]


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
    assert c.requests[-1] == ("@a", settings.tg_catchup_limit, 3)


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
