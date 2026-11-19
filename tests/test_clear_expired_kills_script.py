"""Tests for scripts/clear_expired_kills.py (LIVE-01 one-time operator clear).

Read-only by default (dry-run): reports a latched expired kill but must NOT
mutate the DB. ``--apply`` clears it (cleared_by='auto_expired'); a fresh
(not-yet-expired) kill is never cleared. DB-only (no network), so it runs
in-process on Windows. The script does its own ``asyncio.run`` inside ``main``,
so these tests are synchronous and drive DB setup/verify via ``asyncio.run``.
"""

import asyncio
import importlib.util
from datetime import timedelta
from pathlib import Path

from scout.db import Database
from scout.live.kill_switch import KillSwitch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "clear_expired_kills.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("clear_expired_kills", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _setup_kill(dbp, *, hours_delta):
    db = Database(dbp)
    await db.initialize()
    try:
        ks = KillSwitch(db)
        kid, _ = await ks.trigger(
            triggered_by="daily_loss_cap",
            reason="x",
            duration=timedelta(hours=hours_delta),
        )
        return kid
    finally:
        await db.close()


async def _read_state(dbp, kid):
    db = Database(dbp)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT cleared_at, cleared_by FROM kill_events WHERE id=?", (kid,)
        )
        row = await cur.fetchone()
        cur = await db._conn.execute(
            "SELECT active_kill_event_id FROM live_control WHERE id=1"
        )
        active = (await cur.fetchone())[0]
        return row, active
    finally:
        await db.close()


def test_dry_run_reports_but_does_not_clear(tmp_path):
    dbp = tmp_path / "t.db"
    kid = asyncio.run(_setup_kill(dbp, hours_delta=-33 * 24))  # latched ~33 days

    mod = _load_script()
    rc = mod.main(["--db", str(dbp)])
    assert rc == 0

    row, active = asyncio.run(_read_state(dbp, kid))
    assert row[0] is None  # cleared_at untouched
    assert active == kid  # still active


def test_apply_clears_expired_kill(tmp_path):
    dbp = tmp_path / "t.db"
    kid = asyncio.run(_setup_kill(dbp, hours_delta=-33 * 24))

    mod = _load_script()
    rc = mod.main(["--db", str(dbp), "--apply"])
    assert rc == 0

    row, active = asyncio.run(_read_state(dbp, kid))
    assert row[0] is not None  # cleared_at stamped
    assert row[1] == "auto_expired"
    assert active is None


def test_apply_leaves_fresh_kill(tmp_path):
    dbp = tmp_path / "t.db"
    kid = asyncio.run(_setup_kill(dbp, hours_delta=4))  # not yet expired

    mod = _load_script()
    rc = mod.main(["--db", str(dbp), "--apply"])
    assert rc == 0

    row, active = asyncio.run(_read_state(dbp, kid))
    assert row[0] is None
    assert active == kid


def test_no_active_kill_is_clean(tmp_path):
    dbp = tmp_path / "t.db"

    async def _init():
        db = Database(dbp)
        await db.initialize()
        await db.close()

    asyncio.run(_init())

    mod = _load_script()
    assert mod.main(["--db", str(dbp)]) == 0
    assert mod.main(["--db", str(dbp), "--apply"]) == 0


def test_missing_db_returns_error(tmp_path):
    mod = _load_script()
    assert mod.main(["--db", str(tmp_path / "nope.db")]) == 1


def test_apply_does_not_clear_a_kill_engaged_after_the_expired_one_was_found(
    tmp_path,
):
    """The pipeline is another process. If it engages a fresh kill between
    this script finding the expired one and clearing it, the fresh kill must
    stay in force."""
    import aiosqlite
    from datetime import datetime, timezone

    mod = _load_script()
    dbp = tmp_path / "t.db"
    old_id = asyncio.run(_setup_kill(dbp, hours_delta=-2))

    async def _race():
        db = Database(dbp)
        await db.initialize()
        try:
            new_id, won = await KillSwitch(db).trigger(
                triggered_by="daily_loss_cap",
                reason="fresh breach",
                duration=timedelta(hours=4),
            )
            assert won is True
        finally:
            await db.close()
        async with aiosqlite.connect(dbp) as conn:
            applied = await mod._apply_clear(conn, old_id, datetime.now(timezone.utc))
            cur = await conn.execute(
                "SELECT active_kill_event_id FROM live_control WHERE id = 1"
            )
            pointer = (await cur.fetchone())[0]
        return new_id, applied, pointer

    new_id, applied, pointer = asyncio.run(_race())
    assert applied is False
    assert pointer == new_id


def test_superseded_and_not_expired_results_name_their_reason(tmp_path, monkeypatch):
    """The result's `reason` says why nothing was done. The kill's own reason
    text must not overwrite it."""
    mod = _load_script()
    dbp = tmp_path / "expired.db"
    asyncio.run(_setup_kill(dbp, hours_delta=-2))

    async def _not_applied(conn, kill_event_id, now):
        return False

    monkeypatch.setattr(mod, "_apply_clear", _not_applied)
    result = asyncio.run(mod._run(str(dbp), apply=True))
    assert result["action"] == "none"
    assert result["reason"] == "kill_superseded"

    fresh = tmp_path / "fresh.db"
    asyncio.run(_setup_kill(fresh, hours_delta=2))
    result = asyncio.run(mod._run(str(fresh), apply=True))
    assert result["action"] == "none"
    assert result["reason"] == "kill_not_expired"


def test_every_result_carries_the_kills_own_reason_under_one_key(tmp_path):
    mod = _load_script()
    dbp = tmp_path / "t.db"
    asyncio.run(_setup_kill(dbp, hours_delta=-2))
    dry = asyncio.run(mod._run(str(dbp), apply=False))
    assert dry["kill_reason"] == "x"
    done = asyncio.run(mod._run(str(dbp), apply=True))
    assert done["action"] == "cleared"
    assert done["kill_reason"] == "x"
