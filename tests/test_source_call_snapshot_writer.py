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
    assert snap["identity_key"] == "solana|so1address"  # lowercased per _priceable_identity
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
