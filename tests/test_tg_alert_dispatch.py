"""BL-NEW-TG-ALERT-ALLOWLIST: dispatch + gate tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.tg_alert_dispatch import (
    DEFAULT_ALLOW_SIGNALS,
    _check_cooldown,
    _check_eligibility,
    format_paper_trade_alert,
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
           VALUES (?, ?, 'TST', 'Test', 'coingecko', 'gainers_early',
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


# ---------- _check_eligibility ----------


@pytest.mark.asyncio
async def test_eligibility_allows_when_flag_is_1(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    assert await _check_eligibility(db, "gainers_early") is True
    await db.close()


@pytest.mark.asyncio
async def test_eligibility_blocks_when_flag_is_0(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    assert await _check_eligibility(db, "first_signal") is False
    await db.close()


@pytest.mark.asyncio
async def test_eligibility_chain_completed_excluded_by_default(tmp_path):
    """R2-C2 fold: chain_completed defaults to tg_alert_eligible=0
    because the existing scout/chains/alerts.py path already alerts."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    assert await _check_eligibility(db, "chain_completed") is False
    await db.close()


@pytest.mark.asyncio
async def test_eligibility_unknown_signal_blocked(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    assert await _check_eligibility(db, "bogus_signal") is False
    await db.close()


# ---------- _check_cooldown ----------


@pytest.mark.asyncio
async def test_cooldown_blocks_within_window(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=6)
    await _insert_paper_trade(db, trade_id=1)
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO tg_alert_log (paper_trade_id, signal_type, token_id, "
        "alerted_at, outcome) VALUES (1, 'gainers_early', 'btc', ?, 'sent')",
        (now.isoformat(),),
    )
    await db._conn.commit()
    assert await _check_cooldown(db, settings, "btc") is True
    await db.close()


@pytest.mark.asyncio
async def test_cooldown_blocks_across_signals_for_same_token(tmp_path):
    """R2-I1 fold: per-token cooldown blocks DIFFERENT signal_type for
    the same token within the window."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=6)
    await _insert_paper_trade(db, trade_id=1)
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO tg_alert_log (paper_trade_id, signal_type, token_id, "
        "alerted_at, outcome) VALUES (1, 'gainers_early', 'btc', ?, 'sent')",
        (now.isoformat(),),
    )
    await db._conn.commit()
    # Different signal_type should still block via per-token cooldown
    assert await _check_cooldown(db, settings, "btc") is True
    await db.close()


@pytest.mark.asyncio
async def test_cooldown_allows_after_window(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=6)
    await _insert_paper_trade(db, trade_id=1)
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_alert_log (paper_trade_id, signal_type, token_id, "
        "alerted_at, outcome) VALUES (1, 'gainers_early', 'btc', ?, 'sent')",
        (old,),
    )
    await db._conn.commit()
    assert await _check_cooldown(db, settings, "btc") is False
    await db.close()


@pytest.mark.asyncio
async def test_cooldown_only_counts_sent_outcome(tmp_path):
    """Failed dispatches and blocked alerts don't count toward cooldown —
    so a transient failure doesn't suppress the next legitimate fire."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=6)
    await _insert_paper_trade(db, trade_id=1)
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_alert_log (paper_trade_id, signal_type, token_id, "
        "alerted_at, outcome) VALUES (1, 'gainers_early', 'btc', ?, 'dispatch_failed')",
        (now,),
    )
    await db._conn.commit()
    assert await _check_cooldown(db, settings, "btc") is False
    await db.close()


# ---------- format_paper_trade_alert (R2-C1 per-signal field maps) ----------


def test_format_gainers_early_renders_dispatcher_fields():
    body = format_paper_trade_alert(
        signal_type="gainers_early",
        symbol="BTC",
        coin_id="bitcoin",
        entry_price=50000.0,
        amount_usd=100.0,
        signal_data={"price_change_24h": 36.92, "mcap": 5_500_000},
    )
    assert "GAINERS EARLY" in body
    assert "BTC" in body
    assert "+36.9%" in body
    assert "$5.5M" in body
    assert "coingecko.com/en/coins/bitcoin" in body


def test_format_narrative_prediction_renders_fit_and_category():
    """R2-C1 fold: narrative_prediction emits {fit, category, mcap}."""
    body = format_paper_trade_alert(
        signal_type="narrative_prediction",
        symbol="DOGE",
        coin_id="dogecoin",
        entry_price=0.15,
        amount_usd=100.0,
        signal_data={"fit": 87, "category": "memecoin", "mcap": 20_000_000_000},
    )
    assert "NARRATIVE PREDICTION" in body
    assert "DOGE" in body
    assert "memecoin" in body
    assert "fit 87" in body
    assert "$20.0B" in body


def test_format_volume_spike_renders_spike_ratio():
    """R2-C1 fold: volume_spike emits {spike_ratio} only."""
    body = format_paper_trade_alert(
        signal_type="volume_spike",
        symbol="PEPE",
        coin_id="pepe",
        entry_price=0.0001,
        amount_usd=100.0,
        signal_data={"spike_ratio": 8.3},
    )
    assert "VOLUME SPIKE" in body
    assert "vol×8.3" in body


def test_format_does_not_use_markdown_specials_in_header():
    """R1-C1 fold: header replaces underscores so dispatch with
    parse_mode=None (or accidentally Markdown) renders cleanly."""
    body = format_paper_trade_alert(
        signal_type="gainers_early",
        symbol="BTC",
        coin_id="bitcoin",
        entry_price=50000.0,
        amount_usd=100.0,
        signal_data={"price_change_24h": 36.92, "mcap": 5_500_000},
    )
    assert "_" not in body.split("\n")[0]  # header has no underscores


# ---------- notify_paper_trade_opened ----------


@pytest.mark.asyncio
async def test_notify_writes_sent_row_on_success(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    await _insert_paper_trade(db, trade_id=42)
    sent = []

    async def _fake_send(text, session, settings, parse_mode=None, **kwargs):
        sent.append((text, parse_mode))

    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)
    await notify_paper_trade_opened(
        db,
        settings,
        session=None,
        paper_trade_id=42,
        signal_type="gainers_early",
        token_id="bitcoin",
        symbol="BTC",
        entry_price=50000.0,
        amount_usd=100.0,
        signal_data={"price_change_24h": 36.92, "mcap": 5_500_000},
    )
    cur = await db._conn.execute(
        "SELECT outcome, signal_type, token_id FROM tg_alert_log "
        "WHERE paper_trade_id=42"
    )
    row = await cur.fetchone()
    assert row[0] == "sent"
    assert row[1] == "gainers_early"
    assert row[2] == "bitcoin"
    assert len(sent) == 1
    # R1-C1: dispatch must be parse_mode=None
    assert sent[0][1] is None
    await db.close()


@pytest.mark.asyncio
async def test_notify_logs_eligibility_block(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    await _insert_paper_trade(db, trade_id=42)
    await notify_paper_trade_opened(
        db,
        settings,
        session=None,
        paper_trade_id=42,
        signal_type="first_signal",  # suspended in default migration
        token_id="bitcoin",
        symbol="BTC",
        entry_price=50000.0,
        amount_usd=100.0,
        signal_data={},
    )
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE paper_trade_id=42"
    )
    assert (await cur.fetchone())[0] == "blocked_eligibility"
    await db.close()


@pytest.mark.asyncio
async def test_notify_handles_dispatch_failure_demotes_to_dispatch_failed(
    tmp_path, monkeypatch
):
    """R2-C2 fold + dispatch-fail demotion: pre-emptive 'sent' row is
    UPDATEd to 'dispatch_failed' when send_telegram_message raises."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    await _insert_paper_trade(db, trade_id=42)

    async def _fail(*a, **kw):
        raise RuntimeError("simulated dispatch failure")

    monkeypatch.setattr("scout.alerter.send_telegram_message", _fail)
    await notify_paper_trade_opened(
        db,
        settings,
        session=None,
        paper_trade_id=42,
        signal_type="gainers_early",
        token_id="bitcoin",
        symbol="BTC",
        entry_price=50000.0,
        amount_usd=100.0,
        signal_data={"price_change_24h": 36.92, "mcap": 5_500_000},
    )
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE paper_trade_id=42"
    )
    assert (await cur.fetchone())[0] == "dispatch_failed"
    await db.close()


@pytest.mark.asyncio
async def test_notify_concurrent_only_one_sent(tmp_path, monkeypatch):
    """R2-C2 fold: 3 concurrent dispatches for same token → exactly 1
    'sent' + 2 'blocked_cooldown'. Atomic check-then-write under
    db._txn_lock prevents race."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=6)
    for tid in (1, 2, 3):
        await _insert_paper_trade(db, trade_id=tid)

    async def _fake_send(text, session, settings, parse_mode=None, **kwargs):
        await asyncio.sleep(0.01)  # simulate I/O so race window opens

    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)
    # Spawn 3 concurrent dispatches for the same token
    await asyncio.gather(
        notify_paper_trade_opened(
            db,
            settings,
            session=None,
            paper_trade_id=1,
            signal_type="gainers_early",
            token_id="btc",
            symbol="BTC",
            entry_price=50000.0,
            amount_usd=100.0,
            signal_data={"price_change_24h": 30.0, "mcap": 1_000_000},
        ),
        notify_paper_trade_opened(
            db,
            settings,
            session=None,
            paper_trade_id=2,
            signal_type="losers_contrarian",
            token_id="btc",
            symbol="BTC",
            entry_price=50000.0,
            amount_usd=100.0,
            signal_data={"price_change_24h": -30.0, "mcap": 1_000_000},
        ),
        notify_paper_trade_opened(
            db,
            settings,
            session=None,
            paper_trade_id=3,
            signal_type="volume_spike",
            token_id="btc",
            symbol="BTC",
            entry_price=50000.0,
            amount_usd=100.0,
            signal_data={"spike_ratio": 5.0},
        ),
    )
    cur = await db._conn.execute(
        "SELECT outcome, COUNT(*) FROM tg_alert_log "
        "WHERE token_id='btc' GROUP BY outcome ORDER BY outcome"
    )
    counts = dict(await cur.fetchall())
    assert counts.get("sent") == 1
    assert counts.get("blocked_cooldown") == 2
    await db.close()


