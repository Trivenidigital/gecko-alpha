"""C3 pricing hookup (design #392): price eligible_contract source_calls from the
forward-only source_call_price_snapshots table, with a just-after-call anchor.

Lead section = token_id CHARACTERIZATION tests that must stay green (lock the
existing token_id path). Then contract-path tests (RED until the hookup lands).
All DB-only — runs on Windows.
"""

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.source_quality.ledger import (
    _compute_outcome,
    _fetch_snapshot_rows,
    refresh_source_call_outcomes,
)

T = datetime(2026, 5, 20, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "c3.db")
    await d.initialize()
    yield d
    await d.close()


def _iso(dt):
    return dt.isoformat()


async def _fetchone(conn, sql, params=()):
    cur = await conn.execute(sql, params)
    return await cur.fetchone()


async def _insert_source_call(
    conn,
    *,
    event_id,
    resolved_state,
    call_ts,
    source_type="x",
    contract_address=None,
    chain=None,
    token_id=None,
    symbol=None,
    outcome_status="pending",
    call_kind="ca_call",
    cluster_kind="contract",
):
    await conn.execute(
        "INSERT INTO source_calls "
        "(source_type, source_id, source_event_id, token_id, symbol, "
        " contract_address, chain, call_ts, call_kind, cluster_identity, "
        " cluster_identity_kind, duplicate_cluster_key, resolved_state, "
        " outcome_status, missing_fields) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            source_type,
            "kol_x",
            event_id,
            token_id,
            symbol,
            contract_address,
            chain,
            call_ts,
            call_kind,
            "cid",
            cluster_kind,
            f"dck-{event_id}",
            resolved_state,
            outcome_status,
            "[]",
        ),
    )
    await conn.commit()


async def _insert_gainer_price(conn, coin_id, price, snapshot_at):
    await conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, market_cap, volume_24h, price_change_24h, "
        "price_at_snapshot, snapshot_at) "
        "VALUES (?, 'TOK', 'Token', 10000000, 100000, 0, ?, ?)",
        (coin_id, price, snapshot_at),
    )
    await conn.commit()


async def _insert_price_snapshot(
    conn,
    *,
    identity_key,
    price,
    snapshot_at,
    identity_kind="contract",
    chain="base",
    source="gt",
):
    await conn.execute(
        "INSERT INTO source_call_price_snapshots "
        "(identity_key, identity_kind, chain, price, snapshot_at, source) "
        "VALUES (?,?,?,?,?,?)",
        (identity_key, identity_kind, chain, price, snapshot_at, source),
    )
    await conn.commit()


# ==========================================================================
# CHARACTERIZATION — token_id path (must be GREEN before and after C3)
# ==========================================================================


async def test_c3_char_token_id_at_or_before_anchor_unchanged(db):
    # The common token_id case: an at-or-before gainers snapshot anchors
    # price_at_call; a forward snapshot yields forward_30m_pct. Locks the
    # selection + non-negative age. Reads gainers/losers, NOT the new table.
    await _insert_source_call(
        db._conn,
        event_id="tok",
        resolved_state="resolved",
        token_id="coin-x",
        call_ts=_iso(T),
        cluster_kind="token_id",
        call_kind="first_mention",
    )
    await _insert_gainer_price(db._conn, "coin-x", 1.0, _iso(T - timedelta(minutes=1)))
    await _insert_gainer_price(db._conn, "coin-x", 1.5, _iso(T + timedelta(minutes=35)))

    stats = await refresh_source_call_outcomes(db._conn, now=T + timedelta(minutes=50))

    row = await _fetchone(
        db._conn,
        "SELECT price_at_call, forward_30m_pct, price_age_sec, resolved_state "
        "FROM source_calls WHERE source_event_id='tok'",
    )
    assert row["price_at_call"] == 1.0  # at-or-before snapshot
    assert row["forward_30m_pct"] == 50.0
    assert row["price_age_sec"] == 60  # (call_ts - snapshot_at), non-negative
    assert row["resolved_state"] == "resolved"  # token_id branch never rewrites this
    assert stats["updated"] == 1


async def test_c3_char_token_id_ignores_contract_snapshots(db):
    # A contract snapshot sharing a coincidental key must NOT leak into the
    # token_id pricing path (token_id reads only gainers/losers).
    await _insert_source_call(
        db._conn,
        event_id="tok2",
        resolved_state="resolved",
        token_id="coin-y",
        call_ts=_iso(T),
        cluster_kind="token_id",
    )
    await _insert_price_snapshot(
        db._conn,
        identity_key="coin-y",
        price=9.9,
        snapshot_at=_iso(T),
        identity_kind="contract",
    )
    stats = await refresh_source_call_outcomes(db._conn, now=T + timedelta(minutes=50))
    row = await _fetchone(
        db._conn,
        "SELECT price_at_call FROM source_calls WHERE source_event_id='tok2'",
    )
    assert row["price_at_call"] is None  # no gainers/losers snapshot -> no price
    assert stats["updated"] == 1


