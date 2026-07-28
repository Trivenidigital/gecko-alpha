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
    # P0-1: cancel occurs after dispatch_attempted (send began) → the provider
    # result is unprovable, so the row is delivery_unknown_after_send, never sent.
    assert row["outcome"] == "delivery_unknown_after_send"
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


@pytest.mark.asyncio
async def test_send_trade_surface_alerts_paces_multiple_send_attempts(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "surface.db")
    await db.initialize()
    sent = []
    sleeps = []

    async def _send(*args, **kwargs):
        sent.append(args)

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.alerter.send_telegram_message", _send
    )
    monkeypatch.setattr("scout.trading.trade_surface_alerts.asyncio.sleep", _sleep)
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts._load_today_focus_alert_payload",
        lambda db_path, window_hours=36: {
            "rows": [
                _focus_row("one"),
                _focus_row("two"),
                _focus_row("three"),
            ]
        },
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.dashboard_db.get_live_candidates",
        lambda db_path, limit=30, window_hours=36: {
            "rows": [
                _now_row("one"),
                _now_row("two"),
                _now_row("three"),
            ]
        },
    )

    result = await send_trade_surface_alerts(
        db,
        _settings(TRADE_SURFACE_TG_ALERTS_SEND_SPACING_SECONDS=1.25),
        object(),
    )

    assert result == {"sent": 3, "blocked_dedup_24h": 0, "dispatch_failed": 0}
    assert len(sent) == 3
    assert sleeps == [1.25, 1.25]


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


# ---------------------------------------------------------------------------
# P0-1: write-ahead-intent state model (dispatch_pending → sent / dispatch_failed
# / delivery_unknown_after_send). No false 'sent' under crash; pending reserves
# the dedup + cap slot but is never counted as delivered.
# ---------------------------------------------------------------------------

_ALERTER = "scout.trading.trade_surface_alerts.alerter.send_telegram_message"


def _patch_candidates(monkeypatch, token: str = "mocaverse") -> None:
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts._load_today_focus_alert_payload",
        lambda db_path, window_hours=36: {"rows": [_focus_row(token)]},
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.dashboard_db.get_live_candidates",
        lambda db_path, limit=30, window_hours=36: {"rows": [_now_row(token)]},
    )


def _patch_no_candidates(monkeypatch) -> None:
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts._load_today_focus_alert_payload",
        lambda db_path, window_hours=36: {"rows": []},
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.dashboard_db.get_live_candidates",
        lambda db_path, limit=30, window_hours=36: {"rows": []},
    )


async def _seed_alert(db, token, outcome, alerted_at):
    await db.execute_write(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
        "VALUES (NULL, 'trade_surface', ?, ?, ?)",
        (token, alerted_at, outcome),
    )


@pytest.mark.asyncio
async def test_p0_1_success_promotes_pending_to_sent(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db")
    await db.initialize()
    sent = []

    async def _ok(*a, **k):
        sent.append(a)

    monkeypatch.setattr(_ALERTER, _ok)
    _patch_candidates(monkeypatch)
    result = await send_trade_surface_alerts(db, _settings(), object())
    assert result["sent"] == 1 and sent
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE signal_type='trade_surface'"
    )
    outcomes = [r[0] for r in await cur.fetchall()]
    assert "sent" in outcomes
    assert "dispatch_pending" not in outcomes  # promoted; none left pending
    await db.close()


