"""Tests for scripts/backfill_dexscreener_liquidity.py (Phase 1a-ii cron writer)."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest
import aiosqlite
from aioresponses import aioresponses


def _cg_url(slug: str) -> "re.Pattern":
    """CG /coins/{slug} URL pattern that ignores query params.

    The cron passes localization/tickers/market_data/community_data/
    developer_data/sparkline=false; aioresponses' default behavior
    requires an exact URL match including query string. Pattern-match
    keeps the test focused on the path."""
    return re.compile(
        rf"^https://api\.coingecko\.com/api/v3/coins/{re.escape(slug)}(\?.*)?$"
    )

from scripts.backfill_dexscreener_liquidity import (
    CG_PLATFORM_TO_DEX_CHAIN,
    _parse_dex_prefix,
    fetch_batch,
    resolve_cg_slug_to_platforms,
    resolve_dex_pair_liquidity,
    resolve_row,
    run_tick,
    touch_heartbeat,
)


@pytest.fixture
def cron_settings(settings_factory):
    """Settings preset for cron tests.

    ``settings_factory`` (from conftest.py) handles the env-file bypass
    and shared limiter state. We add the Phase 1a-i cron flags here.
    Per-test overrides can be passed positionally::

        settings = cron_settings(LIQUIDITY_BACKFILL_BATCH_MAX=3)
    """

    def _factory(**overrides):
        defaults = {
            "LIQUIDITY_ENRICHMENT_ENABLED": True,
            "LIQUIDITY_ENRICHMENT_TTL_SEC": 1800,
            "LIQUIDITY_BACKFILL_BATCH_MAX": 10,
        }
        defaults.update(overrides)
        return settings_factory(**defaults)

    return _factory


# ---------------------------------------------------------------------------
# _parse_dex_prefix
# ---------------------------------------------------------------------------


def test_parse_dex_prefix_solana_pattern_returns_chain_address():
    result = _parse_dex_prefix(
        "dex:solana:5UUH9RTDiSpq6HKS6bp4NdU9PNJpXRXuiw6ShBTBhgH2"
    )
    assert result == (
        "solana",
        "5UUH9RTDiSpq6HKS6bp4NdU9PNJpXRXuiw6ShBTBhgH2",
    )


def test_parse_dex_prefix_ethereum_address():
    result = _parse_dex_prefix(
        "dex:ethereum:0x1234567890123456789012345678901234567890"
    )
    assert result == (
        "ethereum",
        "0x1234567890123456789012345678901234567890",
    )


def test_parse_dex_prefix_cg_slug_returns_none():
    """Plain CG slugs (no prefix) → None → caller takes the CG-hop path."""
    assert _parse_dex_prefix("staynex") is None
    assert _parse_dex_prefix("billions-network") is None


def test_parse_dex_prefix_malformed_returns_none():
    """Malformed inputs should fall through to CG-hop, not crash."""
    assert _parse_dex_prefix("dex:") is None
    assert _parse_dex_prefix("dex:solana") is None
    assert _parse_dex_prefix("dex::address") is None
    assert _parse_dex_prefix("dex:chain:") is None


# ---------------------------------------------------------------------------
# CG_PLATFORM_TO_DEX_CHAIN coverage
# ---------------------------------------------------------------------------


def test_cg_platform_mapping_covers_chains_we_already_ingest():
    """All chains gecko-alpha's existing pipeline produces real-chain
    candidates for must be in the CG → DexScreener translation table.

    Per the prod query on 2026-05-29, real chains in `candidates` are:
    solana / base / ethereum / bsc / ton / hyperevm / polygon (via
    config). Missing chains here would mark valid rows
    `dex_no_match`."""
    expected_dex_chains = {
        "solana",
        "base",
        "ethereum",
        "bsc",
        "polygon",
        "arbitrum",
        "optimism",
        "avalanche",
        "fantom",
        "ton",
        "hyperevm",
    }
    actual_dex_chains = set(CG_PLATFORM_TO_DEX_CHAIN.values())
    missing = expected_dex_chains - actual_dex_chains
    assert not missing, f"Missing DexScreener chains in mapping: {missing}"


# ---------------------------------------------------------------------------
# resolve_cg_slug_to_platforms — HTTP mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cg_slug_resolves_to_platforms():
    """Successful CG response with non-empty platforms returns the dict."""
    with aioresponses() as m:
        m.get(
            _cg_url("spark-2"),
            payload={
                "id": "spark-2",
                "symbol": "spk",
                "platforms": {
                    "ethereum": "0xspark",
                    "base": "0xsparkbase",
                },
            },
        )
        async with aiohttp.ClientSession() as session:
            result = await resolve_cg_slug_to_platforms(session, "spark-2")
    assert result == {"ethereum": "0xspark", "base": "0xsparkbase"}


@pytest.mark.asyncio
async def test_cg_slug_404_returns_empty_dict():
    """CG 404 = slug no longer in CG → empty dict marker for unresolvable."""
    with aioresponses() as m:
        m.get(_cg_url("delisted"), status=404)
        async with aiohttp.ClientSession() as session:
            result = await resolve_cg_slug_to_platforms(session, "delisted")
    assert result == {}


@pytest.mark.asyncio
async def test_cg_slug_429_returns_none():
    """429 → None so caller treats as transient skip and does NOT
    clobber the row's prior enriched_at value.

    (The function also reports the 429 to coingecko_limiter; that
    side-effect is exercised in scout.ratelimit's own test suite. We
    don't assert backoff-state here because the test cooldown is 0.)"""
    with aioresponses() as m:
        m.get(
            _cg_url("throttled"), status=429
        )
        async with aiohttp.ClientSession() as session:
            result = await resolve_cg_slug_to_platforms(
                session, "throttled"
            )
    assert result is None


@pytest.mark.asyncio
async def test_cg_slug_empty_platforms_returns_empty_dict():
    """CG returns the slug but with empty platforms — token has no
    on-chain listing CG knows about. cg_slug_unresolvable in caller."""
    with aioresponses() as m:
        m.get(
            _cg_url("abstractcoin"),
            payload={"id": "abstractcoin", "platforms": {}},
        )
        async with aiohttp.ClientSession() as session:
            result = await resolve_cg_slug_to_platforms(
                session, "abstractcoin"
            )
    assert result == {}


@pytest.mark.asyncio
async def test_cg_slug_strips_empty_address_values():
    """CG often lists a token on a chain with an empty-string address
    (registered but not tradeable). Strip those — they are not real
    matches."""
    with aioresponses() as m:
        m.get(
            _cg_url("halfdone"),
            payload={
                "platforms": {
                    "ethereum": "0xreal",
                    "polygon-pos": "",  # listed but no address
                    "base": None,  # also bogus
                },
            },
        )
        async with aiohttp.ClientSession() as session:
            result = await resolve_cg_slug_to_platforms(session, "halfdone")
    assert result == {"ethereum": "0xreal"}


# ---------------------------------------------------------------------------
# resolve_dex_pair_liquidity — HTTP mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dex_pair_picks_highest_liquidity_match():
    """Multiple pairs returned → use the highest-liquidity one (deepest
    pool is the most actionable for sizing)."""
    with aioresponses() as m:
        m.get(
            "https://api.dexscreener.com/tokens/v1/solana/0xtok",
            payload=[
                {"liquidity": {"usd": 50_000}},
                {"liquidity": {"usd": 250_000}},  # winner
                {"liquidity": {"usd": 100_000}},
            ],
        )
        async with aiohttp.ClientSession() as session:
            result = await resolve_dex_pair_liquidity(
                session, "solana", "0xtok"
            )
    assert result == 250_000.0


@pytest.mark.asyncio
async def test_dex_pair_empty_response_returns_none():
    with aioresponses() as m:
        m.get(
            "https://api.dexscreener.com/tokens/v1/solana/0xnone",
            payload=[],
        )
        async with aiohttp.ClientSession() as session:
            result = await resolve_dex_pair_liquidity(
                session, "solana", "0xnone"
            )
    assert result is None


@pytest.mark.asyncio
async def test_dex_pair_all_zero_liquidity_returns_none():
    with aioresponses() as m:
        m.get(
            "https://api.dexscreener.com/tokens/v1/solana/0xempty",
            payload=[
                {"liquidity": {"usd": 0}},
                {"liquidity": {"usd": 0}},
            ],
        )
        async with aiohttp.ClientSession() as session:
            result = await resolve_dex_pair_liquidity(
                session, "solana", "0xempty"
            )
    assert result is None


@pytest.mark.asyncio
async def test_dex_pair_404_returns_none_no_retry():
    """Non-429/5xx HTTP error returns None immediately (no retry — the
    token genuinely isn't on this chain via DexScreener)."""
    with aioresponses() as m:
        m.get(
            "https://api.dexscreener.com/tokens/v1/solana/0xmissing",
            status=404,
        )
        async with aiohttp.ClientSession() as session:
            result = await resolve_dex_pair_liquidity(
                session, "solana", "0xmissing"
            )
    assert result is None


# ---------------------------------------------------------------------------
# resolve_row — top-level resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_row_dex_prefix_bypasses_cg_hop():
    """dex:<chain>:<address> shortcut hits DexScreener directly."""
    with aioresponses() as m:
        m.get(
            "https://api.dexscreener.com/tokens/v1/solana/SoLaddr",
            payload=[{"liquidity": {"usd": 88_888}}],
        )
        async with aiohttp.ClientSession() as session:
            liquidity, source, confidence = await resolve_row(
                session, "dex:solana:SoLaddr"
            )
    assert liquidity == 88_888.0
    assert source == "dexscreener_v1"
    assert confidence == "definite"


@pytest.mark.asyncio
async def test_resolve_row_single_chain_match_is_definite():
    with aioresponses() as m:
        m.get(
            _cg_url("single-chain-tok"),
            payload={"platforms": {"ethereum": "0xtokeneth"}},
        )
        m.get(
            "https://api.dexscreener.com/tokens/v1/ethereum/0xtokeneth",
            payload=[{"liquidity": {"usd": 12_345}}],
        )
        async with aiohttp.ClientSession() as session:
            liquidity, source, confidence = await resolve_row(
                session, "single-chain-tok"
            )
    assert liquidity == 12_345.0
    assert source == "dexscreener_v1"
    assert confidence == "definite"


@pytest.mark.asyncio
async def test_resolve_row_multi_chain_picks_highest_liquidity():
    """CG returns 2 platforms; DexScreener returns liquidity on each;
    cron picks the highest and marks `multi_chain`."""
    with aioresponses() as m:
        m.get(
            _cg_url("multi-tok"),
            payload={
                "platforms": {
                    "ethereum": "0xeth",
                    "base": "0xbase",
                },
            },
        )
        m.get(
            "https://api.dexscreener.com/tokens/v1/ethereum/0xeth",
            payload=[{"liquidity": {"usd": 10_000}}],
        )
        m.get(
            "https://api.dexscreener.com/tokens/v1/base/0xbase",
            payload=[{"liquidity": {"usd": 30_000}}],  # winner
        )
        async with aiohttp.ClientSession() as session:
            liquidity, source, confidence = await resolve_row(
                session, "multi-tok"
            )
    assert liquidity == 30_000.0
    assert source == "dexscreener_v1"
    assert confidence == "multi_chain"


@pytest.mark.asyncio
async def test_resolve_row_cg_empty_platforms_is_unresolvable():
    with aioresponses() as m:
        m.get(
            _cg_url("abstract"),
            payload={"platforms": {}},
        )
        async with aiohttp.ClientSession() as session:
            liquidity, source, confidence = await resolve_row(
                session, "abstract"
            )
    assert liquidity is None
    assert source == "dexscreener_v1"
    assert confidence == "cg_slug_unresolvable"


@pytest.mark.asyncio
async def test_resolve_row_cg_404_is_unresolvable():
    """Slug not in CG = cg_slug_unresolvable (not a transient skip)."""
    with aioresponses() as m:
        m.get(_cg_url("delisted"), status=404)
        async with aiohttp.ClientSession() as session:
            liquidity, source, confidence = await resolve_row(
                session, "delisted"
            )
    assert liquidity is None
    assert source == "dexscreener_v1"
    assert confidence == "cg_slug_unresolvable"


@pytest.mark.asyncio
async def test_resolve_row_cg_429_returns_transient_skip():
    """CG 429 → resolve_row returns empty confidence so caller skips
    the write (does NOT clobber prior good enriched_at).

    Per feedback_resilience_layered_failure_modes.md."""
    with aioresponses() as m:
        m.get(
            _cg_url("throttled"), status=429
        )
        async with aiohttp.ClientSession() as session:
            liquidity, source, confidence = await resolve_row(
                session, "throttled"
            )
    assert liquidity is None
    assert source is None
    assert confidence == ""  # transient skip marker


@pytest.mark.asyncio
async def test_resolve_row_dex_no_match_when_dex_returns_empty():
    """CG resolves; DexScreener returns no pair on any platform →
    dex_no_match."""
    with aioresponses() as m:
        m.get(
            _cg_url("no-dex"),
            payload={"platforms": {"ethereum": "0xeth"}},
        )
        m.get(
            "https://api.dexscreener.com/tokens/v1/ethereum/0xeth",
            payload=[],
        )
        async with aiohttp.ClientSession() as session:
            liquidity, source, confidence = await resolve_row(
                session, "no-dex"
            )
    assert liquidity is None
    assert source == "dexscreener_v1"
    assert confidence == "dex_no_match"


@pytest.mark.asyncio
async def test_resolve_row_unmapped_cg_platform_falls_through_to_no_match():
    """CG returns a platform NOT in CG_PLATFORM_TO_DEX_CHAIN — cron must
    NOT guess the DexScreener chain name. Returns dex_no_match instead.

    Honors operator guardrail #3: deterministic resolution only."""
    with aioresponses() as m:
        m.get(
            _cg_url("exotic-chain-tok"),
            payload={
                "platforms": {
                    "kava": "0xkava",  # not in mapping
                    "celo": "0xcelo",  # not in mapping
                },
            },
        )
        async with aiohttp.ClientSession() as session:
            liquidity, source, confidence = await resolve_row(
                session, "exotic-chain-tok"
            )
    assert liquidity is None
    assert source == "dexscreener_v1"
    assert confidence == "dex_no_match"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _build_test_db(
    db_path: Path,
    rows: list[tuple[str, str | None]],
) -> None:
    """Build a minimal candidates table with the 4 Phase 1a-i columns.

    Each row tuple: ``(contract_address, liquidity_enriched_at | None)``.
    Other required columns get sensible defaults.
    """
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE candidates (
            contract_address TEXT PRIMARY KEY,
            chain TEXT NOT NULL,
            token_name TEXT NOT NULL,
            ticker TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            liquidity_usd_enriched REAL,
            liquidity_enriched_source TEXT,
            liquidity_enriched_at TEXT,
            liquidity_enriched_confidence TEXT
        );
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    for addr, enriched_at in rows:
        conn.execute(
            "INSERT INTO candidates VALUES "
            "(?, 'coingecko', ?, ?, ?, NULL, NULL, ?, NULL)",
            (addr, addr, addr.upper(), now, enriched_at),
        )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_fetch_batch_prioritizes_null_then_oldest(tmp_path):
    """NULL liquidity_enriched_at first; then oldest first."""
    db = tmp_path / "scout.db"
    older = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    newer_but_past_ttl = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    _build_test_db(
        db,
        [
            ("addr_null", None),
            ("addr_older", older),
            ("addr_newer", newer_but_past_ttl),
        ],
    )
    conn = await aiosqlite.connect(str(db))
    try:
        # TTL = 30 min → all 3 rows are stale (newer is 1h old).
        batch = await fetch_batch(conn, ttl_sec=1800, batch_max=10)
    finally:
        await conn.close()
    assert batch[0] == "addr_null"
    # Among non-null timestamps, oldest first
    assert batch[1] == "addr_older"
    assert batch[2] == "addr_newer"


@pytest.mark.asyncio
async def test_fetch_batch_respects_batch_max(tmp_path):
    db = tmp_path / "scout.db"
    _build_test_db(db, [(f"addr_{i}", None) for i in range(20)])
    conn = await aiosqlite.connect(str(db))
    try:
        batch = await fetch_batch(conn, ttl_sec=1800, batch_max=5)
    finally:
        await conn.close()
    assert len(batch) == 5


@pytest.mark.asyncio
async def test_fetch_batch_skips_fresh_rows(tmp_path):
    """Rows with liquidity_enriched_at within TTL should be SKIPPED."""
    db = tmp_path / "scout.db"
    fresh = datetime.now(timezone.utc).isoformat()
    older = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _build_test_db(
        db,
        [
            ("fresh_addr", fresh),
            ("older_addr", older),
        ],
    )
    conn = await aiosqlite.connect(str(db))
    try:
        batch = await fetch_batch(conn, ttl_sec=1800, batch_max=10)
    finally:
        await conn.close()
    # fresh row excluded; older row included
    assert "fresh_addr" not in batch
    assert "older_addr" in batch


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_touch_heartbeat_creates_file(tmp_path):
    hb = tmp_path / "subdir" / "heartbeat"
    touch_heartbeat(hb)
    assert hb.exists()
    body = hb.read_text(encoding="utf-8").strip()
    parsed = datetime.fromisoformat(body)
    assert parsed.tzinfo is not None


def test_touch_heartbeat_updates_existing(tmp_path):
    hb = tmp_path / "heartbeat"
    touch_heartbeat(hb)
    first = hb.stat().st_mtime
    # Force enough time for mtime to differ
    import os
    import time as _time

    past = _time.time() - 10
    os.utime(hb, (past, past))
    touch_heartbeat(hb)
    assert hb.stat().st_mtime > first - 5


# ---------------------------------------------------------------------------
# run_tick — integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_tick_writes_enrichment_and_touches_heartbeat(
    tmp_path, cron_settings
):
    """Happy path: row needs enrichment, CG + DexScreener resolve,
    enrichment columns get populated, heartbeat is touched."""
    db = tmp_path / "scout.db"
    _build_test_db(db, [("test-slug", None)])
    hb = tmp_path / "heartbeat"
    settings = cron_settings()
    with aioresponses() as m:
        m.get(
            _cg_url("test-slug"),
            payload={"platforms": {"ethereum": "0xtest"}},
        )
        m.get(
            "https://api.dexscreener.com/tokens/v1/ethereum/0xtest",
            payload=[{"liquidity": {"usd": 55_555}}],
        )
        visited, enriched, errored = await run_tick(db, hb, settings)
    assert visited == 1
    assert enriched == 1
    assert errored == 0
    assert hb.exists()

    # Verify the row got written
    conn = sqlite3.connect(str(db))
    cur = conn.execute(
        "SELECT liquidity_usd_enriched, liquidity_enriched_source, "
        "  liquidity_enriched_at, liquidity_enriched_confidence "
        "FROM candidates WHERE contract_address = 'test-slug'"
    )
    row = cur.fetchone()
    conn.close()
    assert row[0] == 55_555.0
    assert row[1] == "dexscreener_v1"
    assert row[2] is not None
    assert row[3] == "definite"


