"""BL-NEW-ALERT-UNIVERSE-FILTER: operator-facing universe filter tests.

Blocks paper-trade-open Telegram alerts for out-of-universe CoinGecko ids
(tokenized equities / ETFs, e.g. `*-bstocks-tokenized-stock`) when
ALERT_UNIVERSE_FILTER_ENABLED. Flag defaults OFF; the paper ENGINE is
unaffected (still opens trades) — this only stops the operator-facing send.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.tg_alert_dispatch import (
    _check_universe,
    _log_outcome,
    notify_paper_trade_opened,
)

_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "x",
    "ANTHROPIC_API_KEY": "x",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


async def _insert_paper_trade(db: Database, *, trade_id: int = 42) -> None:
    """Minimal paper_trades row for FK satisfaction."""
    if db._conn is None:
        raise RuntimeError("db not initialized")
    await db._conn.execute(
        """INSERT INTO paper_trades
           (id, token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price,
            sl_price, status, opened_at)
           VALUES (?, ?, 'TST', 'Test', 'coingecko', 'volume_spike',
                   ?, 100.0, 10.0, 0.1, 20.0, 10.0, 120.0, 90.0,
                   'open', ?)""",
        (
            trade_id,
            f"coin-{trade_id}",
            json.dumps({}),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    await db._conn.commit()


# ---------- _check_universe (pure helper) ----------


def test_check_universe_flag_off_returns_none():
    """Default OFF: no pattern is matched even for a tokenized-stock id."""
    settings = _settings()  # ALERT_UNIVERSE_FILTER_ENABLED defaults False
    assert _check_universe(settings, "spy-bstocks-tokenized-stock") is None


def test_check_universe_flag_on_matches_first_pattern():
    """First matching pattern (list order) is returned."""
    settings = _settings(ALERT_UNIVERSE_FILTER_ENABLED=True)
    assert (
        _check_universe(settings, "spy-bstocks-tokenized-stock")
        == "-bstocks-tokenized-stock"
    )


def test_check_universe_flag_on_normal_id_returns_none():
    settings = _settings(ALERT_UNIVERSE_FILTER_ENABLED=True)
    assert _check_universe(settings, "drooling-cat") is None


def test_check_universe_is_case_insensitive():
    settings = _settings(ALERT_UNIVERSE_FILTER_ENABLED=True)
    assert (
        _check_universe(settings, "SPY-BSTOCKS-TOKENIZED-STOCK")
        == "-bstocks-tokenized-stock"
    )


def test_check_universe_custom_pattern_override():
    settings = _settings(
        ALERT_UNIVERSE_FILTER_ENABLED=True,
        ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS=["-wrapped-"],
    )
    assert _check_universe(settings, "foo-WRAPPED-bar") == "-wrapped-"
    assert _check_universe(settings, "spy-bstocks-tokenized-stock") is None


# ---------- notify_paper_trade_opened integration ----------


@pytest.mark.asyncio
async def test_universe_flag_off_lets_tokenized_stock_pass(tmp_path, monkeypatch):
    """Flag OFF (default): a tokenized-stock id sends normally (no block)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()  # ALERT_UNIVERSE_FILTER_ENABLED defaults False
    await _insert_paper_trade(db, trade_id=42)
    sent = []

    async def _fake_send(text, session, settings, parse_mode=None, **kwargs):
        sent.append(text)

    async def _no_minara(*args, **kwargs):
        return None

    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)
    monkeypatch.setattr("scout.trading.minara_alert.maybe_minara_command", _no_minara)
    await notify_paper_trade_opened(
        db,
        settings,
        session=None,
        paper_trade_id=42,
        signal_type="volume_spike",
        token_id="spy-bstocks-tokenized-stock",
        symbol="SPY",
        entry_price=1.0,
        amount_usd=100.0,
        signal_data={"spike_ratio": 8.0},
    )
    assert len(sent) == 1
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE paper_trade_id=42"
    )
    assert (await cur.fetchone())[0] == "sent"
    await db.close()


