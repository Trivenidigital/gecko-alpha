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