@pytest.mark.asyncio
async def test_default_allow_signals_constant():
    """R2-I1 fold target: auto_suspend revive uses this constant to
    decide which signals get tg_alert_eligible=1 restored."""
    assert "gainers_early" in DEFAULT_ALLOW_SIGNALS
    assert "narrative_prediction" in DEFAULT_ALLOW_SIGNALS
    assert "losers_contrarian" in DEFAULT_ALLOW_SIGNALS
    assert "volume_spike" in DEFAULT_ALLOW_SIGNALS
    assert "chain_completed" not in DEFAULT_ALLOW_SIGNALS
    assert "first_signal" not in DEFAULT_ALLOW_SIGNALS


# ---------- M1.5c integration tests ----------


def test_format_with_minara_command_includes_run_line():
    """M1.5c: when minara_command is provided, body has 'Run: minara swap...' line."""
    body = format_paper_trade_alert(
        signal_type="gainers_early",
        symbol="BONK",
        coin_id="bonk",
        entry_price=0.0001,
        amount_usd=10.0,
        signal_data={"price_change_24h": 50.0, "mcap": 2_000_000},
        minara_command="minara swap --from USDC --to ABC123 --amount-usd 10",
    )
    assert "Run: minara swap --from USDC --to ABC123 --amount-usd 10" in body
    # Run: line appears BEFORE coingecko link
    lines = body.split("\n")
    run_idx = next(i for i, l in enumerate(lines) if l.startswith("Run:"))
    link_idx = next(i for i, l in enumerate(lines) if "coingecko.com" in l)
    assert run_idx < link_idx


