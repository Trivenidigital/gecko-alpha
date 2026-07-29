"""P0-1 + P0-2: shared Telegram-alert dispatch lifecycle — unit + lease ownership
+ cross-lane + migration.

Proves the ONE write-ahead-intent state model
(dispatch_pending / dispatch_attempted / sent / dispatch_failed /
delivery_unknown_after_send + operator-only unknown_resolved_retryable) is
honest, ownership-lease-guarded, correctly reserved, and that every ``outcome=``
reporting contract keeps counting only ``sent`` as delivered.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import alert_dispatch_lifecycle as lc

ST = "trade_surface"


async def _seed(
    db,
    token,
    outcome,
    alerted_at=None,
    *,
    signal_type=ST,
    lease=None,
    updated_at=None,
):
    """Insert a tg_alert_log row. ``lease`` + ``updated_at`` populate the P0-2
    ownership columns so transition CAS guards can match."""
    await db.execute_write(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome, "
        " dispatch_state_updated_at, dispatch_lease_token) "
        "VALUES (NULL, ?, ?, ?, ?, ?, ?)",
        (
            signal_type,
            token,
            alerted_at or datetime.now(timezone.utc).isoformat(),
            outcome,
            updated_at,
            lease,
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


def _old_iso():
    return (
        datetime.now(timezone.utc) - timedelta(seconds=lc.STALE_RECONCILE_SECONDS + 60)
    ).isoformat()


def _fresh_iso():
    return datetime.now(timezone.utc).isoformat()


# --- reservation-set invariants --------------------------------------------


def test_reservation_sets_are_frozen_and_correct():
    assert lc.SENT == "sent"
    assert set(lc.CAP_RESERVED_STATES) == {
        "sent",
        "dispatch_pending",
        "dispatch_attempted",
        "delivery_unknown_after_send",
    }
    # P0-1: delivery_unknown now reserves DEDUP too (NOT auto-retryable).
    assert set(lc.DEDUP_RESERVED_STATES) == {
        "sent",
        "dispatch_pending",
        "dispatch_attempted",
        "delivery_unknown_after_send",
    }
    # dispatch_failed frees the slot (neither reserved).
    assert "dispatch_failed" not in lc.CAP_RESERVED_STATES
    assert "dispatch_failed" not in lc.DEDUP_RESERVED_STATES
    # Operator-only terminal state frees BOTH — but nothing automatic writes it.
    assert lc.UNKNOWN_RESOLVED_RETRYABLE == "unknown_resolved_retryable"
    assert lc.UNKNOWN_RESOLVED_RETRYABLE not in lc.CAP_RESERVED_STATES
    assert lc.UNKNOWN_RESOLVED_RETRYABLE not in lc.DEDUP_RESERVED_STATES
    # Deterministic + frozen threshold.
    assert (
        isinstance(lc.STALE_RECONCILE_SECONDS, int) and lc.STALE_RECONCILE_SECONDS > 0
    )


def test_no_automatic_writer_produces_unknown_resolved_retryable():
    """The operator-only state must never be written by any lifecycle helper or
    reconciliation path — a static guard over the module source. Only the SQL
    CHECK admits it (for a future operator action); the write itself is out of
    scope here."""
    import inspect

    src = inspect.getsource(lc)
    # It appears exactly once as a constant definition and never inside any SQL
    # UPDATE/INSERT statement string in this module.
    for line in src.splitlines():
        if "UPDATE tg_alert_log" in line or "INSERT INTO tg_alert_log" in line:
            assert "unknown_resolved_retryable" not in line, line


# --- transition helpers (lease-guarded, return success) --------------------


@pytest.mark.asyncio
async def test_mark_attempted_then_promote_under_lease(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    lease = lc.new_lease()
    rid = await _seed(
        db, "tok", "dispatch_pending", lease=lease, updated_at=_fresh_iso()
    )
    assert await lc.mark_attempted(db, rid, lease=lease) is True
    assert await _outcome(db, rid) == "dispatch_attempted"
    assert await lc.promote_sent(db, rid, lease=lease) is True
    assert await _outcome(db, rid) == "sent"
    # A second promote is a no-op (row already sent) → returns False.
    assert await lc.promote_sent(db, rid, lease=lease) is False
    assert await _outcome(db, rid) == "sent"
    await db.close()


@pytest.mark.asyncio
async def test_transition_requires_matching_lease(tmp_path):
    """A transition presented with the WRONG lease token is a no-op (returns
    False) — ownership is enforced by the CAS, not just by state."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    owner = lc.new_lease()
    rid = await _seed(
        db, "tok", "dispatch_pending", lease=owner, updated_at=_fresh_iso()
    )
    # Wrong lease cannot advance the row.
    assert await lc.mark_attempted(db, rid, lease="not-the-owner") is False
    assert await _outcome(db, rid) == "dispatch_pending"
    # Correct lease can.
    assert await lc.mark_attempted(db, rid, lease=owner) is True
    assert await _outcome(db, rid) == "dispatch_attempted"
    await db.close()


