"""C6 (routing) — health-channel routing for instrumentation alerts. CI-only.

Imports the alerter (aiohttp) via monkeypatch, so this does not run on the
OpenSSL-blocked dev box. Verifies alerts go to the operator/health channel with
plain text, never the trading channel.
"""

import pytest

from scout.db import Database
from scout.instrumentation import watchdog
from scout.instrumentation.watchdog import check_dex_instrumentation_health

SOL = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


@pytest.fixture(autouse=True)
def _reset_dedup():
    watchdog._reset_dedup()
    yield
    watchdog._reset_dedup()


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "wd.db")
    await d.initialize()
    yield d
    await d.close()


async def _held_open_entry(db):
    # zero/placeholder -> held open -> entry_nonzero_rate == 0 -> fresh-but-empty
    await db.record_entry_mcap(SOL, "solana", "2026-06-17T00:00:00+00:00", 0.0, 0.0, 1.0)


async def test_alert_routes_to_health_channel(db, settings_factory, monkeypatch):
    settings = settings_factory(
        DEX_INSTRUMENTATION_ENABLED=True,
        TELEGRAM_HEALTH_CHAT_ID="health-123",
        DEX_NONZERO_MCAP_FLOOR=0.9,
    )
    await _held_open_entry(db)
    captured: dict = {}

    async def fake_send(text, session, settings, *, parse_mode="Markdown",
                        raise_on_failure=False, source="unattributed", chat_id=None):
        captured.update(text=text, parse_mode=parse_mode, chat_id=chat_id, source=source)

    monkeypatch.setattr("scout.alerter.send_telegram_message", fake_send)
    alarms = await check_dex_instrumentation_health(db, None, settings)

    assert any("entry_mcap" in a for a in alarms)
    assert captured["chat_id"] == "health-123"
    assert captured["parse_mode"] is None
    assert captured["source"] == "dex_instrumentation_watchdog"


async def test_empty_health_chat_falls_back_to_none(db, settings_factory, monkeypatch):
    settings = settings_factory(
        DEX_INSTRUMENTATION_ENABLED=True, DEX_NONZERO_MCAP_FLOOR=0.9
    )
    await _held_open_entry(db)
    captured: dict = {}

    async def fake_send(text, session, settings, *, parse_mode="Markdown",
                        raise_on_failure=False, source="unattributed", chat_id=None):
        captured["chat_id"] = chat_id

    monkeypatch.setattr("scout.alerter.send_telegram_message", fake_send)
    await check_dex_instrumentation_health(db, None, settings)
    # empty health chat -> None -> alerter uses the main chat internally
    assert captured["chat_id"] is None
