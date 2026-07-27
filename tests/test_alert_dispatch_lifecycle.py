"""P0-1: shared Telegram-alert dispatch lifecycle — unit + cross-lane + migration.

Proves the ONE write-ahead-intent state model
(dispatch_pending / dispatch_attempted / sent / dispatch_failed /
delivery_unknown_after_send) is honest, idempotent, correctly reserved, and that
every ``outcome=`` reporting contract keeps counting only ``sent`` as delivered.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import alert_dispatch_lifecycle as lc

ST = "trade_surface"


async def _seed(db, token, outcome, alerted_at=None, *, signal_type=ST):
    await db.execute_write(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
        "VALUES (NULL, ?, ?, ?, ?)",
        (
            signal_type,
            token,
            alerted_at or datetime.now(timezone.utc).isoformat(),
            outcome,
        ),
    )
    cur = await db._conn.execute(
        "SELECT id FROM tg_alert_log WHERE token_id=? ORDER BY id DESC LIMIT 1",
        (token,),
    )
    return (await cur.fetchone())[0]


async def _outcome(db, row_id):
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE id=?", (row_id,)
    )
    row = await cur.fetchone()
    return row[0] if row else None


# --- reservation-set invariants --------------------------------------------


def test_reservation_sets_are_frozen_and_correct():
    assert lc.SENT == "sent"
    assert set(lc.CAP_RESERVED_STATES) == {
        "sent",
        "dispatch_pending",
        "dispatch_attempted",
        "delivery_unknown_after_send",
    }
    assert set(lc.DEDUP_RESERVED_STATES) == {
        "sent",
        "dispatch_pending",
        "dispatch_attempted",
    }
    # dispatch_failed frees the slot (neither reserved).
    assert "dispatch_failed" not in lc.CAP_RESERVED_STATES
    assert "dispatch_failed" not in lc.DEDUP_RESERVED_STATES
    # Deterministic + frozen threshold.
    assert (
        isinstance(lc.STALE_RECONCILE_SECONDS, int) and lc.STALE_RECONCILE_SECONDS > 0
    )


# --- transition helpers -----------------------------------------------------


@pytest.mark.asyncio
async def test_mark_attempted_then_promote_is_idempotent(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    rid = await _seed(db, "tok", "dispatch_pending")
    await lc.mark_attempted(db, rid)
    assert await _outcome(db, rid) == "dispatch_attempted"
    await lc.promote_sent(db, rid)
    assert await _outcome(db, rid) == "sent"
    # Idempotent promotion: a second promote is a no-op (guarded on attempted).
    await lc.promote_sent(db, rid)
    assert await _outcome(db, rid) == "sent"
    await db.close()


@pytest.mark.asyncio
async def test_promote_only_from_attempted(tmp_path):
    """A pending row cannot jump straight to sent — mark_attempted first."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    rid = await _seed(db, "tok", "dispatch_pending")
    await lc.promote_sent(db, rid)  # guarded on attempted -> no-op
    assert await _outcome(db, rid) == "dispatch_pending"
    await db.close()


