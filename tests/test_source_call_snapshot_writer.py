"""Tests for the forward-only CA price-snapshot writer (design #392 C2).

The writer takes the C0 resolver/fetcher as injected async callables, so these
tests run without importing aiohttp (fakes below). Covers acceptance criteria
2-10: selection, dedup, provider-error observability, missing-pool vs empty-OHLCV
separation, and the guarantee that no source_calls performance fields are written.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scout.db import Database
from scout.exceptions import PriceProviderError
from scout.source_quality.snapshot_writer import write_price_snapshots

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "scps_writer.db")
    await d.initialize()
    yield d
    await d.close()


def _pool(network="solana", pool_address="POOLX", source="gt"):
    return SimpleNamespace(
        network=network,
        pool_address=pool_address,
        base_token_address=None,
        reserve_usd=15000.0,
        source=source,
    )


def _candle(close=1.23, source="gt"):
    return SimpleNamespace(
        timestamp=1_700_000_000,
        open=1.0,
        high=2.0,
        low=0.5,
        close=close,
        volume_usd=100.0,
        source=source,
    )


class RecordingResolver:
    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    async def __call__(self, *, chain, contract_address):
        self.calls.append({"chain": chain, "contract_address": contract_address})
        if self.raises is not None:
            raise self.raises
        return self.result


class RecordingFetcher:
    def __init__(self, result=None, raises=None):
        self.result = [] if result is None else result
        self.raises = raises
        self.calls = []

    async def __call__(self, *, network, pool_address):
        self.calls.append({"network": network, "pool_address": pool_address})
        if self.raises is not None:
            raise self.raises
        return self.result


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
            "contract",
            f"dck-{event_id}",
            resolved_state,
            outcome_status,
            "[]",
        ),
    )
    await conn.commit()


async def _snapshots(conn):
    cur = await conn.execute(
        "SELECT identity_key, identity_kind, chain, price, snapshot_at, source "
        "FROM source_call_price_snapshots"
    )
    return [dict(r) for r in await cur.fetchall()]


def _iso(dt):
    return dt.isoformat()


# --------------------------------------------------------------------------
# Happy path — criteria 4, 5
# --------------------------------------------------------------------------


async def test_writer_writes_gt_snapshot_for_eligible_contract(db):
    await _insert_source_call(
        db._conn,
        event_id="e1",
        resolved_state="eligible_contract",
        contract_address="So1Address",  # mixed case (Solana is case-sensitive)
        chain="solana",
        call_ts=_iso(NOW - timedelta(hours=1)),
    )
    resolver = RecordingResolver(result=_pool(network="solana", pool_address="POOLX"))
    fetcher = RecordingFetcher(result=[_candle(close=1.23)])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    rows = await _snapshots(db._conn)
    assert len(rows) == 1
    snap = rows[0]
    assert snap["identity_kind"] == "contract"
    assert (
        snap["identity_key"] == "solana|so1address"
    )  # lowercased per _priceable_identity
    assert snap["chain"] == "solana"
    assert snap["price"] == 1.23
    assert snap["source"] == "gt"
    assert snap["snapshot_at"] == NOW.isoformat()
    assert stats["snapshots_written"] == 1
    assert stats["identities_seen"] == 1
    assert stats["provider_errors"] == 0
    # criterion 4: C0 called with ORIGINAL-case CA (not the lowercased key).
    assert resolver.calls == [{"chain": "solana", "contract_address": "So1Address"}]
    assert fetcher.calls == [{"network": "solana", "pool_address": "POOLX"}]


# --------------------------------------------------------------------------
# Dedup — criterion 3
# --------------------------------------------------------------------------


async def test_writer_dedupes_same_identity(db):
    for ev in ("e1", "e2"):
        await _insert_source_call(
            db._conn,
            event_id=ev,
            resolved_state="eligible_contract",
            contract_address="0xAbC",
            chain="base",
            call_ts=_iso(NOW - timedelta(minutes=30)),
        )
    resolver = RecordingResolver(result=_pool(network="base", pool_address="P"))
    fetcher = RecordingFetcher(result=[_candle(close=2.0)])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert len(resolver.calls) == 1  # fetched once for the shared identity
    assert stats["identities_seen"] == 1
    assert stats["snapshots_written"] == 1
    assert len(await _snapshots(db._conn)) == 1


# --------------------------------------------------------------------------
# Provider errors observable, never faked — criterion 6
# --------------------------------------------------------------------------


async def test_writer_fetch_provider_error_is_observed_no_row(db):
    await _insert_source_call(
        db._conn,
        event_id="e1",
        resolved_state="eligible_contract",
        contract_address="0xabc",
        chain="base",
        call_ts=_iso(NOW - timedelta(hours=1)),
    )
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(raises=PriceProviderError("gt", "boom"))

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert stats["provider_errors"] == 1
    assert stats["snapshots_written"] == 0
    assert await _snapshots(db._conn) == []  # no fake price row


async def test_writer_resolve_provider_error_is_observed(db):
    await _insert_source_call(
        db._conn,
        event_id="e1",
        resolved_state="eligible_contract",
        contract_address="0xabc",
        chain="base",
        call_ts=_iso(NOW - timedelta(hours=1)),
    )
    resolver = RecordingResolver(raises=PriceProviderError("gt", "pool lookup failed"))
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert stats["provider_errors"] == 1
    assert stats["snapshots_written"] == 0
    assert fetcher.calls == []  # short-circuited before fetch


# --------------------------------------------------------------------------
# Missing pool / empty OHLCV counted SEPARATELY from provider error — criterion 7
# --------------------------------------------------------------------------


async def test_writer_missing_pool_counted_separately(db):
    await _insert_source_call(
        db._conn,
        event_id="e1",
        resolved_state="eligible_contract",
        contract_address="0xabc",
        chain="base",
        call_ts=_iso(NOW - timedelta(hours=1)),
    )
    resolver = RecordingResolver(result=None)  # pool not found
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert stats["pools_unresolved"] == 1
    assert stats["provider_errors"] == 0
    assert stats["snapshots_written"] == 0
    assert fetcher.calls == []


async def test_writer_empty_ohlcv_counted_separately(db):
    await _insert_source_call(
        db._conn,
        event_id="e1",
        resolved_state="eligible_contract",
        contract_address="0xabc",
        chain="base",
        call_ts=_iso(NOW - timedelta(hours=1)),
    )
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[])  # pool exists, no candles

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert stats["empty_ohlcv"] == 1
    assert stats["provider_errors"] == 0
    assert stats["snapshots_written"] == 0
    assert await _snapshots(db._conn) == []


# --------------------------------------------------------------------------
# Selection guards — criteria 2, 8, 9
# --------------------------------------------------------------------------


async def test_writer_ignores_cashtag_only_rows(db):
    await _insert_source_call(
        db._conn,
        event_id="e1",
        resolved_state="unresolved",
        contract_address=None,
        symbol="MEME",
        call_ts=_iso(NOW - timedelta(hours=1)),
        call_kind="cashtag_only",
    )
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert resolver.calls == []
    assert stats["identities_seen"] == 0
    assert stats["snapshots_written"] == 0


async def test_writer_ignores_token_id_rows(db):
    await _insert_source_call(
        db._conn,
        event_id="e1",
        resolved_state="resolved",
        token_id="coin-x",
        contract_address="0xabc",
        chain="base",
        call_ts=_iso(NOW - timedelta(hours=1)),
    )
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert resolver.calls == []  # token_id priced path untouched
    assert stats["snapshots_written"] == 0


async def test_writer_ignores_rows_outside_horizon(db):
    await _insert_source_call(
        db._conn,
        event_id="e1",
        resolved_state="eligible_contract",
        contract_address="0xabc",
        chain="base",
        call_ts=_iso(NOW - timedelta(hours=40)),  # beyond 28h horizon
    )
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert resolver.calls == []
    assert stats["identities_seen"] == 0


async def test_writer_ignores_completed_outcome(db):
    await _insert_source_call(
        db._conn,
        event_id="e1",
        resolved_state="eligible_contract",
        contract_address="0xabc",
        chain="base",
        call_ts=_iso(NOW - timedelta(hours=1)),
        outcome_status="complete",
    )
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert resolver.calls == []
    assert stats["identities_seen"] == 0


# --------------------------------------------------------------------------
# No source_calls performance-field writes — criterion 10
# --------------------------------------------------------------------------


async def test_writer_does_not_touch_source_calls_performance_fields(db):
    await _insert_source_call(
        db._conn,
        event_id="e1",
        resolved_state="eligible_contract",
        contract_address="0xabc",
        chain="base",
        call_ts=_iso(NOW - timedelta(hours=1)),
    )
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle(close=5.0)])

    await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    cur = await db._conn.execute(
        "SELECT price_at_call, forward_24h_pct, max_favorable_pct_24h, "
        "outcome_status, resolved_state FROM source_calls WHERE source_event_id='e1'"
    )
    row = await cur.fetchone()
    assert row["price_at_call"] is None
    assert row["forward_24h_pct"] is None
    assert row["max_favorable_pct_24h"] is None
    assert row["outcome_status"] == "pending"  # unchanged
    assert row["resolved_state"] == "eligible_contract"  # unchanged


# --------------------------------------------------------------------------
# Bootstrap deadlock regression (D1 + D2 + D3, 2026-07-31)
#
# Production had 0 rows in source_call_price_snapshots and 0 source_calls in
# 'pending', because three defects compounded:
#   D1 ledger._status_from_missing terminalised a call to 'unresolvable' before
#      checking whether its forward windows were still open, so a brand-new call
#      was excluded from the writer's outcome_status IN ('pending','partial').
#   D2 the writer's SQL was hard-scoped to source_type='x' AND
#      resolved_state='eligible_contract' — the live lane is 'tg'.
#   D3 _priceable_identity returned ("token_id", …) for ANY truthy token_id, and
#      the writer skips non-"contract" identities. Every TG row carries a
#      synthetic `dex:{chain}:{address}` token_id, so all 404 CA-bearing TG rows
#      were skipped even after D1+D2.
# Each fix alone still writes zero snapshots; these lock in all three.
# --------------------------------------------------------------------------


async def test_writer_prices_tg_row_with_dex_pseudo_token_id(db):
    """The full-chain regression: a TG call carrying BOTH a synthetic `dex:`
    token_id AND a real contract address must be priced via the contract path.

    Fails on D1-only, D2-only and D1+D2 — the row is skipped by
    `identity[0] != "contract"` because the dex: pseudo-id wins in
    _priceable_identity."""
    await _insert_source_call(
        db._conn,
        event_id="tg1",
        source_type="tg",
        resolved_state="RESOLVED",  # prod value; NOT 'eligible_contract'
        token_id="dex:solana:BX8np2NYESCYThnWhRMXZytPyybKsbnVEYMPGgWVpump",
        contract_address="BX8np2NYESCYThnWhRMXZytPyybKsbnVEYMPGgWVpump",
        chain="solana",
        call_ts=_iso(NOW - timedelta(hours=2)),
        outcome_status="pending",
    )
    resolver = RecordingResolver(result=_pool(network="solana", pool_address="POOLTG"))
    fetcher = RecordingFetcher(result=[_candle(close=0.00035)])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    rows = await _snapshots(db._conn)
    assert len(rows) == 1, "TG row with dex: token_id must be priced by contract"
    assert rows[0]["identity_kind"] == "contract"
    assert rows[0]["identity_key"] == (
        "solana|" + "BX8np2NYESCYThnWhRMXZytPyybKsbnVEYMPGgWVpump".lower()
    )
    assert stats["snapshots_written"] == 1
    # original-case CA reaches the provider (Solana is case-sensitive)
    assert resolver.calls == [
        {
            "chain": "solana",
            "contract_address": "BX8np2NYESCYThnWhRMXZytPyybKsbnVEYMPGgWVpump",
        }
    ]


async def test_writer_still_skips_row_with_real_cg_coin_id(db):
    """No regression on the working path: a real CG slug still routes to the
    token_id identity, which this contract-keyed writer does not price."""
    await _insert_source_call(
        db._conn,
        event_id="tg2",
        source_type="tg",
        resolved_state="RESOLVED",
        token_id="rocket-pool",  # genuine CG coin id
        contract_address="0xD33526068D116cE69F19A9ee46F0bd304F21A51f",
        chain="ethereum",
        call_ts=_iso(NOW - timedelta(hours=2)),
    )
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert await _snapshots(db._conn) == []
    assert stats["identities_seen"] == 0
    assert resolver.calls == []


async def test_writer_selects_tg_row_regardless_of_resolved_state_case(db):
    """D2: resolved_state is mixed-case and inconsistent in prod
    (RESOLVED/resolved/unresolved/UNRESOLVED_*). Selection must not depend on it."""
    for ev, state in (("a", "resolved"), ("b", "UNRESOLVED_TRANSIENT")):
        await _insert_source_call(
            db._conn,
            event_id=f"tg-{ev}",
            source_type="tg",
            resolved_state=state,
            token_id=f"dex:solana:Addr{ev}",
            contract_address=f"Addr{ev}",
            chain="solana",
            call_ts=_iso(NOW - timedelta(hours=1)),
        )
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert stats["identities_seen"] == 2
    assert stats["snapshots_written"] == 2


async def test_writer_still_excludes_row_without_contract_address(db):
    await _insert_source_call(
        db._conn,
        event_id="tg3",
        source_type="tg",
        resolved_state="RESOLVED",
        symbol="PEPE",
        call_ts=_iso(NOW - timedelta(hours=1)),
    )
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert stats["identities_seen"] == 0
    assert await _snapshots(db._conn) == []


async def test_writer_still_excludes_tg_row_outside_horizon(db):
    await _insert_source_call(
        db._conn,
        event_id="tg4",
        source_type="tg",
        resolved_state="RESOLVED",
        token_id="dex:solana:OldAddr",
        contract_address="OldAddr",
        chain="solana",
        call_ts=_iso(NOW - timedelta(hours=40)),  # > 28h horizon
    )
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert stats["identities_seen"] == 0
    assert await _snapshots(db._conn) == []


async def test_writer_still_excludes_terminal_outcome_status(db):
    """outcome_status gating is retained — it is meaningful once D1 is fixed."""
    await _insert_source_call(
        db._conn,
        event_id="tg5",
        source_type="tg",
        resolved_state="RESOLVED",
        token_id="dex:solana:TermAddr",
        contract_address="TermAddr",
        chain="solana",
        call_ts=_iso(NOW - timedelta(hours=1)),
        outcome_status="unresolvable",
    )
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert stats["identities_seen"] == 0


# --------------------------------------------------------------------------
# Activation controls (2026-07-31): hard per-run and per-day provider-request
# ceilings, plus the counters an operator needs to judge a live writer.
# Both ceilings fail CLOSED — over-cap work is DEFERRED (still inside the
# forward horizon), never silently dropped.
# --------------------------------------------------------------------------


async def _seed_identities(db, n, *, now=NOW, hours_ago=1):
    for i in range(n):
        await _insert_source_call(
            db._conn,
            event_id=f"cap-{i}",
            source_type="tg",
            resolved_state="RESOLVED",
            token_id=f"dex:solana:Cap{i}",
            contract_address=f"Cap{i}",
            chain="solana",
            call_ts=_iso(now - timedelta(hours=hours_ago)),
        )


async def test_per_run_cap_defers_excess_and_reports_it(db):
    await _seed_identities(db, 8)
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=resolver,
        fetch_ohlcv=fetcher,
        max_identities_per_run=3,
    )

    assert stats["identities_selected"] == 8
    assert stats["identities_seen"] == 3
    assert stats["skipped_over_cap"] == 5
    assert stats["snapshots_written"] == 3
    assert len(resolver.calls) == 3  # hard ceiling actually bounds provider calls


async def test_daily_ceiling_blocks_run_before_any_provider_call(db):
    await _seed_identities(db, 4)
    # 2 requests/identity, so a 10-request budget is exhausted by 5 identities.
    await db._conn.execute(
        "INSERT INTO source_call_price_snapshot_runs "
        "(ran_at, identities_seen, snapshots_written, provider_errors, "
        " pools_unresolved, empty_ohlcv) VALUES (?, 5, 5, 0, 0, 0)",
        (NOW.isoformat(),),
    )
    await db._conn.commit()

    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=resolver,
        fetch_ohlcv=fetcher,
        max_requests_per_day=10,
    )

    assert stats["daily_cap_reached"] == 1
    assert stats["requests_attempted"] == 0
    assert resolver.calls == []  # breach costs ZERO provider requests
    assert await _snapshots(db._conn) == []


async def test_daily_ceiling_allows_partial_budget(db):
    await _seed_identities(db, 5)
    await db._conn.execute(
        "INSERT INTO source_call_price_snapshot_runs "
        "(ran_at, identities_seen, snapshots_written, provider_errors, "
        " pools_unresolved, empty_ohlcv) VALUES (?, 3, 3, 0, 0, 0)",
        (NOW.isoformat(),),
    )
    await db._conn.commit()

    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    # 10-request budget, 6 already spent -> 4 remaining -> 2 identities affordable.
    stats = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=resolver,
        fetch_ohlcv=fetcher,
        max_requests_per_day=10,
    )

    assert stats["identities_seen"] == 2
    assert stats["skipped_over_cap"] == 3
    assert stats["requests_used_today_before"] == 6


async def test_rate_limited_counter_separates_429_from_other_errors(db):
    await _seed_identities(db, 1)
    resolver = RecordingResolver(raises=PriceProviderError("geckoterminal", "http_429"))
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert stats["rate_limited"] == 1
    assert stats["provider_errors"] == 1


async def test_non_429_error_is_not_counted_as_rate_limited(db):
    await _seed_identities(db, 1)
    resolver = RecordingResolver(raises=PriceProviderError("geckoterminal", "http_500"))
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert stats["rate_limited"] == 0
    assert stats["provider_errors"] == 1


async def test_cycle_reports_latency_error_rate_and_remaining_budget(db):
    await _seed_identities(db, 2)
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=resolver,
        fetch_ohlcv=fetcher,
        max_requests_per_day=100,
    )

    assert "duration_ms" in stats and stats["duration_ms"] >= 0
    assert stats["error_rate"] == 0.0
    assert stats["requests_attempted"] == 4  # 2 identities x (resolve + ohlcv)
    assert stats["requests_remaining_today_before"] == 100
    assert stats["requests_remaining_today_after"] == 96


# ---------------------------------------------------------------------------
# Stage B Option 3 — CG coin_id lane.
#
# The identities covered here are the MAJORITY of priceable TG calls (110 of 127
# open calls on prod 2026-08-13), and their only other price source —
# gainers/losers snapshots — carries a 7-day retention DELETE, so a trailing-30d
# decision surface cannot be reconstructed from it. These snapshots must
# therefore be append-only, forward-only, and knowable as-of any past instant.
# ---------------------------------------------------------------------------


async def _seed_price_cache(conn, coin_id, price, updated_at):
    await conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(coin_id) DO UPDATE SET "
        " current_price=excluded.current_price, updated_at=excluded.updated_at",
        (coin_id, price, updated_at),
    )
    await conn.commit()


async def _cg_rows(conn, coin_id):
    cur = await conn.execute(
        "SELECT price, snapshot_at, source, identity_kind, chain "
        "FROM source_call_price_snapshots "
        "WHERE identity_key = ? ORDER BY snapshot_at",
        (coin_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


def test_ledger_and_storage_cg_kind_strings_are_reconciled():
    """THE NAMING TRAP, pinned.

    The schema CHECK permits 'coin_id'; `ledger._priceable_identity` emits
    'token_id'. Writing under one and reading under the other returns ZERO ROWS
    with no error at all. This test fails loudly on a mismatch instead.
    """
    import inspect

    from scout.db import Database as _Db
    from scout.source_quality.snapshot_writer import (
        LEDGER_CG_KIND,
        STORAGE_CG_KIND,
    )

    assert LEDGER_CG_KIND == "token_id", "ledger kind drifted from _priceable_identity"
    assert STORAGE_CG_KIND == "coin_id", "storage kind drifted from the schema CHECK"
    assert LEDGER_CG_KIND != STORAGE_CG_KIND, (
        "if these ever become equal the reconciliation is moot — but the test "
        "below must still prove the schema accepts the storage kind"
    )

    # The storage kind must be one the schema CHECK actually permits, read from
    # the DDL rather than assumed. NOTE this half is WEAK on its own: the CHECK
    # also permits 'contract', so it would pass for the wrong string too. The
    # literal `== "coin_id"` assert above is the real pin; this one only catches
    # a schema edit that drops the kind entirely.
    ddl = inspect.getsource(_Db)
    marker = "CHECK (identity_kind IN ("
    assert marker in ddl
    allowed = ddl.split(marker, 1)[1].split(")", 1)[0]
    assert (
        f"'{STORAGE_CG_KIND}'" in allowed
    ), f"schema CHECK does not permit {STORAGE_CG_KIND!r}: {allowed}"


async def test_ledger_priceable_identity_kind_matches_the_writer_constant(db):
    """The writer branches on `_priceable_identity`'s kind string. If the ledger
    renames it, the writer stops recognising CG identities and silently writes
    nothing — so pin the actual returned value, not a copy of it."""
    from scout.source_quality.ledger import _priceable_identity
    from scout.source_quality.snapshot_writer import LEDGER_CG_KIND

    kind, key = _priceable_identity({"token_id": "bitcoin", "contract_address": None})
    assert kind == LEDGER_CG_KIND
    assert key == "bitcoin"


def test_new_writer_env_knobs_are_declared_in_settings():
    """G1: every knob the .sh wrapper reads from .env MUST be declared here.

    `Settings` sets extra="forbid", so an operator following the documented
    usage — putting one of these in .env — would raise a ValidationError at
    import and break EVERY pipeline boot, not just this writer. The two knobs
    added for the CG lane were wired through the wrapper and argparse without
    being declared, which is exactly what the sibling declarations exist to
    prevent.

    Mirrors the reviewer's three-way probe: a known-declared sibling plus both
    new knobs, all set at once.
    """
    from scout.config import Settings

    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        SOURCE_CALL_SNAPSHOT_MAX_IDENTITIES_PER_RUN=25,  # declared sibling
        SOURCE_CALL_SNAPSHOT_MAX_CG_IDENTITIES_PER_RUN=8,
        SOURCE_CALL_SNAPSHOT_MAX_PRICE_CACHE_AGE_MIN=45,
    )
    assert s.SOURCE_CALL_SNAPSHOT_MAX_CG_IDENTITIES_PER_RUN == 8
    assert s.SOURCE_CALL_SNAPSHOT_MAX_PRICE_CACHE_AGE_MIN == 45


def test_writer_defaults_match_the_settings_declarations():
    """The wrapper defaults and the Settings defaults must not drift apart —
    otherwise the knob means one thing unset and another set."""
    from scout.config import Settings
    from scout.source_quality.snapshot_writer import (
        DEFAULT_MAX_CG_IDENTITIES_PER_RUN,
        DEFAULT_MAX_PRICE_CACHE_AGE_MIN,
    )

    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    assert (
        s.SOURCE_CALL_SNAPSHOT_MAX_CG_IDENTITIES_PER_RUN
        == DEFAULT_MAX_CG_IDENTITIES_PER_RUN
    )
    assert (
        s.SOURCE_CALL_SNAPSHOT_MAX_PRICE_CACHE_AGE_MIN
        == DEFAULT_MAX_PRICE_CACHE_AGE_MIN
    )


def test_storage_kind_for_translates_and_refuses_unknown_kinds():
    """R7: constants alone are one-sided — a reader holding the LEDGER's kind and
    querying the table with it still gets zero rows, silently. The translation
    must exist, and an unmapped kind must RAISE rather than pass through."""
    from scout.source_quality.snapshot_writer import storage_kind_for

    assert storage_kind_for("token_id") == "coin_id"
    assert storage_kind_for("contract") == "contract"
    with pytest.raises(ValueError, match="no storage identity_kind mapped"):
        storage_kind_for("symbol")


async def test_token_id_row_without_chain_never_enters_the_contract_lane(db):
    """F1: the widened SELECT admits token_id rows, and one can carry a contract
    with NO chain. Routed into the contract lane it calls resolve_pool with
    chain=None, GT 404s, and provider_errors climbs — enough on its own to hold
    the provider_error_spike watchdog at threshold at 1-2 identities per run.

    (Measured zero rows of this class on prod 2026-08-13: a latent guard.)
    """
    await _insert_source_call(
        db._conn,
        event_id="nochain",
        resolved_state="resolved",
        call_ts=(NOW - timedelta(hours=1)).isoformat(),
        source_type="tg",
        token_id="dex:solana:SoMeAddr",  # synthetic id -> falls through to CA
        contract_address="SoMeAddr",
        chain=None,
    )
    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])

    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert resolver.calls == [], "chain-less row reached the provider"
    assert stats["identities_selected"] == 0
    assert stats["provider_errors"] == 0
    assert await _snapshots(db._conn) == []


async def test_persisted_run_row_counts_contract_snapshots_only(db):
    """F2: `source_call_price_snapshot_runs.snapshots_written` is CONTRACT-only.

    The fresh_calls_no_snapshots watchdog reads it as "did the contract lane
    produce anything". Persisting the combined total makes a dead contract lane
    look healthy on any run where only the CG lane wrote.
    """
    from scout.source_quality.snapshot_writer import record_snapshot_run

    await record_snapshot_run(
        db._conn,
        ran_at=NOW.isoformat(),
        stats={"identities_seen": 0, "snapshots_written": 5, "cg_snapshots_written": 5},
    )
    cur = await db._conn.execute(
        "SELECT snapshots_written FROM source_call_price_snapshot_runs"
    )
    assert (await cur.fetchone())[0] == 0, "CG writes masked a dead contract lane"

    await record_snapshot_run(
        db._conn,
        ran_at=NOW.isoformat(),
        stats={"identities_seen": 1, "snapshots_written": 7, "cg_snapshots_written": 5},
    )
    cur = await db._conn.execute(
        "SELECT snapshots_written FROM source_call_price_snapshot_runs "
        "ORDER BY id DESC LIMIT 1"
    )
    assert (await cur.fetchone())[0] == 2


async def test_cg_identity_is_snapshotted_from_price_cache_without_provider_calls(db):
    """The majority path: a call with a genuine CG id and NO contract address is
    now selected, priced from the local cache, and costs zero provider calls."""
    await _insert_source_call(
        db._conn,
        event_id="cg1",
        resolved_state="resolved",
        call_ts=(NOW - timedelta(hours=1)).isoformat(),
        source_type="tg",
        token_id="ai-rig-complex",
    )
    await _seed_price_cache(
        db._conn, "ai-rig-complex", 0.42, (NOW - timedelta(minutes=2)).isoformat()
    )

    resolver = RecordingResolver(result=_pool())
    fetcher = RecordingFetcher(result=[_candle()])
    stats = await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    rows = await _cg_rows(db._conn, "ai-rig-complex")
    assert len(rows) == 1
    assert rows[0]["identity_kind"] == "coin_id"
    assert rows[0]["price"] == pytest.approx(0.42)
    assert rows[0]["source"] == "cg"
    assert rows[0]["chain"] is None
    # The observation time is the CACHE's, not `now` — stamping `now` on a
    # cached price would fabricate an observation that never happened.
    assert rows[0]["snapshot_at"] == (NOW - timedelta(minutes=2)).isoformat()

    assert stats["cg_identities_selected"] == 1
    assert stats["cg_snapshots_written"] == 1
    # Zero provider traffic for this lane.
    assert resolver.calls == []
    assert fetcher.calls == []
    assert stats["requests_attempted"] == 0


async def test_stale_price_cache_row_is_skipped_not_snapshotted(db):
    """A days-old cache row must not become a fresh observation."""
    await _insert_source_call(
        db._conn,
        event_id="cg2",
        resolved_state="resolved",
        call_ts=(NOW - timedelta(hours=1)).isoformat(),
        source_type="tg",
        token_id="cupsey-2",
    )
    await _seed_price_cache(
        db._conn, "cupsey-2", 1.0, (NOW - timedelta(days=3)).isoformat()
    )

    stats = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=RecordingResolver(result=None),
        fetch_ohlcv=RecordingFetcher(),
    )

    assert await _cg_rows(db._conn, "cupsey-2") == []
    assert stats["cg_stale_cache"] == 1
    assert stats["cg_snapshots_written"] == 0


async def test_cg_snapshots_survive_a_contract_lane_budget_stop(db):
    """The CG lane costs zero provider requests, so a contract-lane budget stop
    must not discard its rows.

    Caught by the mutation battery: removing the commit on the early-return path
    left every other test green, because uncommitted rows are still visible on
    the SAME connection. The durable check is the transaction state.
    """
    await _insert_source_call(
        db._conn,
        event_id="cg5",
        resolved_state="resolved",
        call_ts=(NOW - timedelta(hours=1)).isoformat(),
        source_type="tg",
        token_id="hyper-bull",
    )
    await _seed_price_cache(
        db._conn, "hyper-bull", 3.5, (NOW - timedelta(minutes=2)).isoformat()
    )

    stats = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=RecordingResolver(result=None),
        fetch_ohlcv=RecordingFetcher(),
        max_requests_per_day=0,  # contract lane cannot afford anything
    )

    assert stats["daily_cap_reached"] == 1, "fixture invalid: budget was not exhausted"
    assert stats["cg_snapshots_written"] == 1
    assert (
        db._conn._conn.in_transaction is False
    ), "CG rows left uncommitted on the budget-stop path"
    rows = await _cg_rows(db._conn, "hyper-bull")
    assert len(rows) == 1 and rows[0]["identity_kind"] == "coin_id"


async def test_two_cg_identities_each_get_their_own_observation(db):
    """F3 (reviewer probe M3, promoted): forward-only is scoped PER IDENTITY.

    Both identities must be snapshotted even though their caches refreshed at
    different times. A global (non-per-identity) high-water mark silently drops
    whichever identity's cache is older — and with single-identity fixtures that
    mutation left 31/31 green.
    """
    pairs = [("alpha-coin", 5), ("bravo-coin", 45)]  # minutes of cache age
    for cid, age in pairs:
        await _insert_source_call(
            db._conn,
            event_id=f"m3-{cid}",
            resolved_state="resolved",
            call_ts=(NOW - timedelta(hours=1)).isoformat(),
            source_type="tg",
            token_id=cid,
        )
        await _seed_price_cache(
            db._conn, cid, 1.0, (NOW - timedelta(minutes=age)).isoformat()
        )

    stats = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=RecordingResolver(result=None),
        fetch_ohlcv=RecordingFetcher(),
    )

    cur = await db._conn.execute(
        "SELECT identity_key FROM source_call_price_snapshots "
        "WHERE identity_kind='coin_id' ORDER BY identity_key"
    )
    keys = [r[0] for r in await cur.fetchall()]
    assert keys == [
        "alpha-coin",
        "bravo-coin",
    ], "an identity was dropped — forward-only is not scoped per identity"
    assert stats["cg_snapshots_written"] == 2


async def test_cg_lane_defers_identities_over_the_per_run_cap(db):
    """R2: the CG lane has its own per-run ceiling, and deferral is REPORTED.

    It bounds write rate rather than provider budget (this lane spends none),
    so without it the row rate is bounded only by open-call coverage.
    """
    for i in range(4):
        cid = f"capped-{i}"
        await _insert_source_call(
            db._conn,
            event_id=f"cap-{i}",
            resolved_state="resolved",
            call_ts=(NOW - timedelta(hours=1)).isoformat(),
            source_type="tg",
            token_id=cid,
        )
        await _seed_price_cache(
            db._conn, cid, 1.0, (NOW - timedelta(minutes=2)).isoformat()
        )

    stats = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=RecordingResolver(result=None),
        fetch_ohlcv=RecordingFetcher(),
        max_cg_identities_per_run=2,
    )

    assert stats["cg_identities_selected"] == 4
    assert stats["cg_skipped_over_cap"] == 2
    assert stats["cg_snapshots_written"] == 2

    # Deferred, not dropped — and verified at the SAME cap. Raising the cap on
    # the second run (as this test originally did) proves only that a bigger cap
    # covers more identities; it cannot see starvation, which is what the cap
    # actually risked. G2/reviewer Q2.
    for i in range(4):
        await _seed_price_cache(
            db._conn, f"capped-{i}", 2.0, (NOW - timedelta(minutes=1)).isoformat()
        )
    stats2 = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=RecordingResolver(result=None),
        fetch_ohlcv=RecordingFetcher(),
        max_cg_identities_per_run=2,
    )
    assert stats2["cg_snapshots_written"] == 2
    cur = await db._conn.execute(
        "SELECT COUNT(DISTINCT identity_key) FROM source_call_price_snapshots "
        "WHERE identity_kind='coin_id'"
    )
    assert (await cur.fetchone())[0] == 4, "the tail never rotated in"


async def test_every_identity_rotates_in_under_a_steady_cap(db):
    """G2 (reviewer probe Q2, promoted): a per-run cap must DEFER, not STARVE.

    `cg_seen` comes out of a source_calls scan whose order is stable across runs,
    so slicing it directly picked the same prefix every cycle and identities past
    the cap were never observed even once. Four identities, cap of two, four runs
    at the SAME cap — every identity must end up with a snapshot.
    """
    for i in range(4):
        cid = f"rot-{i}"
        await _insert_source_call(
            db._conn,
            event_id=f"q2-{i}",
            resolved_state="resolved",
            call_ts=(NOW - timedelta(hours=1)).isoformat(),
            source_type="tg",
            token_id=cid,
        )

    for run in range(4):
        # The cache refreshes between runs, as it does in production.
        for i in range(4):
            await _seed_price_cache(
                db._conn,
                f"rot-{i}",
                1.0 + run,
                (NOW - timedelta(minutes=2) + timedelta(seconds=run + 1)).isoformat(),
            )
        await write_price_snapshots(
            db._conn,
            now=NOW + timedelta(minutes=run),
            resolve_pool=RecordingResolver(result=None),
            fetch_ohlcv=RecordingFetcher(),
            max_cg_identities_per_run=2,  # STEADY cap, never raised
        )

    cur = await db._conn.execute(
        "SELECT DISTINCT identity_key FROM source_call_price_snapshots "
        "WHERE identity_kind='coin_id' ORDER BY identity_key"
    )
    keys = [r[0] for r in await cur.fetchall()]
    assert keys == [
        "rot-0",
        "rot-1",
        "rot-2",
        "rot-3",
    ], "identities beyond the cap are STARVED, not deferred"


async def _observation_counts(conn):
    cur = await conn.execute(
        "SELECT identity_key, COUNT(*) AS n FROM source_call_price_snapshots "
        "WHERE identity_kind='coin_id' GROUP BY identity_key"
    )
    return {r["identity_key"]: r["n"] for r in await cur.fetchall()}


async def test_unpriceable_identities_do_not_pin_the_rotation_head(db):
    """H1 (reviewer probe A1, promoted): identities with NO price_cache row must
    not hold the bootstrap slots forever.

    Never-OBSERVABLE implies never-OBSERVED, so such an identity sorts into the
    rank-0 bucket and stays at the head of the rotation. With `cap` of them the
    observable lane stalls COMPLETELY — and the only symptom is a rising
    `cg_no_cache_row`, which reads as innocent. Keys are chosen so the dead ones
    sort first.
    """
    dead = [f"aaa-dead-{i}" for i in range(7)]  # no price_cache row at all
    live = [f"zzz-live-{i}" for i in range(3)]
    for cid in dead + live:
        await _insert_source_call(
            db._conn,
            event_id=f"a1-{cid}",
            resolved_state="resolved",
            call_ts=(NOW - timedelta(hours=1)).isoformat(),
            source_type="tg",
            token_id=cid,
        )

    for run in range(6):
        for cid in live:
            await _seed_price_cache(
                db._conn,
                cid,
                1.0 + run,
                (NOW - timedelta(minutes=2) + timedelta(seconds=run)).isoformat(),
            )
        await write_price_snapshots(
            db._conn,
            now=NOW + timedelta(minutes=run),
            resolve_pool=RecordingResolver(result=None),
            fetch_ohlcv=RecordingFetcher(),
            max_cg_identities_per_run=4,
        )

    counts = await _observation_counts(db._conn)
    missing = [c for c in live if c not in counts]
    assert missing == [], f"priceable identities never reached: {missing}"
    assert not any(c in counts for c in dead), "an unpriceable identity was written"


async def test_perpetually_stale_identities_do_not_pin_the_rotation_head(db):
    """H1 (reviewer probe A4, promoted): the same head-pin via a different
    mechanism — a cache row that is PRESENT but never fresh enough.

    Operationally distinct from A1 and worth its own test: price_cache rows
    freeze once a token drops out of the ranked universe, so perpetually-stale
    residents ACCUMULATE rather than resolve.
    """
    stale = [f"aaa-stale-{i}" for i in range(5)]
    live = [f"zzz-live-{i}" for i in range(2)]
    for cid in stale + live:
        await _insert_source_call(
            db._conn,
            event_id=f"a4-{cid}",
            resolved_state="resolved",
            call_ts=(NOW - timedelta(hours=1)).isoformat(),
            source_type="tg",
            token_id=cid,
        )
    for cid in stale:  # always beyond the 90-minute bound
        await _seed_price_cache(
            db._conn, cid, 1.0, (NOW - timedelta(days=3)).isoformat()
        )

    for run in range(5):
        for cid in live:
            await _seed_price_cache(
                db._conn,
                cid,
                1.0 + run,
                (NOW - timedelta(minutes=2) + timedelta(seconds=run)).isoformat(),
            )
        await write_price_snapshots(
            db._conn,
            now=NOW + timedelta(minutes=run),
            resolve_pool=RecordingResolver(result=None),
            fetch_ohlcv=RecordingFetcher(),
            max_cg_identities_per_run=3,
        )

    counts = await _observation_counts(db._conn)
    missing = [c for c in live if c not in counts]
    assert missing == [], f"stale identities pinned the rotation head: {missing}"
    assert not any(c in counts for c in stale), "a stale identity was written"


async def test_skipped_over_cap_counts_eligible_identities_not_raw_candidates(db):
    """H1 (reviewer probe B5, promoted): `cg_skipped_over_cap` must be measured
    against what could actually be WORKED, not against raw candidates.

    Only the raw > cap > eligible shape can see this. Here 13 candidates meet a
    cap of 5, but just 3 are priceable — so nothing was deferred, and the raw
    formula would report 8 identities as "deferred" that were in fact
    unpriceable. That mislabels a coverage problem as a throughput one, which is
    the wrong operator response entirely.
    """
    dead = [f"d-{i}" for i in range(10)]  # no price_cache row
    live = [f"l-{i}" for i in range(3)]
    for cid in dead + live:
        await _insert_source_call(
            db._conn,
            event_id=f"b5-{cid}",
            resolved_state="resolved",
            call_ts=(NOW - timedelta(hours=1)).isoformat(),
            source_type="tg",
            token_id=cid,
        )
    for cid in live:
        await _seed_price_cache(
            db._conn, cid, 1.0, (NOW - timedelta(minutes=2)).isoformat()
        )

    stats = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=RecordingResolver(result=None),
        fetch_ohlcv=RecordingFetcher(),
        max_cg_identities_per_run=5,  # raw 13 > cap 5 > eligible 3
    )

    assert stats["cg_identities_selected"] == 13
    assert stats["cg_no_cache_row"] == 10
    assert (
        stats["cg_skipped_over_cap"] == 0
    ), "unpriceable identities were mislabelled as deferred"
    assert stats["cg_snapshots_written"] == 3


async def test_rotation_order_is_canonical_not_input_dependent(db):
    """The rank's identity tiebreak makes the order independent of input order.

    Surfaced by a surviving mutation and kept deliberately: dropping the
    tiebreak does NOT break drainage, because Python's sort is stable and the
    rank-0 bucket still rotates. What it breaks is canonical ordering — the
    selection would then follow whatever order the source_calls scan happened to
    return, so a future change to that scan silently reshuffles which identities
    a capped run picks. Pinned directly on the helper, since the property is
    about the ORDERING, not about writes.
    """
    from scout.source_quality.snapshot_writer import _rotate_cg_identities

    # Two never-observed (rank 0) and one observed, fed in two different orders.
    await db._conn.execute(
        "INSERT INTO source_call_price_snapshots "
        "(identity_key, identity_kind, chain, price, snapshot_at, source) "
        "VALUES ('seen-one', 'coin_id', NULL, 1.0, ?, 'cg')",
        ((NOW - timedelta(minutes=5)).isoformat(),),
    )
    await db._conn.commit()

    order_a = await _rotate_cg_identities(db._conn, ["new-b", "new-a", "seen-one"])
    order_b = await _rotate_cg_identities(db._conn, ["seen-one", "new-a", "new-b"])
    assert order_a == order_b, "selection order depends on input order"
    # Never-observed bootstrap ahead of the observed one, keys canonical.
    assert order_a == ["new-a", "new-b", "seen-one"]


async def test_selection_filter_counts_each_exclusion_exactly_once(db):
    """The pre-filter is the SINGLE counting site for what it excludes, and the
    no-row vs stale-row distinction survives it.

    Both matter operationally: conflating them turns "this token left the ranked
    universe" into "we have never seen this token".
    """
    cases = {
        "norow": None,  # no price_cache row
        "stale": (NOW - timedelta(days=3)).isoformat(),
        "fresh": (NOW - timedelta(minutes=2)).isoformat(),
    }
    for cid, updated_at in cases.items():
        await _insert_source_call(
            db._conn,
            event_id=f"cnt-{cid}",
            resolved_state="resolved",
            call_ts=(NOW - timedelta(hours=1)).isoformat(),
            source_type="tg",
            token_id=cid,
        )
        if updated_at is not None:
            await _seed_price_cache(db._conn, cid, 1.0, updated_at)

    stats = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=RecordingResolver(result=None),
        fetch_ohlcv=RecordingFetcher(),
    )

    assert stats["cg_no_cache_row"] == 1, "no-row exclusion miscounted"
    assert stats["cg_stale_cache"] == 1, "stale exclusion miscounted"
    assert stats["cg_snapshots_written"] == 1
    assert list(await _observation_counts(db._conn)) == ["fresh"]


async def test_malformed_cache_timestamp_is_counted_separately(db):
    """R3: a corrupt `updated_at` is a data-quality fact, not an absence fact.

    Counting it as `cg_no_cache_row` would report corruption as "nothing there".
    """
    await _insert_source_call(
        db._conn,
        event_id="cgbad",
        resolved_state="resolved",
        call_ts=(NOW - timedelta(hours=1)).isoformat(),
        source_type="tg",
        token_id="badstamp",
    )
    await _seed_price_cache(db._conn, "badstamp", 1.0, "not-a-timestamp")

    stats = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=RecordingResolver(result=None),
        fetch_ohlcv=RecordingFetcher(),
    )

    assert stats["cg_bad_timestamp"] == 1
    assert stats["cg_no_cache_row"] == 0
    assert stats["cg_snapshots_written"] == 0
    assert await _cg_rows(db._conn, "badstamp") == []


async def test_cache_regression_is_logged_separately_from_nothing_new(db):
    """R5: an upstream cache moving BACKWARDS is distinct from a quiet tick."""
    import structlog

    await _insert_source_call(
        db._conn,
        event_id="cgreg",
        resolved_state="resolved",
        call_ts=(NOW - timedelta(hours=1)).isoformat(),
        source_type="tg",
        token_id="regressor",
    )
    resolver = RecordingResolver(result=None)
    fetcher = RecordingFetcher()

    await _seed_price_cache(
        db._conn, "regressor", 1.0, (NOW - timedelta(minutes=10)).isoformat()
    )
    await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )
    # Cache goes BACKWARDS.
    await _seed_price_cache(
        db._conn, "regressor", 2.0, (NOW - timedelta(minutes=40)).isoformat()
    )
    with structlog.testing.capture_logs() as log_events:
        stats = await write_price_snapshots(
            db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
        )

    events = [e["event"] for e in log_events]
    assert events.count("scps_cg_cache_regressed") == 1
    assert stats["cg_not_newer"] == 1
    assert len(await _cg_rows(db._conn, "regressor")) == 1


async def test_price_cache_age_bound_is_inclusive_at_the_boundary(db):
    """R8/M2: a row exactly AT the bound is still an observation (`>` not `>=`).

    Pins which side of the boundary is accepted, so a `>` -> `>=` flip fails.
    """
    await _insert_source_call(
        db._conn,
        event_id="cgb",
        resolved_state="resolved",
        call_ts=(NOW - timedelta(hours=1)).isoformat(),
        source_type="tg",
        token_id="boundary",
    )
    await _seed_price_cache(
        db._conn, "boundary", 1.0, (NOW - timedelta(minutes=90)).isoformat()
    )

    stats = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=RecordingResolver(result=None),
        fetch_ohlcv=RecordingFetcher(),
        max_price_cache_age_min=90,
    )
    assert stats["cg_snapshots_written"] == 1, "exactly-at-bound must be accepted"
    assert stats["cg_stale_cache"] == 0


async def test_cg_lane_is_forward_only_across_runs(db):
    """Every inserted snapshot_at is >= all existing ones for that identity, and
    an unchanged cache does not append a duplicate observation."""
    await _insert_source_call(
        db._conn,
        event_id="cg3",
        resolved_state="resolved",
        call_ts=(NOW - timedelta(hours=1)).isoformat(),
        source_type="tg",
        token_id="kintara",
    )
    resolver = RecordingResolver(result=None)
    fetcher = RecordingFetcher()

    await _seed_price_cache(
        db._conn, "kintara", 1.0, (NOW - timedelta(minutes=30)).isoformat()
    )
    await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )
    # Same cache row, later run: nothing new to observe.
    stats2 = await write_price_snapshots(
        db._conn,
        now=NOW + timedelta(minutes=5),
        resolve_pool=resolver,
        fetch_ohlcv=fetcher,
    )
    assert stats2["cg_not_newer"] == 1
    # Cache refreshes: a new observation appends.
    await _seed_price_cache(
        db._conn, "kintara", 2.0, (NOW - timedelta(minutes=1)).isoformat()
    )
    await write_price_snapshots(
        db._conn,
        now=NOW + timedelta(minutes=10),
        resolve_pool=resolver,
        fetch_ohlcv=fetcher,
    )

    rows = await _cg_rows(db._conn, "kintara")
    assert len(rows) == 2, "expected exactly one row per distinct observation"
    stamps = [r["snapshot_at"] for r in rows]
    assert stamps == sorted(stamps), "forward-only violated"
    assert rows[-1]["price"] == pytest.approx(2.0)


async def test_historical_cg_read_is_deterministic_under_later_writes(db):
    """THE KNOWABILITY INVARIANT the whole Stage B program rests on.

    An as-of read at time T must be byte-identical before and after NEWER
    observations land. Guaranteed by `snapshot_at <= as_of` plus a forward-only
    writer — if either breaks, every historical feature silently rewrites itself.
    """
    import json

    await _insert_source_call(
        db._conn,
        event_id="cg4",
        resolved_state="resolved",
        call_ts=(NOW - timedelta(hours=3)).isoformat(),
        source_type="tg",
        token_id="three-ws",
    )
    resolver = RecordingResolver(result=None)
    fetcher = RecordingFetcher()

    # All inside the cache-freshness bound, so each one is genuinely observed.
    for minutes, price in ((80, 1.0), (60, 1.1), (40, 1.2)):
        await _seed_price_cache(
            db._conn, "three-ws", price, (NOW - timedelta(minutes=minutes)).isoformat()
        )
        await write_price_snapshots(
            db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
        )

    as_of = (NOW - timedelta(minutes=50)).isoformat()

    async def _read_as_of():
        cur = await db._conn.execute(
            "SELECT price, snapshot_at FROM source_call_price_snapshots "
            "WHERE identity_kind = 'coin_id' AND identity_key = 'three-ws' "
            "  AND snapshot_at <= ? ORDER BY snapshot_at",
            (as_of,),
        )
        return json.dumps([dict(r) for r in await cur.fetchall()], sort_keys=True)

    before = await _read_as_of()
    assert before != "[]", "fixture invalid: as-of read must see something"

    # Newer observations land AFTER the historical read.
    for minutes, price in ((30, 9.0), (5, 99.0)):
        await _seed_price_cache(
            db._conn, "three-ws", price, (NOW - timedelta(minutes=minutes)).isoformat()
        )
        await write_price_snapshots(
            db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
        )

    after = await _read_as_of()
    assert after == before, "historical as-of read changed when newer rows landed"

    # DISCRIMINATING HALF (reviewer R4): the assertions above only prove SQL
    # filtering — they pass even with forward-only removed, because every write
    # so far moved forward anyway. Now regress the cache to a timestamp INSIDE
    # the as-of window. With forward-only, this is refused and the historical
    # read is unchanged; without it, a new row lands BEHIND `as_of` and the
    # answer to a question about the past silently changes.
    await _seed_price_cache(
        db._conn, "three-ws", 0.001, (NOW - timedelta(minutes=70)).isoformat()
    )
    await write_price_snapshots(
        db._conn, now=NOW, resolve_pool=resolver, fetch_ohlcv=fetcher
    )

    assert await _read_as_of() == before, (
        "a backdated observation rewrote history — forward-only is what makes "
        "the past knowable"
    )


def test_source_call_price_snapshots_has_no_prune_or_delete_path():
    """RETENTION INVARIANT: the table is append-only and unpruned.

    Mirrors the no-UPDATE pin pattern. gainers/losers snapshots were rejected as
    Stage B substrate precisely because they DELETE on a 7-day retention; if this
    table ever grows one, the substrate stops being reconstructable and the
    rejection reasoning is silently undone.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in list((root / "scout").rglob("*.py")) + list(
        (root / "scripts").rglob("*.py")
    ):
        text = path.read_text(encoding="utf-8")
        if "source_call_price_snapshots" not in text:
            continue
        for stmt in re.findall(
            r"(DELETE\s+FROM\s+source_call_price_snapshots"
            r"|DROP\s+TABLE[^\n]*source_call_price_snapshots)",
            text,
            re.IGNORECASE,
        ):
            offenders.append(f"{path.relative_to(root).as_posix()}: {stmt}")
    assert offenders == [], (
        "source_call_price_snapshots must stay append-only — pruning is bounded "
        "by the active generation evidence lifecycle: " + "; ".join(offenders)
    )