# ==========================================================================
# CONTRACT PATH — new C3 behavior (RED until the hookup lands)
# ==========================================================================


async def test_c3_generalized_fetch_reads_contract_snapshots(db):
    await _insert_price_snapshot(
        db._conn, identity_key="base|0xabc", price=2.0, snapshot_at=_iso(T)
    )
    rows = await _fetch_snapshot_rows(db._conn, "base|0xabc", "contract")
    assert len(rows) == 1
    assert rows[0]["price"] == 2.0
    assert rows[0]["source"] == "gt"


async def test_c3_contract_priced_from_forward_snapshots(db):
    await _insert_source_call(
        db._conn,
        event_id="ca1",
        resolved_state="eligible_contract",
        contract_address="0xAbC",
        chain="base",
        call_ts=_iso(T),
    )
    # just-after anchor (T+2min, within 900s) + a 30m-forward snapshot (T+35min).
    await _insert_price_snapshot(
        db._conn,
        identity_key="base|0xabc",
        price=1.0,
        snapshot_at=_iso(T + timedelta(minutes=2)),
    )
    await _insert_price_snapshot(
        db._conn,
        identity_key="base|0xabc",
        price=1.5,
        snapshot_at=_iso(T + timedelta(minutes=35)),
    )

    stats = await refresh_source_call_outcomes(db._conn, now=T + timedelta(minutes=50))

    row = await _fetchone(
        db._conn,
        "SELECT price_at_call, forward_30m_pct, price_age_sec, resolved_state "
        "FROM source_calls WHERE source_event_id='ca1'",
    )
    assert row["price_at_call"] == 1.0  # just-after anchor within tolerance
    assert row["forward_30m_pct"] == 50.0
    assert row["price_age_sec"] == 120  # abs(anchor - call), positive
    assert row["resolved_state"] == "eligible_contract"
    assert stats["eligible_contract"] == 1
    assert stats["updated"] == 0


async def test_c3_just_after_anchor_only_within_tolerance(db):
    # A snapshot beyond the tolerance window is NOT accepted as the anchor.
    await _insert_source_call(
        db._conn,
        event_id="ca2",
        resolved_state="eligible_contract",
        contract_address="0xDef",
        chain="base",
        call_ts=_iso(T),
    )
    await _insert_price_snapshot(
        db._conn,
        identity_key="base|0xdef",
        price=1.0,
        snapshot_at=_iso(T + timedelta(seconds=1200)),  # 20min > 900s tolerance
    )
    await refresh_source_call_outcomes(db._conn, now=T + timedelta(hours=30))
    row = await _fetchone(
        db._conn,
        "SELECT price_at_call FROM source_calls WHERE source_event_id='ca2'",
    )
    assert row["price_at_call"] is None  # beyond tolerance -> no anchor


async def test_c3_compute_outcome_just_after_anchor_age_absolute():
    # Direct _compute_outcome: forward-only series, anchor age is abs (positive).
    price_rows = [
        {"price": 1.0, "snapshot_at": T + timedelta(seconds=300), "source": "gt"},
        {"price": 2.0, "snapshot_at": T + timedelta(minutes=35), "source": "gt"},
    ]
    out = _compute_outcome(
        call_ts=T, now=T + timedelta(minutes=50), price_rows=price_rows
    )
    assert out["price_at_call"] == 1.0
    assert out["price_age_sec"] == 300  # abs(+300s), not negative
    assert out["forward_30m_pct"] == 100.0


async def test_c3_contract_no_snapshots_priced_as_unresolvable(db):
    # eligible_contract row, no snapshots, matured past 24h -> priced (not skipped)
    # with no price. resolved_state stays eligible_contract; perf fields stay NULL.
    await _insert_source_call(
        db._conn,
        event_id="ca3",
        resolved_state="eligible_contract",
        contract_address="0x000",
        chain="base",
        call_ts=_iso(T),
    )
    stats = await refresh_source_call_outcomes(db._conn, now=T + timedelta(hours=30))
    row = await _fetchone(
        db._conn,
        "SELECT price_at_call, forward_24h_pct, outcome_status, resolved_state "
        "FROM source_calls WHERE source_event_id='ca3'",
    )
    assert row["price_at_call"] is None
    assert row["forward_24h_pct"] is None
    assert row["outcome_status"] == "unresolvable"
    assert row["resolved_state"] == "eligible_contract"
    assert stats["eligible_contract"] == 1