def test_format_without_minara_command_unchanged():
    """M1.5c: when minara_command is None, format matches pre-M1.5c output."""
    body = format_paper_trade_alert(
        signal_type="gainers_early",
        symbol="BTC",
        coin_id="bitcoin",
        entry_price=50000.0,
        amount_usd=100.0,
        signal_data={"price_change_24h": 30.0, "mcap": 1_000_000_000_000},
        minara_command=None,
    )
    assert "Run:" not in body


@pytest.mark.asyncio
async def test_notify_includes_minara_command_for_solana_token(tmp_path, monkeypatch):
    """End-to-end: Solana token paper-trade-open alert includes Run: line."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    await _insert_paper_trade(db, trade_id=42)
    sent = []

    async def _fake_send(text, session, settings, parse_mode=None, **kwargs):
        sent.append(text)

    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)

    _SPL = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"solana": _SPL}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)

    await notify_paper_trade_opened(
        db,
        settings,
        session=object(),  # non-None for session-guard
        paper_trade_id=42,
        signal_type="gainers_early",
        token_id="bonk",
        symbol="BONK",
        entry_price=0.0001,
        amount_usd=10.0,
        signal_data={"price_change_24h": 50.0, "mcap": 2_000_000},
    )
    assert len(sent) == 1
    assert f"Run: minara swap --from USDC --to {_SPL}" in sent[0]
    cur = await db._conn.execute(
        "SELECT paper_trade_id, signal_type, coin_id, chain, command_text_observed "
        "FROM minara_alert_emissions"
    )
    row = await cur.fetchone()
    assert row["paper_trade_id"] == 42
    assert row["signal_type"] == "gainers_early"
    assert row["coin_id"] == "bonk"
    assert row["chain"] == "solana"
    assert row["command_text_observed"] == 1
    await db.close()


@pytest.mark.asyncio
async def test_notify_passes_minara_persistence_context(tmp_path, monkeypatch):
    """BL-NEW-MINARA-DB-PERSISTENCE: TG pre-claim id becomes Minara audit context."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    await _insert_paper_trade(db, trade_id=42)
    calls = []

    async def _fake_send(text, session, settings, parse_mode=None, **kwargs):
        pass

    async def _fake_minara(*args, **kwargs):
        return "minara swap --from USDC --to ABC --amount-usd 10"

    async def _fake_persist(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)
    monkeypatch.setattr("scout.trading.minara_alert.maybe_minara_command", _fake_minara)
    monkeypatch.setattr(
        "scout.trading.minara_alert.persist_minara_alert_emission",
        _fake_persist,
    )

    await notify_paper_trade_opened(
        db,
        settings,
        session=object(),
        paper_trade_id=42,
        signal_type="gainers_early",
        token_id="bonk",
        symbol="BONK",
        entry_price=0.0001,
        amount_usd=10.0,
        signal_data={"price_change_24h": 50.0, "mcap": 2_000_000},
    )

    assert len(calls) == 1
    assert calls[0]["db"] is db
    assert calls[0]["paper_trade_id"] == 42
    assert calls[0]["signal_type"] == "gainers_early"
    assert calls[0]["tg_alert_log_id"] is not None
    assert calls[0]["command_text"].startswith("minara swap")
    await db.close()