@pytest.mark.asyncio
async def test_demote_guarded_never_clobbers_sent(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    rid = await _seed(db, "tok", "sent")
    await lc.demote_failed(db, rid, detail="late failure")
    assert await _outcome(db, rid) == "sent"  # a delivered row is never demoted
    await db.close()


@pytest.mark.asyncio
async def test_mark_delivery_unknown_only_from_attempted(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    rid_p = await _seed(db, "p", "dispatch_pending")
    await lc.mark_delivery_unknown(db, rid_p, detail="x")
    assert await _outcome(db, rid_p) == "dispatch_pending"  # guarded — no-op
    rid_a = await _seed(db, "a", "dispatch_attempted")
    await lc.mark_delivery_unknown(db, rid_a, detail="x")
    assert await _outcome(db, rid_a) == "delivery_unknown_after_send"
    await db.close()


# --- reconciliation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_pending_to_failed_attempted_to_unknown(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    old = (
        datetime.now(timezone.utc) - timedelta(seconds=lc.STALE_RECONCILE_SECONDS + 60)
    ).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    rp = await _seed(db, "old-pending", "dispatch_pending", old)
    ra = await _seed(db, "old-attempted", "dispatch_attempted", old)
    rpf = await _seed(db, "fresh-pending", "dispatch_pending", fresh)

    result = await lc.reconcile_stale(db, ST)
    assert result == {"pending_failed": 1, "attempted_unknown": 1}
    assert await _outcome(db, rp) == "dispatch_failed"
    assert await _outcome(db, ra) == "delivery_unknown_after_send"
    assert await _outcome(db, rpf) == "dispatch_pending"  # fresh row untouched
    await db.close()


@pytest.mark.asyncio
async def test_reconcile_is_signal_type_scoped(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    old = (
        datetime.now(timezone.utc) - timedelta(seconds=lc.STALE_RECONCILE_SECONDS + 60)
    ).isoformat()
    other = await _seed(
        db, "other", "dispatch_pending", old, signal_type="gainers_early"
    )
    await lc.reconcile_stale(db, ST)  # only sweeps trade_surface
    assert await _outcome(db, other) == "dispatch_pending"  # untouched
    await db.close()


# --- delivered-reporting counts ONLY 'sent' --------------------------------


@pytest.mark.asyncio
async def test_delivery_counts_only_sent(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    for tok, oc in [
        ("a", "sent"),
        ("b", "dispatch_pending"),
        ("c", "dispatch_attempted"),
        ("d", "dispatch_failed"),
        ("e", "delivery_unknown_after_send"),
    ]:
        await _seed(db, tok, oc)
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log WHERE outcome='sent'"
    )
    assert (await cur.fetchone())[0] == 1  # only the delivered row
    # cap-reservation counts all except dispatch_failed
    cur = await db._conn.execute(
        f"SELECT COUNT(*) FROM tg_alert_log WHERE outcome IN {lc.cap_reserved_in_clause()}"
    )
    assert (await cur.fetchone())[0] == 4
    await db.close()


# --- migration: legacy rows reportable + idempotent ------------------------


@pytest.mark.asyncio
async def test_migration_admits_new_states_and_keeps_legacy_rows(tmp_path):
    """The CHECK-widening migration admits the intent states, preserves legacy
    rows verbatim (still reportable via outcome='sent'), and is idempotent."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    # legacy delivered row
    await _seed(db, "legacy", "sent")
    # every new intent state must now be INSERTable (CHECK widened)
    for oc in (
        "dispatch_pending",
        "dispatch_attempted",
        "delivery_unknown_after_send",
    ):
        await _seed(db, f"new-{oc}", oc)
    # legacy row still reportable as delivered
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log WHERE token_id='legacy' AND outcome='sent'"
    )
    assert (await cur.fetchone())[0] == 1
    # re-running the migration is a no-op (idempotent) and preserves rows
    await db._migrate_tg_alert_log_dispatch_pending_outcome()
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_alert_log")
    assert (await cur.fetchone())[0] == 4
    await db.close()


@pytest.mark.asyncio
async def test_migration_recorded_in_paper_migrations(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT 1 FROM paper_migrations WHERE name=?",
        ("bl_tg_alert_log_dispatch_pending_outcome",),
    )
    assert (await cur.fetchone()) is not None
    await db.close()


# --- dedup race: two concurrent claims cannot both reserve -----------------


@pytest.mark.asyncio
async def test_dedup_race_only_one_claim_reserves(tmp_path, monkeypatch):
    """Two concurrent trade-surface dispatches for the SAME token cannot both
    reserve a slot: the dedup NOT EXISTS over reserved states serializes under
    the manager transaction so exactly one claims (pending), the other is
    blocked_dedup_24h."""
    from scout.trading.trade_surface_alerts import _send_claimed_alert
    from scout.trading.trade_surface_alerts import TradeSurfaceAlertCandidate

    db = Database(tmp_path / "d.db")
    await db.initialize()

    sent = []

    async def _ok(*a, **k):
        sent.append(a)

    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.alerter.send_telegram_message", _ok
    )
    monkeypatch.setattr(
        "scout.trading.trade_surface_alerts.format_trade_surface_alert",
        lambda c: "body",
    )

    def _cand():
        return TradeSurfaceAlertCandidate(
            token_id="racetok",
            symbol="RACE",
            name="Race",
            surface="now_tradable",
            verdict="candidate_review",
            market_cap=1_000_000,
            current_price=1.0,
            move_pct=1.0,
            source_corpus="paper",
            surfaces=("now_tradable",),
            reasons=(),
        )

    from scout.config import Settings

    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    r1, r2 = await asyncio.gather(
        _send_claimed_alert(db, s, object(), candidate=_cand(), window_hours=24),
        _send_claimed_alert(db, s, object(), candidate=_cand(), window_hours=24),
    )
    outcomes = sorted([r1, r2])
    assert outcomes == ["blocked_dedup_24h", "sent"], outcomes
    # exactly one reserved+delivered row, one blocked row.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log WHERE token_id='racetok' AND outcome='sent'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()