@pytest.mark.asyncio
async def test_run_tick_no_work_still_touches_heartbeat(
    tmp_path, cron_settings
):
    """No rows need enrichment → batch is empty → heartbeat STILL
    touched. This honors operator's gate: 'Touch heartbeat on every
    successful cron tick, including no-work ticks.'"""
    db = tmp_path / "scout.db"
    fresh = datetime.now(timezone.utc).isoformat()
    _build_test_db(db, [("addr_fresh", fresh)])
    hb = tmp_path / "heartbeat"
    settings = cron_settings(LIQUIDITY_ENRICHMENT_TTL_SEC=3600)
    with aioresponses():
        visited, enriched, errored = await run_tick(db, hb, settings)
    assert visited == 0
    assert enriched == 0
    assert errored == 0
    assert hb.exists()


@pytest.mark.asyncio
async def test_run_tick_fail_soft_per_token_does_not_abort_batch(
    tmp_path, cron_settings
):
    """If one token's resolve_row raises, the cron continues with the
    rest. Heartbeat is still touched at tick end."""
    db = tmp_path / "scout.db"
    _build_test_db(
        db,
        [
            ("bad-row", None),
            ("good-row", None),
        ],
    )
    hb = tmp_path / "heartbeat"
    settings = cron_settings()

    # Patch resolve_row to throw on bad-row but succeed on good-row.
    async def fake_resolve_row(_session, addr):
        if addr == "bad-row":
            raise RuntimeError("simulated upstream failure")
        return 11_111.0, "dexscreener_v1", "definite"

    with patch(
        "scripts.backfill_dexscreener_liquidity.resolve_row",
        new=fake_resolve_row,
    ):
        visited, enriched, errored = await run_tick(db, hb, settings)
    assert errored == 1
    assert enriched == 1  # good-row still made it
    assert hb.exists()
    # Verify good-row got written, bad-row did NOT get clobbered.
    conn = sqlite3.connect(str(db))
    cur = conn.execute(
        "SELECT contract_address, liquidity_enriched_at "
        "FROM candidates ORDER BY contract_address"
    )
    rows = list(cur.fetchall())
    conn.close()
    by_addr = {r[0]: r[1] for r in rows}
    assert by_addr["good-row"] is not None
    # bad-row's enriched_at was NULL pre-tick; must remain NULL (skip,
    # do not clobber with a partial state) per fail-soft semantics.
    assert by_addr["bad-row"] is None