@pytest.mark.asyncio
async def test_notify_no_minara_command_for_evm_only_token(tmp_path, monkeypatch):
    """Token with platforms.ethereum but no platforms.solana → no Run: line."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    await _insert_paper_trade(db, trade_id=42)
    sent = []

    async def _fake_send(text, session, settings, parse_mode=None, **kwargs):
        sent.append(text)

    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"ethereum": "0xabc"}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)

    await notify_paper_trade_opened(
        db,
        settings,
        session=object(),
        paper_trade_id=42,
        signal_type="gainers_early",
        token_id="random",
        symbol="RND",
        entry_price=1.0,
        amount_usd=10.0,
        signal_data={"price_change_24h": 30.0, "mcap": 5_000_000},
    )
    assert len(sent) == 1
    assert "Run:" not in sent[0]
    await db.close()


@pytest.mark.asyncio
async def test_notify_demotes_sent_row_on_cancelled_error_during_minara_lookup(
    tmp_path, monkeypatch
):
    """PR-V2-I1 fold: asyncio.CancelledError raised inside maybe_minara_command
    (network interrupted, task cancelled mid-fetch) must NOT leave the
    pre-emptive 'sent' row stuck — that would suppress the cooldown for 6h.
    Demote to 'dispatch_failed' then re-raise."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    await _insert_paper_trade(db, trade_id=42)

    async def _send_never_called(*args, **kwargs):
        raise AssertionError("send should not be called on cancel")

    monkeypatch.setattr("scout.alerter.send_telegram_message", _send_never_called)

    async def _cancel_mid_fetch(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "scout.trading.minara_alert.maybe_minara_command", _cancel_mid_fetch
    )

    with pytest.raises(asyncio.CancelledError):
        await notify_paper_trade_opened(
            db,
            settings,
            session=object(),
            paper_trade_id=42,
            signal_type="gainers_early",
            token_id="bonk",
            symbol="BONK",
            entry_price=0.0001,
            amount_usd=10.0,
            signal_data={"price_change_24h": 50.0, "mcap": 2_000_000},
        )

    # Sentinel row must have been demoted, NOT left as 'sent'.
    cur = await db._conn.execute(
        "SELECT outcome, detail FROM tg_alert_log "
        "WHERE token_id='bonk' ORDER BY id DESC LIMIT 1"
    )
    outcome, detail = await cur.fetchone()
    assert outcome == "dispatch_failed"
    assert "cancel" in (detail or "").lower()
    await db.close()


