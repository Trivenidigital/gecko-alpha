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
