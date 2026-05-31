from __future__ import annotations

import json
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.trade_surface_alerts import (
    format_trade_surface_alert,
    select_trade_surface_alert_candidates,
    send_trade_surface_alerts,
)

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def _settings(**overrides) -> Settings:
    defaults = {
        "_env_file": None,
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_CHAT_ID": "chat",
        "ANTHROPIC_API_KEY": "anthropic",
        "TRADE_SURFACE_TG_ALERTS_ENABLED": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _focus_row(token_id: str, *, source_corpus: str = "paper") -> dict:
    return {
        "row_key": f"{source_corpus}:{token_id}",
        "token_id": token_id,
        "symbol": token_id.upper()[:6],
        "name": token_id.title(),
        "source_corpus": source_corpus,
        "verdict": "candidate_review" if source_corpus == "paper" else "watch",
        "current_move_pct": 12.34,
        "market_cap": 12_000_000,
        "surfaces": ["chain_completed"],
        "price_is_stale": False,
    }


def _now_row(token_id: str, *, verdict: str = "candidate_review") -> dict:
    return {
        "token_id": token_id,
        "symbol": token_id.upper()[:6],
        "name": token_id.title(),
        "verdict": verdict,
        "pct_from_entry": 3.21,
        "market_cap": 20_000_000,
        "surfaces": ["chain_completed"],
        "price_is_stale": False,
        "risk_reasons": [],
        "inclusion_reasons": ["would_be_live=1"],
    }


def test_selector_prioritizes_overlap_then_now_candidates_then_focus_fill():
    focus_payload = {
        "rows": [
            _focus_row("overlap"),
            _focus_row("focus-only", source_corpus="tracker"),
        ]
    }
    now_payload = {
        "rows": [
            _now_row("overlap"),
            _now_row("now-only"),
            _now_row("watch-only", verdict="watch"),
        ]
    }

    selected = select_trade_surface_alert_candidates(
        focus_payload, now_payload, max_candidates=3
    )

    assert [c.token_id for c in selected] == ["overlap", "now-only", "focus-only"]
    assert selected[0].surface == "todays_focus+now_tradable"
    assert selected[0].source_corpus == "paper"
    assert selected[1].surface == "now_tradable"
    assert selected[1].source_corpus == "paper"
    assert selected[2].surface == "todays_focus"
    assert selected[2].source_corpus == "tracker"


def test_selector_excludes_stale_and_non_candidate_now_rows():
    stale_focus = _focus_row("stale")
    stale_focus["price_is_stale"] = True
    stale_now = _now_row("stale-now")
    stale_now["price_is_stale"] = True
    selected = select_trade_surface_alert_candidates(
        {"rows": [stale_focus]},
        {"rows": [stale_now, _now_row("blocked", verdict="blocked")]},
        max_candidates=5,
    )

    assert selected == []


def test_format_trade_surface_alert_is_plain_factual_copy():
    body = format_trade_surface_alert(
        select_trade_surface_alert_candidates(
            {"rows": [_focus_row("mocaverse")]},
            {"rows": [_now_row("mocaverse")]},
            max_candidates=1,
        )[0]
    )

    assert "TODAY FOCUS + NOW TRADABLE" in body
    assert "MOCA" in body
    assert "coingecko.com/en/coins/mocaverse" in body
    banned = ("buy", "sell", "trade now", "urgent", "moon", "guaranteed")
    assert not any(word in body.lower() for word in banned)


@pytest.mark.asyncio
async def test_send_trade_surface_alerts_writes_sent_row_and_uses_plain_parse_mode(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "surface.db")
    await db.initialize()
    sent = []

    async def _send(
        body,
        session,
        settings,
        *,
        parse_mode=None,
        raise_on_failure=False,
        source="unattributed",
    ):
        sent.append(
            {
                "body": body,
                "parse_mode": parse_mode,
                "raise_on_failure": raise_on_failure,
                "source": source,
            }
        )

    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.alerter.send_telegram_message", _send
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts._load_today_focus_alert_payload",
        lambda db_path, window_hours=36: {"rows": [_focus_row("mocaverse")]},
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.dashboard_db.get_live_candidates",
        lambda db_path, limit=30, window_hours=36: {
            "rows": [_now_row("mocaverse")],
        },
    )

    result = await send_trade_surface_alerts(db, _settings(), object())

    assert result == {"sent": 1, "blocked_dedup_24h": 0, "dispatch_failed": 0}
    assert sent and sent[0]["parse_mode"] is None
    assert sent[0]["raise_on_failure"] is True
    assert sent[0]["source"] == "trade_surface_alerts"
    cur = await db._conn.execute(
        "SELECT paper_trade_id, signal_type, token_id, outcome, detail "
        "FROM tg_alert_log"
    )
    row = await cur.fetchone()
    assert row["paper_trade_id"] is None
    assert row["signal_type"] == "trade_surface"
    assert row["token_id"] == "mocaverse"
    assert row["outcome"] == "sent"
    detail = json.loads(row["detail"])
    assert detail["surface"] == "todays_focus+now_tradable"
    assert detail["source_corpus"] == "paper"
    await db.close()


