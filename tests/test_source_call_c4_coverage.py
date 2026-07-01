"""C4 coverage metrics (design #392 §4.4): X-wide price/forward coverage +
unresolved-by-reason, computed from EXISTING source_calls fields (no new column).
DB-only — runs on Windows.
"""

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.source_quality.ledger import compute_x_price_coverage

NOW = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "c4.db")
    await d.initialize()
    yield d
    await d.close()


def _iso(dt):
    return dt.isoformat()


async def _insert_x_call(
    conn,
    *,
    event_id,
    resolved_state,
    outcome_status,
    call_ts,
    source_type="x",
    token_id=None,
    contract_address=None,
    symbol=None,
    price_at_call=None,
    forward_24h_pct=None,
    max_favorable_pct_24h=None,
    call_kind="ca_call",
    missing_fields="[]",
    cluster_kind="contract",
):
    await conn.execute(
        "INSERT INTO source_calls "
        "(source_type, source_id, source_event_id, token_id, symbol, "
        " contract_address, chain, call_ts, call_kind, cluster_identity, "
        " cluster_identity_kind, duplicate_cluster_key, resolved_state, "
        " price_at_call, forward_24h_pct, max_favorable_pct_24h, "
        " outcome_status, missing_fields) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            source_type,
            "kol_x",
            event_id,
            token_id,
            symbol,
            contract_address,
            "base",
            call_ts,
            call_kind,
            "cid",
            cluster_kind,
            f"dck-{event_id}",
            resolved_state,
            price_at_call,
            forward_24h_pct,
            max_favorable_pct_24h,
            outcome_status,
            missing_fields,
        ),
    )
    await conn.commit()


async def test_x_price_coverage_aggregate_counts(db):
    # resolved(token_id), fully priced, old
    await _insert_x_call(
        db._conn,
        event_id="r1",
        resolved_state="resolved",
        token_id="c1",
        outcome_status="complete",
        call_ts=_iso(NOW - timedelta(hours=30)),
        price_at_call=1.0,
        forward_24h_pct=10.0,
        max_favorable_pct_24h=20.0,
        call_kind="cashtag_only",
        cluster_kind="token_id",
    )
    # eligible_contract, price_at_call only, recent (pending forward)
    await _insert_x_call(
        db._conn,
        event_id="c1",
        resolved_state="eligible_contract",
        contract_address="0xa",
        outcome_status="partial",
        call_ts=_iso(NOW - timedelta(hours=1)),
        price_at_call=2.0,
        missing_fields='[{"field":"forward_24h_pct","reason":"pending_window"}]',
    )
    # eligible_contract, matured >28h with NO price and NO forward -> matured_all_null
    await _insert_x_call(
        db._conn,
        event_id="c2",
        resolved_state="eligible_contract",
        contract_address="0xb",
        outcome_status="unresolvable",
        call_ts=_iso(NOW - timedelta(hours=30)),
        missing_fields='[{"field":"forward_24h_pct","reason":"no_time_series"}]',
    )
    # cashtag-only unresolved
    await _insert_x_call(
        db._conn,
        event_id="u1",
        resolved_state="unresolved",
        symbol="MEME",
        outcome_status="unresolvable",
        call_ts=_iso(NOW - timedelta(hours=30)),
        call_kind="cashtag_only",
        cluster_kind="symbol",
        missing_fields='[{"field":"forward_24h_pct","reason":"no_time_series"}]',
    )

    cov = await compute_x_price_coverage(db._conn, now=NOW)

    assert cov.total_x_calls == 4
    assert cov.resolved_token_id == 1
    assert cov.eligible_contract == 2
    assert cov.unresolved == 1
    assert cov.with_price_at_call == 2  # r1, c1
    assert cov.with_forward_24h == 1  # r1
    assert cov.with_max_favorable_24h == 1  # r1
    assert (
        cov.matured_all_null == 1
    )  # c2 only (resolved-identity, old, null price+forward)
    assert cov.outcome_status_counts == {"complete": 1, "partial": 1, "unresolvable": 2}
    assert cov.call_kind_counts == {"cashtag_only": 2, "ca_call": 2}
    assert cov.unresolved_reason_counts == {"pending_window": 1, "no_time_series": 2}


async def test_x_price_coverage_excludes_non_x(db):
    await _insert_x_call(
        db._conn,
        event_id="tg1",
        resolved_state="resolved",
        source_type="tg",
        token_id="c9",
        outcome_status="complete",
        call_ts=_iso(NOW - timedelta(hours=30)),
        price_at_call=1.0,
    )
    cov = await compute_x_price_coverage(db._conn, now=NOW)
    assert cov.total_x_calls == 0  # tg rows excluded


async def test_x_price_coverage_empty_db(db):
    cov = await compute_x_price_coverage(db._conn, now=NOW)
    assert cov.total_x_calls == 0
    assert cov.matured_all_null == 0
    assert cov.outcome_status_counts == {}
    assert cov.unresolved_reason_counts == {}