@pytest.mark.asyncio
async def test_p0_1_failure_demotes_to_dispatch_failed(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db")
    await db.initialize()

    async def _fail(*a, **k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(_ALERTER, _fail)
    _patch_candidates(monkeypatch)
    result = await send_trade_surface_alerts(db, _settings(), object())
    assert result["dispatch_failed"] == 1
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE signal_type='trade_surface' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert (await cur.fetchone())[0] == "dispatch_failed"
    await db.close()


@pytest.mark.asyncio
async def test_p0_1_cancellation_never_leaves_sent(tmp_path, monkeypatch):
    """Cancel during the send (after dispatch_attempted) → delivery_unknown, never
    a false 'sent'."""
    db = Database(tmp_path / "s.db")
    await db.initialize()

    async def _cancel(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(_ALERTER, _cancel)
    _patch_candidates(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        await send_trade_surface_alerts(db, _settings(), object())
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE signal_type='trade_surface'"
    )
    outcomes = [r[0] for r in await cur.fetchall()]
    assert "sent" not in outcomes
    assert outcomes == ["delivery_unknown_after_send"]
    await db.close()


@pytest.mark.asyncio
async def test_p0_1_crash_during_format_stays_pending(tmp_path, monkeypatch):
    """Crash (uncaught BaseException) during preprocessing/formatting — BEFORE the
    provider call — leaves the row dispatch_pending (send never started), never a
    false 'sent'. Distinguishable from a crash after the send began."""
    db = Database(tmp_path / "s.db")
    await db.initialize()

    class _Kill(BaseException):
        pass

    def _kill_format(candidate):
        raise _Kill("SIGKILL during format (preprocessing)")

    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.format_trade_surface_alert", _kill_format
    )
    _patch_candidates(monkeypatch)
    with pytest.raises(_Kill):
        await send_trade_surface_alerts(db, _settings(), object())
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE signal_type='trade_surface' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert (await cur.fetchone())[0] == "dispatch_pending"
    await db.close()


@pytest.mark.asyncio
async def test_p0_1_crash_during_send_stays_attempted(tmp_path, monkeypatch):
    """Crash (uncaught BaseException) during the send — AFTER dispatch_attempted —
    leaves the row dispatch_attempted (reconciled later to
    delivery_unknown_after_send), never a false 'sent'."""
    db = Database(tmp_path / "s.db")
    await db.initialize()

    class _Kill(BaseException):
        pass

    async def _kill(*a, **k):
        raise _Kill("SIGKILL mid-send")

    monkeypatch.setattr(_ALERTER, _kill)
    _patch_candidates(monkeypatch)
    with pytest.raises(_Kill):
        await send_trade_surface_alerts(db, _settings(), object())
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE signal_type='trade_surface' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert (await cur.fetchone())[0] == "dispatch_attempted"
    await db.close()


@pytest.mark.asyncio
async def test_p0_1_stale_reconciliation_pending_failed_attempted_unknown(
    tmp_path, monkeypatch
):
    """Stale-intent sweep: stale dispatch_pending → dispatch_failed (never reached
    the provider); stale dispatch_attempted → delivery_unknown_after_send."""
    db = Database(tmp_path / "s.db")
    await db.initialize()
    old = (datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat()
    await _seed_alert(db, "crashed-pending", "dispatch_pending", old)
    await _seed_alert(db, "crashed-attempted", "dispatch_attempted", old)

    async def _ok(*a, **k):
        pass

    monkeypatch.setattr(_ALERTER, _ok)
    _patch_no_candidates(monkeypatch)
    await send_trade_surface_alerts(db, _settings(), object())
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE token_id='crashed-pending'"
    )
    assert (await cur.fetchone())[0] == "dispatch_failed"
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE token_id='crashed-attempted'"
    )
    assert (await cur.fetchone())[0] == "delivery_unknown_after_send"
    await db.close()


@pytest.mark.asyncio
async def test_p0_1_pending_reserves_dedup_slot(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db")
    await db.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await _seed_alert(db, "mocaverse", "dispatch_pending", now)
    sent = []

    async def _ok(*a, **k):
        sent.append(a)

    monkeypatch.setattr(_ALERTER, _ok)
    _patch_candidates(monkeypatch, "mocaverse")
    result = await send_trade_surface_alerts(db, _settings(), object())
    assert result["blocked_dedup_24h"] == 1 and result["sent"] == 0
    assert sent == []  # deduped against the pending reservation — no send
    await db.close()


@pytest.mark.asyncio
async def test_p0_1_pending_counts_toward_cap_not_delivered(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db")
    await db.initialize()
    now = datetime.now(timezone.utc).isoformat()
    for i in range(5):  # fill the cap entirely with RESERVED pending rows
        await _seed_alert(db, f"tok{i}", "dispatch_pending", now)
    sent = []

    async def _ok(*a, **k):
        sent.append(a)

    monkeypatch.setattr(_ALERTER, _ok)
    _patch_candidates(monkeypatch, "newtok")
    result = await send_trade_surface_alerts(
        db, _settings(TRADE_SURFACE_TG_ALERTS_MAX_PER_DAY=5), object()
    )
    assert sent == []  # cap reserved by pending rows → no new send
    assert result["sent"] == 0
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log WHERE outcome='sent'"
    )
    assert (await cur.fetchone())[0] == 0  # pending is NEVER counted as delivered
    await db.close()


# ---------------------------------------------------------------------------
# P0-2: ownership lease — a concurrent stale reconciliation can NEVER demote an
# actively-sending dispatch, and the owner's promotion still succeeds.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p0_2_reconcile_cannot_demote_active_send(tmp_path, monkeypatch):
    """Event-ordered: pause the provider send after the row is marked
    dispatch_attempted (lease heartbeat fresh), run reconcile_stale concurrently,
    prove the leased row is NOT demoted, then release the send and prove the owner
    still promotes it to 'sent'."""
    from scout.trading import alert_dispatch_lifecycle as lifecycle
    from scout.trading.trade_surface_alerts import (
        SIGNAL_TYPE,
        TradeSurfaceAlertCandidate,
        _send_claimed_alert,
    )

    db = Database(tmp_path / "s.db")
    await db.initialize()

    sending = asyncio.Event()
    may_finish = asyncio.Event()

    async def _blocking_send(*a, **k):
        sending.set()
        await may_finish.wait()

    monkeypatch.setattr(_ALERTER, _blocking_send)
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.format_trade_surface_alert",
        lambda c: "body",
    )

    cand = TradeSurfaceAlertCandidate(
        token_id="leasetok",
        symbol="LEASE",
        name="Lease",
        surface="now_tradable",
        verdict="candidate_review",
        market_cap=1_000_000,
        current_price=1.0,
        move_pct=1.0,
        source_corpus="paper",
        surfaces=("now_tradable",),
        reasons=(),
    )

    task = asyncio.create_task(
        _send_claimed_alert(db, _settings(), object(), candidate=cand, window_hours=24)
    )
    await sending.wait()
    # The row is now dispatch_attempted with a fresh lease heartbeat.
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE token_id='leasetok'"
    )
    assert (await cur.fetchone())[0] == "dispatch_attempted"

    # Reconcile runs WHILE the send is in flight — must be a no-op.
    result = await lifecycle.reconcile_stale(db, SIGNAL_TYPE)
    assert result == {"pending_failed": 0, "attempted_unknown": 0}
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE token_id='leasetok'"
    )
    assert (await cur.fetchone())[0] == "dispatch_attempted"  # NOT demoted

    may_finish.set()
    assert await task == "sent"
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE token_id='leasetok'"
    )
    assert (await cur.fetchone())[0] == "sent"
    await db.close()
