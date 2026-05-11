"""BL-NEW-M1.5C: onboarding announcement tests for _maybe_announce_m1_5c.

R1-I3 design fold — explicit fixturing to mirror prod state where the
M1.5b allowlist announcement has already fired before M1.5c migrates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from scout.config import Settings
from scout.db import Database
from scout.main import _maybe_announce_m1_5c

_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "x",
    "ANTHROPIC_API_KEY": "x",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


async def _insert_m1_5b_sentinel(db: Database) -> None:
    """Mirror prod: M1.5b allowlist announcement was already sent."""
    await db._conn.execute(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
        "VALUES (NULL, 'announcement', '_system', ?, 'announcement_sent')",
        (datetime.now(timezone.utc).isoformat(),),
    )
    await db._conn.commit()


@pytest.mark.asyncio
async def test_m1_5c_announcement_fires_when_m1_5b_already_sent(
    tmp_path, monkeypatch
):
    """Prod path: M1.5b sentinel pre-exists; M1.5c must still announce
    via its independent sentinel (separate outcome value)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _insert_m1_5b_sentinel(db)

    sent = AsyncMock()
    monkeypatch.setattr("scout.main.alerter.send_telegram_message", sent)

    await _maybe_announce_m1_5c(db, session=object(), settings=_settings())

    sent.assert_called_once()
    body = sent.call_args[0][0]
    assert "M1.5c" in body
    assert "minara swap" in body

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log "
        "WHERE outcome='m1_5c_announcement_sent'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


@pytest.mark.asyncio
async def test_m1_5c_announcement_skipped_when_disabled(tmp_path, monkeypatch):
    """MINARA_ALERT_ENABLED=False → no fetch, no send, no sentinel."""
    db = Database(tmp_path / "t.db")
    await db.initialize()

    sent = AsyncMock()
    monkeypatch.setattr("scout.main.alerter.send_telegram_message", sent)

    await _maybe_announce_m1_5c(
        db, session=object(), settings=_settings(MINARA_ALERT_ENABLED=False)
    )

    sent.assert_not_called()
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log "
        "WHERE outcome='m1_5c_announcement_sent'"
    )
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_m1_5c_announcement_on_fresh_db_still_works(tmp_path, monkeypatch):
    """Fresh DB (no M1.5b sentinel) — M1.5c announcement still works
    standalone. Guards against ordering coupling between announcements."""
    db = Database(tmp_path / "t.db")
    await db.initialize()

    sent = AsyncMock()
    monkeypatch.setattr("scout.main.alerter.send_telegram_message", sent)

    await _maybe_announce_m1_5c(db, session=object(), settings=_settings())

    sent.assert_called_once()
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log "
        "WHERE outcome='m1_5c_announcement_sent'"
    )
    assert (await cur.fetchone())[0] == 1

    # Idempotency: second call must NOT re-send.
    await _maybe_announce_m1_5c(db, session=object(), settings=_settings())
    assert sent.call_count == 1
    await db.close()


@pytest.mark.asyncio
async def test_m1_5c_migration_preserves_m1_5b_sentinel(tmp_path):
    """R1-C1 critical fold: after the table-rename migration, any
    pre-existing M1.5b 'announcement_sent' row must survive. Otherwise
    operators get re-spammed with the allowlist announcement on the
    next restart."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _insert_m1_5b_sentinel(db)

    # Verify pre-state.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log WHERE outcome='announcement_sent'"
    )
    pre_count = (await cur.fetchone())[0]
    assert pre_count == 1, "fixture insert failed"

    await db.close()

    # Re-open: migration runs again (no-op since already applied), and
    # the M1.5b row must still be there.
    db2 = Database(tmp_path / "t.db")
    await db2.initialize()
    cur = await db2._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log WHERE outcome='announcement_sent'"
    )
    post_count = (await cur.fetchone())[0]
    assert post_count == 1, "M1.5b sentinel lost across migration"
    await db2.close()