@pytest.mark.asyncio
async def test_notify_demotes_sent_row_on_cancelled_error_during_telegram_send(
    tmp_path, monkeypatch
):
    """Cancellation during Telegram send must not leave pre-claimed row as sent."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    await _insert_paper_trade(db, trade_id=42)

    async def _prepared_minara(*args, **kwargs):
        return "minara swap --from USDC --to ABC --amount-usd 10"

    async def _cancel_send(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "scout.trading.minara_alert.maybe_minara_command", _prepared_minara
    )
    monkeypatch.setattr("scout.alerter.send_telegram_message", _cancel_send)

    with pytest.raises(asyncio.CancelledError):
        await notify_paper_trade_opened(
            db,
            settings,
            session=object(),
            paper_trade_id=42,
            signal_type="gainers_early",
            token_id="bonk",
            symbol="BONK",
            entry_price=0.0001,
            amount_usd=10.0,
            signal_data={"price_change_24h": 50.0, "mcap": 2_000_000},
        )

    cur = await db._conn.execute(
        "SELECT outcome, detail FROM tg_alert_log "
        "WHERE token_id='bonk' ORDER BY id DESC LIMIT 1"
    )
    outcome, detail = await cur.fetchone()
    assert outcome == "dispatch_failed"
    assert "telegram" in (detail or "").lower()
    cur = await db._conn.execute("SELECT COUNT(*) FROM minara_alert_emissions")
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_notify_does_not_persist_minara_when_telegram_send_reports_failure(
    tmp_path, monkeypatch
):
    """Swallowed Telegram failures must be promoted to dispatch_failed here."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    await _insert_paper_trade(db, trade_id=42)

    async def _prepared_minara(*args, **kwargs):
        return "minara swap --from USDC --to ABC --amount-usd 10"

    async def _failed_send(*args, **kwargs):
        assert kwargs["raise_on_failure"] is True
        raise RuntimeError("telegram send failed status=500")

    monkeypatch.setattr(
        "scout.trading.minara_alert.maybe_minara_command", _prepared_minara
    )
    monkeypatch.setattr("scout.alerter.send_telegram_message", _failed_send)

    await notify_paper_trade_opened(
        db,
        settings,
        session=object(),
        paper_trade_id=42,
        signal_type="gainers_early",
        token_id="bonk",
        symbol="BONK",
        entry_price=0.0001,
        amount_usd=10.0,
        signal_data={"price_change_24h": 50.0, "mcap": 2_000_000},
    )

    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE token_id='bonk'"
    )
    assert (await cur.fetchone())[0] == "dispatch_failed"
    cur = await db._conn.execute("SELECT COUNT(*) FROM minara_alert_emissions")
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_notify_does_not_demote_if_persistence_cancelled_after_send(
    tmp_path, monkeypatch
):
    """After Telegram send returns, persistence cancellation must not clear cooldown."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    await _insert_paper_trade(db, trade_id=42)

    async def _prepared_minara(*args, **kwargs):
        return "minara swap --from USDC --to ABC --amount-usd 10"

    async def _send_ok(*args, **kwargs):
        assert kwargs["raise_on_failure"] is True

    async def _persist_cancelled(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "scout.trading.minara_alert.maybe_minara_command", _prepared_minara
    )
    monkeypatch.setattr("scout.alerter.send_telegram_message", _send_ok)
    monkeypatch.setattr(
        "scout.trading.minara_alert.persist_minara_alert_emission",
        _persist_cancelled,
    )

    with pytest.raises(asyncio.CancelledError):
        await notify_paper_trade_opened(
            db,
            settings,
            session=object(),
            paper_trade_id=42,
            signal_type="gainers_early",
            token_id="bonk",
            symbol="BONK",
            entry_price=0.0001,
            amount_usd=10.0,
            signal_data={"price_change_24h": 50.0, "mcap": 2_000_000},
        )

    cur = await db._conn.execute(
        "SELECT outcome, detail FROM tg_alert_log WHERE token_id='bonk'"
    )
    outcome, detail = await cur.fetchone()
    assert outcome == "sent"
    assert detail is None
    await db.close()