@pytest.mark.asyncio
async def test_run_tick_transient_cg_429_skips_row_without_clobbering(
    tmp_path, cron_settings
):
    """CG 429 → confidence == "" → caller skips write. Existing
    enriched_at MUST survive (resilience-layered failure-mode discipline)."""
    db = tmp_path / "scout.db"
    prior = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()  # past TTL → batch includes
    _build_test_db(db, [("throttle-tok", prior)])
    hb = tmp_path / "heartbeat"
    settings = cron_settings()

    # Pre-seed enrichment state so we can detect non-clobber.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE candidates SET "
        "  liquidity_usd_enriched = ?, "
        "  liquidity_enriched_source = ?, "
        "  liquidity_enriched_confidence = ? "
        "WHERE contract_address = 'throttle-tok'",
        (99_999.0, "dexscreener_v1", "definite"),
    )
    conn.commit()
    conn.close()

    with aioresponses() as m:
        m.get(
            _cg_url("throttle-tok"),
            status=429,
        )
        visited, enriched, errored = await run_tick(db, hb, settings)
    assert visited == 1  # visited
    assert enriched == 0  # no write (transient skip)
    assert hb.exists()  # heartbeat touched on tick completion
    # Prior enrichment SURVIVED — no clobber.
    conn = sqlite3.connect(str(db))
    cur = conn.execute(
        "SELECT liquidity_usd_enriched, liquidity_enriched_at, "
        "  liquidity_enriched_confidence "
        "FROM candidates WHERE contract_address = 'throttle-tok'"
    )
    row = cur.fetchone()
    conn.close()
    assert row[0] == 99_999.0
    assert row[1] == prior  # original enriched_at
    assert row[2] == "definite"


@pytest.mark.asyncio
async def test_run_tick_respects_batch_max(tmp_path, cron_settings):
    """Cron processes at most batch_max rows per tick — bounds CG budget."""
    db = tmp_path / "scout.db"
    _build_test_db(db, [(f"slug_{i}", None) for i in range(20)])
    hb = tmp_path / "heartbeat"
    settings = cron_settings(LIQUIDITY_BACKFILL_BATCH_MAX=3)
    with aioresponses() as m:
        # Mock CG + DEX for first 3 rows (in fetch order, by slug name)
        for i in range(20):
            m.get(
                _cg_url(f"slug_{i}"),
                payload={"platforms": {"ethereum": f"0x{i}"}},
            )
            m.get(
                f"https://api.dexscreener.com/tokens/v1/ethereum/0x{i}",
                payload=[{"liquidity": {"usd": 1000 + i}}],
            )
        visited, enriched, errored = await run_tick(db, hb, settings)
    assert visited == 3
    assert enriched == 3