@pytest.mark.asyncio
async def test_promote_only_from_attempted(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    lease = lc.new_lease()
    rid = await _seed(
        db, "tok", "dispatch_pending", lease=lease, updated_at=_fresh_iso()
    )
    assert await lc.promote_sent(db, rid, lease=lease) is False  # not attempted yet
    assert await _outcome(db, rid) == "dispatch_pending"
    await db.close()


@pytest.mark.asyncio
async def test_demote_guarded_never_clobbers_sent(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    lease = lc.new_lease()
    rid = await _seed(db, "tok", "sent", lease=lease, updated_at=_fresh_iso())
    assert await lc.demote_failed(db, rid, lease=lease, detail="late failure") is False
    assert await _outcome(db, rid) == "sent"  # a delivered row is never demoted
    await db.close()


@pytest.mark.asyncio
async def test_mark_delivery_unknown_only_from_attempted(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    lp = lc.new_lease()
    rid_p = await _seed(db, "p", "dispatch_pending", lease=lp, updated_at=_fresh_iso())
    assert await lc.mark_delivery_unknown(db, rid_p, lease=lp, detail="x") is False
    assert await _outcome(db, rid_p) == "dispatch_pending"  # guarded — no-op
    la = lc.new_lease()
    rid_a = await _seed(
        db, "a", "dispatch_attempted", lease=la, updated_at=_fresh_iso()
    )
    assert await lc.mark_delivery_unknown(db, rid_a, lease=la, detail="x") is True
    assert await _outcome(db, rid_a) == "delivery_unknown_after_send"
    await db.close()


# --- P0-2: lease ownership vs reconciliation -------------------------------


@pytest.mark.asyncio
async def test_reconcile_uses_heartbeat_not_alerted_at(tmp_path):
    """A row whose original ``alerted_at`` is ancient but whose lease heartbeat
    (``dispatch_state_updated_at``) is FRESH must NOT be reclaimed — an active
    dispatch keeps its heartbeat fresh, so the sweep leaves it alone and the
    owner can still advance it."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    lease = lc.new_lease()
    # alerted_at ancient, heartbeat fresh → active dispatch.
    rid = await _seed(
        db,
        "tok",
        "dispatch_attempted",
        _old_iso(),
        lease=lease,
        updated_at=_fresh_iso(),
    )
    result = await lc.reconcile_stale(db, ST)
    assert result == {"pending_failed": 0, "attempted_unknown": 0}
    assert await _outcome(db, rid) == "dispatch_attempted"
    # Owner still owns it — promotion succeeds.
    assert await lc.promote_sent(db, rid, lease=lease) is True
    assert await _outcome(db, rid) == "sent"
    await db.close()


@pytest.mark.asyncio
async def test_reconcile_sweeps_expired_lease_and_blocks_stale_owner(tmp_path):
    """An expired-heartbeat lease is reclaimed (pending→failed, attempted→unknown),
    and the original owner presenting its now-stale lease can no longer promote
    (the CAS fails because the state moved)."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    lp = lc.new_lease()
    la = lc.new_lease()
    rp = await _seed(
        db, "old-p", "dispatch_pending", _old_iso(), lease=lp, updated_at=_old_iso()
    )
    ra = await _seed(
        db, "old-a", "dispatch_attempted", _old_iso(), lease=la, updated_at=_old_iso()
    )
    result = await lc.reconcile_stale(db, ST)
    assert result == {"pending_failed": 1, "attempted_unknown": 1}
    assert await _outcome(db, rp) == "dispatch_failed"
    assert await _outcome(db, ra) == "delivery_unknown_after_send"
    # The reclaimed row's original owner can no longer promote to sent.
    assert await lc.promote_sent(db, ra, lease=la) is False
    assert await _outcome(db, ra) == "delivery_unknown_after_send"
    await db.close()


@pytest.mark.asyncio
async def test_reconcile_concurrent_with_active_send_cannot_demote(tmp_path):
    """Event-ordered proof (P0-2): while a leased row is actively being sent (its
    heartbeat fresh from mark_attempted), a concurrent reconciliation cannot
    demote it, and the owner's promotion still succeeds."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    lease = lc.new_lease()
    # alerted_at ancient (would be swept by an alerted_at-based reconcile) but the
    # owner refreshes the heartbeat at mark_attempted just before "sending".
    rid = await _seed(
        db, "tok", "dispatch_pending", _old_iso(), lease=lease, updated_at=_old_iso()
    )

    sending = asyncio.Event()
    may_finish = asyncio.Event()

    async def owner():
        assert await lc.mark_attempted(db, rid, lease=lease) is True  # heartbeat fresh
        sending.set()
        await may_finish.wait()  # simulate an in-flight provider send
        return await lc.promote_sent(db, rid, lease=lease)

    task = asyncio.create_task(owner())
    await sending.wait()
    # Reconcile runs WHILE the send is in flight — must be a no-op (fresh heartbeat).
    result = await lc.reconcile_stale(db, ST)
    assert result == {"pending_failed": 0, "attempted_unknown": 0}
    assert await _outcome(db, rid) == "dispatch_attempted"  # NOT demoted
    may_finish.set()
    assert await task is True
    assert await _outcome(db, rid) == "sent"
    await db.close()


# --- reconciliation is signal-scoped ---------------------------------------


@pytest.mark.asyncio
async def test_reconcile_is_signal_type_scoped(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    other = await _seed(
        db,
        "other",
        "dispatch_pending",
        _old_iso(),
        signal_type="gainers_early",
        updated_at=_old_iso(),
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
        ("f", "unknown_resolved_retryable"),
    ]:
        await _seed(db, tok, oc)
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log WHERE outcome='sent'"
    )
    assert (await cur.fetchone())[0] == 1  # only the delivered row
    # cap-reservation counts sent + pending + attempted + unknown (NOT failed/retryable)
    cur = await db._conn.execute(
        f"SELECT COUNT(*) FROM tg_alert_log WHERE outcome IN {lc.cap_reserved_in_clause()}"
    )
    assert (await cur.fetchone())[0] == 4
    # dedup-reservation now also includes delivery_unknown (P0-1) → 4.
    cur = await db._conn.execute(
        f"SELECT COUNT(*) FROM tg_alert_log WHERE outcome IN {lc.dedup_reserved_in_clause()}"
    )
    assert (await cur.fetchone())[0] == 4
    await db.close()


# --- migration: legacy rows reportable + idempotent ------------------------


@pytest.mark.asyncio
async def test_migration_admits_new_states_and_keeps_legacy_rows(tmp_path):
    """The CHECK-widening migration admits every intent state + the operator-only
    state, preserves legacy rows verbatim (still reportable via outcome='sent'),
    and is idempotent."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    await _seed(db, "legacy", "sent")  # legacy delivered row
    for oc in (
        "dispatch_pending",
        "dispatch_attempted",
        "delivery_unknown_after_send",
        "unknown_resolved_retryable",
    ):
        await _seed(db, f"new-{oc}", oc)
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log WHERE token_id='legacy' AND outcome='sent'"
    )
    assert (await cur.fetchone())[0] == 1
    # re-running the migration is a no-op (idempotent) and preserves rows
    await db._migrate_tg_alert_log_dispatch_pending_outcome()
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_alert_log")
    assert (await cur.fetchone())[0] == 5
    await db.close()


@pytest.mark.asyncio
async def test_lease_columns_present_and_migration_recorded(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(tg_alert_log)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "dispatch_state_updated_at" in cols
    assert "dispatch_lease_token" in cols
    for name in (
        "bl_tg_alert_log_dispatch_pending_outcome",
        "bl_tg_alert_log_dispatch_lease_v1",
    ):
        cur = await db._conn.execute(
            "SELECT 1 FROM paper_migrations WHERE name=?", (name,)
        )
        assert (await cur.fetchone()) is not None, name
    await db.close()


# --- dedup race: two concurrent claims cannot both reserve -----------------


@pytest.mark.asyncio
async def test_dedup_race_only_one_claim_reserves(tmp_path, monkeypatch):
    """Two concurrent trade-surface dispatches for the SAME token cannot both
    reserve a slot: the dedup NOT EXISTS over reserved states serializes under
    the manager transaction so exactly one claims (pending), the other is
    blocked_dedup_24h."""
    from scout.trading.trade_surface_alerts import (
        TradeSurfaceAlertCandidate,
        _send_claimed_alert,
    )

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
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log WHERE token_id='racetok' AND outcome='sent'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


# --- P0-1: operator-only, audited resolution into unknown_resolved_retryable --


@pytest.mark.asyncio
async def test_resolve_unknown_is_operator_gated_and_audited(tmp_path):
    """The sole write site of unknown_resolved_retryable: requires operator +
    reason (authorization / who + why), CAS-transitions ONLY a delivery_unknown
    row, writes a durable audit record, and the resulting state is never counted
    as delivered."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    rid = await _seed(db, "unk", "delivery_unknown_after_send")

    # Authorization gate: operator + reason are both required.
    with pytest.raises(ValueError):
        await db.resolve_delivery_unknown_as_retryable(
            tg_alert_log_id=rid, operator="", reason="x"
        )
    with pytest.raises(ValueError):
        await db.resolve_delivery_unknown_as_retryable(
            tg_alert_log_id=rid, operator="alice", reason="  "
        )
    assert await _outcome(db, rid) == "delivery_unknown_after_send"  # unchanged

    out = await db.resolve_delivery_unknown_as_retryable(
        tg_alert_log_id=rid, operator="alice", reason="confirmed non-delivery in TG"
    )
    assert out["to_outcome"] == "unknown_resolved_retryable"
    assert await _outcome(db, rid) == "unknown_resolved_retryable"

    # Durable audit record captures ALL of: alert identity, prior state,
    # resolution result, operator identity, reason, timestamp.
    cur = await db._conn.execute(
        "SELECT tg_alert_log_id, from_outcome, to_outcome, operator, reason, "
        "resolved_at FROM tg_alert_unknown_resolutions WHERE tg_alert_log_id=?",
        (rid,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == rid  # alert identity
    assert (row[1], row[2]) == (
        "delivery_unknown_after_send",  # prior state
        "unknown_resolved_retryable",  # resolution result
    )
    assert row[3] == "alice"  # operator identity
    assert row[4] == "confirmed non-delivery in TG"  # reason
    assert row[5]  # resolved_at timestamp present

    # Never counts as delivered; freed from both reserved sets.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log WHERE id=? AND outcome='sent'", (rid,)
    )
    assert (await cur.fetchone())[0] == 0
    cur = await db._conn.execute(
        f"SELECT COUNT(*) FROM tg_alert_log WHERE id=? AND outcome IN "
        f"{lc.cap_reserved_in_clause()}",
        (rid,),
    )
    assert (await cur.fetchone())[0] == 0
    cur = await db._conn.execute(
        f"SELECT COUNT(*) FROM tg_alert_log WHERE id=? AND outcome IN "
        f"{lc.dedup_reserved_in_clause()}",
        (rid,),
    )
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_resolve_unknown_cas_only_from_delivery_unknown(tmp_path):
    """CAS guard: a row that is NOT delivery_unknown_after_send cannot be resolved
    (no silent conversion, no double-resolution)."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    rid_sent = await _seed(db, "s", "sent")
    with pytest.raises(KeyError):
        await db.resolve_delivery_unknown_as_retryable(
            tg_alert_log_id=rid_sent, operator="alice", reason="x"
        )
    assert await _outcome(db, rid_sent) == "sent"  # untouched
    # Double-resolution: second call finds no delivery_unknown row → raises.
    rid = await _seed(db, "u", "delivery_unknown_after_send")
    await db.resolve_delivery_unknown_as_retryable(
        tg_alert_log_id=rid, operator="alice", reason="ok"
    )
    with pytest.raises(KeyError):
        await db.resolve_delivery_unknown_as_retryable(
            tg_alert_log_id=rid, operator="alice", reason="again"
        )
    await db.close()


# --- P0-1/P0-2 static proofs: sole writer + timestamp write-locality ---------

import ast  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402

_SCOUT = pathlib.Path(__file__).resolve().parents[1] / "scout"


def test_unknown_resolved_retryable_has_a_single_write_site():
    """Across scout/, the ONLY code that writes ``unknown_resolved_retryable``
    (INSERT/UPDATE SET outcome=...) is resolve_delivery_unknown_as_retryable. No
    automatic path produces it."""
    write_sites = []
    for path in _SCOUT.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            fsrc = ast.get_source_segment(src, node) or ""
            # a SQL statement string that assigns the state to outcome
            if re.search(
                r"outcome\s*=\s*'unknown_resolved_retryable'", fsrc
            ) or re.search(
                r"'unknown_resolved_retryable'[^)]*\)\s*(?:VALUES|SELECT)", fsrc
            ):
                write_sites.append((path.name, node.name))
    assert write_sites == [
        ("db.py", "resolve_delivery_unknown_as_retryable")
    ], write_sites


def test_only_transition_writes_touch_dispatch_state_updated_at():
    """``dispatch_state_updated_at`` (the lease heartbeat) is written ONLY by the
    lifecycle transitions/reconcile, the two dispatch claims, and the db.py
    operator-resolve + migration — never by an unrelated reader (so a reader can
    never accidentally extend a lease)."""
    allowed = {
        "trading/alert_dispatch_lifecycle.py",
        "trading/trade_surface_alerts.py",
        "trading/tg_alert_dispatch.py",
        "db.py",
    }
    writers = set()
    for path in _SCOUT.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(_SCOUT).as_posix()
        # a write = SET dispatch_state_updated_at OR an INSERT column list naming it
        if re.search(r"SET\s+outcome[^\n]*dispatch_state_updated_at", src) or re.search(
            r"dispatch_state_updated_at\s*=\s*\?", src
        ):
            writers.add(rel)
        elif "dispatch_state_updated_at" in src and re.search(
            r"INSERT INTO tg_alert_log", src
        ):
            writers.add(rel)
    assert (
        writers <= allowed
    ), f"unexpected writer of the lease heartbeat: {writers - allowed}"


@pytest.mark.asyncio
async def test_lease_migration_idempotent_and_index_present(tmp_path):
    """Migration 20260729 is idempotent on re-run (upgrade + restart safety) and
    creates the reconcile-path index; legacy rows are preserved."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    # a pre-existing row survives the (already-applied) additive migration
    legacy = await _seed(db, "legacy", "sent")
    # re-run the lease migration — must be a no-op, rows preserved, columns intact
    await db._migrate_tg_alert_log_dispatch_lease_v1()
    assert await _outcome(db, legacy) == "sent"
    cur = await db._conn.execute("PRAGMA table_info(tg_alert_log)")
    cols = {row[1] for row in await cur.fetchall()}
    assert {"dispatch_state_updated_at", "dispatch_lease_token"} <= cols
    # reconcile-path index exists
    cur = await db._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_tg_alert_log_reconcile'"
    )
    assert (await cur.fetchone()) is not None
    # audit-table migration recorded + table present
    cur = await db._conn.execute(
        "SELECT 1 FROM paper_migrations WHERE name=?",
        ("bl_tg_alert_unknown_resolution_audit_v1",),
    )
    assert (await cur.fetchone()) is not None
    cur = await db._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='tg_alert_unknown_resolutions'"
    )
    assert (await cur.fetchone()) is not None
    await db.close()


def test_lease_migration_registered_before_audit_migration():
    """Migration ORDER: 20260729 (lease fields + reconcile index) is registered
    BEFORE 20260730 (audit table) in the initialize() migration battery."""
    import inspect

    src = inspect.getsource(Database.initialize)
    i_lease = src.index("_migrate_tg_alert_log_dispatch_lease_v1()")
    i_audit = src.index("_migrate_tg_alert_unknown_resolution_audit_v1()")
    assert i_lease < i_audit, "lease migration must register before the audit migration"


@pytest.mark.asyncio
async def test_both_new_migrations_idempotent_and_preserve_legacy(tmp_path):
    """Upgrade + restart safety: re-running BOTH new migrations (20260729 +
    20260730) preserves legacy rows and creates no duplicate audit rows."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    legacy = await _seed(db, "legacy", "sent")
    rid = await _seed(db, "unk", "delivery_unknown_after_send")
    await db.resolve_delivery_unknown_as_retryable(
        tg_alert_log_id=rid, operator="alice", reason="ok"
    )
    # re-run both migrations — idempotent no-ops
    await db._migrate_tg_alert_log_dispatch_lease_v1()
    await db._migrate_tg_alert_unknown_resolution_audit_v1()
    assert await _outcome(db, legacy) == "sent"
    assert await _outcome(db, rid) == "unknown_resolved_retryable"
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_unknown_resolutions WHERE tg_alert_log_id=?",
        (rid,),
    )
    assert (await cur.fetchone())[0] == 1  # no duplicate audit row
    await db.close()


@pytest.mark.asyncio
async def test_resolve_cas_failure_writes_no_audit_record(tmp_path):
    """CAS failure → NO state change AND NO audit record: the audit row is written
    ONLY on an applied CAS (inside the same transaction, after the rowcount check),
    so a failed resolution can never leave a record claiming success."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    rid = await _seed(db, "s", "sent")  # not a delivery_unknown row → CAS fails
    with pytest.raises(KeyError):
        await db.resolve_delivery_unknown_as_retryable(
            tg_alert_log_id=rid, operator="alice", reason="x"
        )
    assert await _outcome(db, rid) == "sent"  # no state change
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_alert_unknown_resolutions")
    assert (await cur.fetchone())[0] == 0  # no audit record claiming success
    await db.close()


@pytest.mark.asyncio
async def test_resolving_unknown_frees_dedup_permitting_one_redispatch(
    tmp_path, monkeypatch
):
    """unknown_resolved_retryable grants NO delivery status and NO systemic retry:
    it simply removes the dedup block the delivery_unknown imposed, so the next
    pipeline fire for the token can create ONE fresh dispatch. Before resolution
    the token is deduped; after resolution a new claim proceeds. Nothing automatic
    re-dispatches — the dispatch path 'consumes' it only via its ABSENCE from the
    dedup reserved set; no code reads the state to trigger anything."""
    from scout.config import Settings
    from scout.trading.trade_surface_alerts import (
        TradeSurfaceAlertCandidate,
        _send_claimed_alert,
    )

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
    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )

    def _cand():
        return TradeSurfaceAlertCandidate(
            token_id="rtok",
            symbol="R",
            name="R",
            surface="now_tradable",
            verdict="candidate_review",
            market_cap=1_000_000,
            current_price=1.0,
            move_pct=1.0,
            source_corpus="paper",
            surfaces=("now_tradable",),
            reasons=(),
        )

    # A delivery_unknown row reserves dedup → a new claim is blocked.
    await _seed(db, "rtok", "delivery_unknown_after_send")
    assert (
        await _send_claimed_alert(db, s, object(), candidate=_cand(), window_hours=24)
        == "blocked_dedup_24h"
    )
    # Operator resolves it → frees the dedup slot.
    cur = await db._conn.execute(
        "SELECT id FROM tg_alert_log WHERE token_id='rtok' "
        "AND outcome='delivery_unknown_after_send'"
    )
    rid = (await cur.fetchone())[0]
    await db.resolve_delivery_unknown_as_retryable(
        tg_alert_log_id=rid, operator="alice", reason="confirmed undelivered"
    )
    # Now exactly one fresh re-dispatch is permitted (the normal pipeline fire).
    assert (
        await _send_claimed_alert(db, s, object(), candidate=_cand(), window_hours=24)
        == "sent"
    )
    await db.close()


@pytest.mark.asyncio
async def test_reconcile_never_touches_resolved_or_unknown_terminal(tmp_path):
    """Consumer sweep (reconciliation): the reconcile sweep targets ONLY
    dispatch_pending/dispatch_attempted — a terminal delivery_unknown or an
    operator-resolved unknown_resolved_retryable row is never swept, even with an
    expired heartbeat."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    rr = await _seed(
        db, "rr", "unknown_resolved_retryable", _old_iso(), updated_at=_old_iso()
    )
    ru = await _seed(
        db, "ru", "delivery_unknown_after_send", _old_iso(), updated_at=_old_iso()
    )
    res = await lc.reconcile_stale(db, ST)
    assert res == {"pending_failed": 0, "attempted_unknown": 0}
    assert await _outcome(db, rr) == "unknown_resolved_retryable"
    assert await _outcome(db, ru) == "delivery_unknown_after_send"
    await db.close()


# --- B2: finalize_confirmed_sent + ownership-safe unknown->sent recovery ------


@pytest.mark.asyncio
async def test_finalize_promotes_attempted_to_sent(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.initialize()
    lease = lc.new_lease()
    rid = await _seed(
        db, "t", "dispatch_attempted", lease=lease, updated_at=_fresh_iso()
    )
    assert await lc.finalize_confirmed_sent(db, rid, lease=lease) is True
    assert await _outcome(db, rid) == "sent"
    await db.close()


@pytest.mark.asyncio
async def test_finalize_recovers_unknown_to_sent_under_lease(tmp_path):
    """A confirmed acceptance whose row was reconciled to delivery_unknown is
    recovered unknown->sent under the owner lease, with a durable audit note."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    lease = lc.new_lease()
    rid = await _seed(
        db, "t", "delivery_unknown_after_send", lease=lease, updated_at=_fresh_iso()
    )
    assert await lc.finalize_confirmed_sent(db, rid, lease=lease) is True
    assert await _outcome(db, rid) == "sent"
    cur = await db._conn.execute("SELECT detail FROM tg_alert_log WHERE id=?", (rid,))
    assert "recovered" in ((await cur.fetchone())[0] or "")
    await db.close()


@pytest.mark.asyncio
async def test_recover_is_lease_guarded(tmp_path):
    """The unknown->sent recovery only applies under the owner lease — a wrong
    lease cannot resurrect a delivery_unknown row to sent."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    owner = lc.new_lease()
    rid = await _seed(
        db, "t", "delivery_unknown_after_send", lease=owner, updated_at=_fresh_iso()
    )
    assert (
        await lc.recover_sent_after_confirmed_acceptance(db, rid, lease="not-owner")
        is False
    )
    assert await _outcome(db, rid) == "delivery_unknown_after_send"
    assert (
        await lc.recover_sent_after_confirmed_acceptance(db, rid, lease=owner) is True
    )
    assert await _outcome(db, rid) == "sent"
    await db.close()


# --- B2 proof-2: finalize vs reconcile contention (no torn outcome) ----------


@pytest.mark.asyncio
async def test_b2_finalize_vs_reconcile_race_is_consistent(tmp_path):
    """Finalization-margin proof (design a): finalize's transition is itself a
    lease-guarded CAS, and both finalize and reconcile serialize through the shared
    _txn_lock (single connection). So no matter who wins the race, the outcome is a
    durable, internally consistent 'sent' — a reconcile that wins simply routes the
    finalizer into the ownership-safe unknown->sent recovery. Neither DB-lock
    contention nor scheduler order can tear the outcome."""
    for order in ("finalize_first", "reconcile_first"):
        db = Database(tmp_path / f"race_{order}.db")
        await db.initialize()
        lease = lc.new_lease()
        # heartbeat stale so reconcile WOULD sweep the attempted row if it wins.
        rid = await _seed(
            db,
            "t",
            "dispatch_attempted",
            _old_iso(),
            lease=lease,
            updated_at=_old_iso(),
        )
        if order == "finalize_first":
            f = asyncio.create_task(lc.finalize_confirmed_sent(db, rid, lease=lease))
            r = asyncio.create_task(lc.reconcile_stale(db, ST))
        else:
            r = asyncio.create_task(lc.reconcile_stale(db, ST))
            f = asyncio.create_task(lc.finalize_confirmed_sent(db, rid, lease=lease))
        finalized, _reconciled = await asyncio.gather(f, r)
        # Finalize ALWAYS ends in durable sent (promote-or-recover); DB agrees.
        assert finalized is True, order
        assert await _outcome(db, rid) == "sent", order
        await db.close()


# --- B2 proof-3: attempt-specific recovery (blocked on ownership change) ------


@pytest.mark.asyncio
async def test_recover_blocked_after_operator_resolution(tmp_path):
    """If an operator already resolved the row (unknown_resolved_retryable), the
    unknown->sent recovery fails SAFELY — it never overwrites the operator decision
    and never claims sent."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    lease = lc.new_lease()
    rid = await _seed(
        db, "t", "unknown_resolved_retryable", lease=lease, updated_at=_fresh_iso()
    )
    assert (
        await lc.recover_sent_after_confirmed_acceptance(db, rid, lease=lease) is False
    )
    assert await lc.finalize_confirmed_sent(db, rid, lease=lease) is False
    assert await _outcome(db, rid) == "unknown_resolved_retryable"  # preserved
    await db.close()


@pytest.mark.asyncio
async def test_recover_blocked_after_new_attempt_token(tmp_path):
    """If a NEWER attempt rotated the row's lease token, the OLD attempt's recovery
    fails SAFELY (attempt-specific) — it cannot promote a generic unknown row that
    now belongs to a different attempt; only the current owner can."""
    db = Database(tmp_path / "d.db")
    await db.initialize()
    old = lc.new_lease()
    new = lc.new_lease()
    rid = await _seed(
        db, "t", "delivery_unknown_after_send", lease=old, updated_at=_fresh_iso()
    )
    await db.execute_write(
        "UPDATE tg_alert_log SET dispatch_lease_token=? WHERE id=?", (new, rid)
    )
    assert await lc.finalize_confirmed_sent(db, rid, lease=old) is False
    assert await _outcome(db, rid) == "delivery_unknown_after_send"
    # the current owner (new token) can recover
    assert await lc.recover_sent_after_confirmed_acceptance(db, rid, lease=new) is True
    assert await _outcome(db, rid) == "sent"
    await db.close()