@pytest.mark.asyncio
async def test_universe_flag_on_blocks_tokenized_stock(tmp_path, monkeypatch):
    """Flag ON: spy-bstocks-tokenized-stock is blocked, NOT sent, and audited
    as outcome='blocked_eligibility' detail='universe_filter:-bstocks-tokenized-stock'.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(ALERT_UNIVERSE_FILTER_ENABLED=True)
    await _insert_paper_trade(db, trade_id=42)

    async def _no_send(*args, **kwargs):
        raise AssertionError("universe-filter block must not send")

    monkeypatch.setattr("scout.alerter.send_telegram_message", _no_send)
    await notify_paper_trade_opened(
        db,
        settings,
        session=None,
        paper_trade_id=42,
        signal_type="volume_spike",
        token_id="spy-bstocks-tokenized-stock",
        symbol="SPY",
        entry_price=1.0,
        amount_usd=100.0,
        signal_data={"spike_ratio": 8.0},
    )
    cur = await db._conn.execute(
        "SELECT outcome, detail FROM tg_alert_log WHERE paper_trade_id=42"
    )
    outcome, detail = await cur.fetchone()
    assert outcome == "blocked_eligibility"
    assert detail == "universe_filter:-bstocks-tokenized-stock"
    await db.close()


@pytest.mark.asyncio
async def test_universe_flag_on_normal_memecoin_unaffected(tmp_path, monkeypatch):
    """Flag ON: a normal memecoin id is unaffected — sends normally."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(ALERT_UNIVERSE_FILTER_ENABLED=True)
    await _insert_paper_trade(db, trade_id=42)
    sent = []

    async def _fake_send(text, session, settings, parse_mode=None, **kwargs):
        sent.append(text)

    async def _no_minara(*args, **kwargs):
        return None

    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)
    monkeypatch.setattr("scout.trading.minara_alert.maybe_minara_command", _no_minara)
    await notify_paper_trade_opened(
        db,
        settings,
        session=None,
        paper_trade_id=42,
        signal_type="volume_spike",
        token_id="drooling-cat",
        symbol="DROOL",
        entry_price=0.001,
        amount_usd=100.0,
        signal_data={"spike_ratio": 8.0},
    )
    assert len(sent) == 1
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE paper_trade_id=42"
    )
    assert (await cur.fetchone())[0] == "sent"
    await db.close()


@pytest.mark.asyncio
async def test_universe_flag_on_custom_pattern_case_insensitive(tmp_path, monkeypatch):
    """Flag ON with a custom lower-case pattern blocks a mixed-case id and
    audits the configured pattern in the detail string."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(
        ALERT_UNIVERSE_FILTER_ENABLED=True,
        ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS=["-wrapped-"],
    )
    await _insert_paper_trade(db, trade_id=42)

    async def _no_send(*args, **kwargs):
        raise AssertionError("universe-filter block must not send")

    monkeypatch.setattr("scout.alerter.send_telegram_message", _no_send)
    await notify_paper_trade_opened(
        db,
        settings,
        session=None,
        paper_trade_id=42,
        signal_type="volume_spike",
        token_id="foo-WRAPPED-bar",
        symbol="FOO",
        entry_price=1.0,
        amount_usd=100.0,
        signal_data={"spike_ratio": 8.0},
    )
    cur = await db._conn.execute(
        "SELECT outcome, detail FROM tg_alert_log WHERE paper_trade_id=42"
    )
    outcome, detail = await cur.fetchone()
    assert outcome == "blocked_eligibility"
    assert detail == "universe_filter:-wrapped-"
    await db.close()


@pytest.mark.asyncio
async def test_universe_block_emits_structlog(tmp_path, monkeypatch):
    """tg_alert_blocked_universe structlog event carries token_id, signal_type,
    pattern."""
    from structlog.testing import capture_logs

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(ALERT_UNIVERSE_FILTER_ENABLED=True)
    await _insert_paper_trade(db, trade_id=42)

    async def _no_send(*args, **kwargs):
        raise AssertionError("universe-filter block must not send")

    monkeypatch.setattr("scout.alerter.send_telegram_message", _no_send)
    with capture_logs() as cap:
        await notify_paper_trade_opened(
            db,
            settings,
            session=None,
            paper_trade_id=42,
            signal_type="volume_spike",
            token_id="qualcomm-bstocks-tokenized-stock",
            symbol="QCOM",
            entry_price=1.0,
            amount_usd=100.0,
            signal_data={"spike_ratio": 8.0},
        )
    blocked = [e for e in cap if e["event"] == "tg_alert_blocked_universe"]
    assert len(blocked) == 1
    ev = blocked[0]
    assert ev["token_id"] == "qualcomm-bstocks-tokenized-stock"
    assert ev["signal_type"] == "volume_spike"
    assert ev["pattern"] == "-bstocks-tokenized-stock"
    await db.close()


# ---------- _log_outcome detail backward-compat ----------


@pytest.mark.asyncio
async def test_log_outcome_without_detail_stores_null(tmp_path):
    """Existing callsites pass no detail -> detail column is NULL."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _log_outcome(
        db,
        paper_trade_id=None,
        signal_type="gainers_early",
        token_id="bitcoin",
        outcome="blocked_eligibility",
    )
    cur = await db._conn.execute(
        "SELECT outcome, detail FROM tg_alert_log WHERE token_id='bitcoin'"
    )
    outcome, detail = await cur.fetchone()
    assert outcome == "blocked_eligibility"
    assert detail is None
    await db.close()


@pytest.mark.asyncio
async def test_log_outcome_with_detail_stores_string(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _log_outcome(
        db,
        paper_trade_id=None,
        signal_type="volume_spike",
        token_id="spy-bstocks-tokenized-stock",
        outcome="blocked_eligibility",
        detail="universe_filter:-bstocks-tokenized-stock",
    )
    cur = await db._conn.execute(
        "SELECT detail FROM tg_alert_log WHERE token_id='spy-bstocks-tokenized-stock'"
    )
    assert (await cur.fetchone())[0] == "universe_filter:-bstocks-tokenized-stock"
    await db.close()