def test_trade_surface_alert_settings_caps_are_policy_bounded():
    assert _settings(TRADE_SURFACE_TG_ALERTS_MAX_PER_DAY=5)
    assert _settings(TRADE_SURFACE_TG_ALERTS_MAX_PER_RUN=5)

    with pytest.raises(ValueError):
        _settings(TRADE_SURFACE_TG_ALERTS_MAX_PER_DAY=6)
    with pytest.raises(ValueError):
        _settings(TRADE_SURFACE_TG_ALERTS_MAX_PER_RUN=6)


@pytest.mark.asyncio
async def test_send_trade_surface_alerts_blocks_duplicate_token_within_window(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "surface.db")
    await db.initialize()
    prior = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
        "VALUES (NULL, 'trade_surface', 'mocaverse', ?, 'sent')",
        (prior,),
    )
    await db._conn.commit()
    sent = []

    async def _send(*args, **kwargs):
        sent.append(args)

    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.alerter.send_telegram_message", _send
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts._load_today_focus_alert_payload",
        lambda db_path, window_hours=36: {"rows": [_focus_row("mocaverse")]},
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.dashboard_db.get_live_candidates",
        lambda db_path, limit=30, window_hours=36: {"rows": [_now_row("mocaverse")]},
    )

    result = await send_trade_surface_alerts(
        db,
        _settings(TRADE_SURFACE_TG_ALERTS_DEDUP_HOURS=24),
        object(),
    )

    assert result == {"sent": 0, "blocked_dedup_24h": 1, "dispatch_failed": 0}
    assert sent == []
    cur = await db._conn.execute(
        "SELECT outcome, detail FROM tg_alert_log ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    assert row["outcome"] == "blocked_dedup_24h"
    detail = json.loads(row["detail"])
    assert detail["surface"] == "todays_focus+now_tradable"
    assert detail["source_corpus"] == "paper"
    assert detail["dedup_window_h"] == 24
    await db.close()


@pytest.mark.asyncio
async def test_dispatch_failure_preserves_provenance_in_detail(tmp_path, monkeypatch):
    db = Database(tmp_path / "surface.db")
    await db.initialize()

    async def _fail(*args, **kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.alerter.send_telegram_message", _fail
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts._load_today_focus_alert_payload",
        lambda db_path, window_hours=36: {"rows": [_focus_row("mocaverse")]},
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.dashboard_db.get_live_candidates",
        lambda db_path, limit=30, window_hours=36: {"rows": [_now_row("mocaverse")]},
    )

    result = await send_trade_surface_alerts(db, _settings(), object())

    assert result == {"sent": 0, "blocked_dedup_24h": 0, "dispatch_failed": 1}
    cur = await db._conn.execute(
        "SELECT outcome, detail FROM tg_alert_log ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    assert row["outcome"] == "dispatch_failed"
    detail = json.loads(row["detail"])
    assert detail["surface"] == "todays_focus+now_tradable"
    assert detail["source_corpus"] == "paper"
    assert detail["error"] == "telegram down"
    await db.close()


@pytest.mark.asyncio
async def test_format_failure_demotes_claimed_sent_row(tmp_path, monkeypatch):
    db = Database(tmp_path / "surface.db")
    await db.initialize()

    def _bad_format(candidate):
        raise TypeError("bad format")

    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.format_trade_surface_alert", _bad_format
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts._load_today_focus_alert_payload",
        lambda db_path, window_hours=36: {"rows": [_focus_row("mocaverse")]},
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.dashboard_db.get_live_candidates",
        lambda db_path, limit=30, window_hours=36: {"rows": [_now_row("mocaverse")]},
    )

    result = await send_trade_surface_alerts(db, _settings(), object())

    assert result == {"sent": 0, "blocked_dedup_24h": 0, "dispatch_failed": 1}
    cur = await db._conn.execute(
        "SELECT outcome, detail FROM tg_alert_log ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    assert row["outcome"] == "dispatch_failed"
    detail = json.loads(row["detail"])
    assert detail["source_corpus"] == "paper"
    assert detail["error"] == "bad format"
    await db.close()


@pytest.mark.asyncio
async def test_dispatch_cancel_demotes_claimed_sent_row(tmp_path, monkeypatch):
    db = Database(tmp_path / "surface.db")
    await db.initialize()

    async def _cancel(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.alerter.send_telegram_message", _cancel
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts._load_today_focus_alert_payload",
        lambda db_path, window_hours=36: {"rows": [_focus_row("mocaverse")]},
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.dashboard_db.get_live_candidates",
        lambda db_path, limit=30, window_hours=36: {"rows": [_now_row("mocaverse")]},
    )

    with pytest.raises(asyncio.CancelledError):
        await send_trade_surface_alerts(db, _settings(), object())

    cur = await db._conn.execute(
        "SELECT outcome, detail FROM tg_alert_log ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    assert row["outcome"] == "dispatch_failed"
    detail = json.loads(row["detail"])
    assert detail["source_corpus"] == "paper"
    assert detail["error"] == "cancelled_during_telegram_send"
    await db.close()


@pytest.mark.asyncio
async def test_send_trade_surface_alerts_respects_daily_cap(tmp_path, monkeypatch):
    db = Database(tmp_path / "surface.db")
    await db.initialize()
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    await db._conn.execute(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
        "VALUES (NULL, 'trade_surface', 'already-sent', ?, 'sent')",
        (start.isoformat(),),
    )
    await db._conn.commit()
    sent = []

    async def _send(*args, **kwargs):
        sent.append(args)

    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.alerter.send_telegram_message", _send
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts._load_today_focus_alert_payload",
        lambda db_path, window_hours=36: {"rows": [_focus_row("mocaverse")]},
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.dashboard_db.get_live_candidates",
        lambda db_path, limit=30, window_hours=36: {"rows": [_now_row("mocaverse")]},
    )

    result = await send_trade_surface_alerts(
        db, _settings(TRADE_SURFACE_TG_ALERTS_MAX_PER_DAY=1), object()
    )

    assert result == {"sent": 0, "blocked_dedup_24h": 0, "dispatch_failed": 0}
    assert sent == []
    await db.close()


@pytest.mark.asyncio
async def test_surface_alert_claim_checks_dedup_atomically(tmp_path, monkeypatch):
    db = Database(tmp_path / "surface.db")
    await db.initialize()
    inserted_during_claim = False
    real_execute = db._conn.execute

    async def _race_execute(sql, parameters=None):
        nonlocal inserted_during_claim
        if (
            not inserted_during_claim
            and isinstance(sql, str)
            and "INSERT INTO tg_alert_log" in sql
            and "outcome" in sql
        ):
            inserted_during_claim = True
            await real_execute(
                "INSERT INTO tg_alert_log "
                "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
                "VALUES (NULL, 'chain_completed', 'mocaverse', ?, 'sent')",
                (datetime.now(timezone.utc).isoformat(),),
            )
        if parameters is None:
            return await real_execute(sql)
        return await real_execute(sql, parameters)

    monkeypatch.setattr(db._conn, "execute", _race_execute)
    sent = []

    async def _send(*args, **kwargs):
        sent.append(args)

    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.alerter.send_telegram_message", _send
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts._load_today_focus_alert_payload",
        lambda db_path, window_hours=36: {"rows": [_focus_row("mocaverse")]},
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.dashboard_db.get_live_candidates",
        lambda db_path, limit=30, window_hours=36: {"rows": [_now_row("mocaverse")]},
    )

    result = await send_trade_surface_alerts(db, _settings(), object())

    assert result["sent"] == 0
    assert result["blocked_dedup_24h"] == 1
    assert sent == []
    await db.close()


def test_pipeline_loop_wires_trade_surface_alerts_as_opt_in_non_dry_run_lane():
    main_src = (REPO_ROOT / "scout" / "main.py").read_text(encoding="utf-8")

    assert "TRADE_SURFACE_TG_ALERTS_ENABLED" in main_src
    assert "send_trade_surface_alerts" in main_src
    assert "not args.dry_run" in main_src


def test_dispatcher_does_not_call_todays_focus_endpoint_helper():
    src = (REPO_ROOT / "scout" / "trading" / "trade_surface_alerts.py").read_text(
        encoding="utf-8"
    )

    assert "get_todays_focus(" not in src
    assert "get_trade_inbox(" in src
