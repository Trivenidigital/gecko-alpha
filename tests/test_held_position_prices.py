"""Tests for held-position price-refresh lane (§12c-narrow remediation).

See scout/ingestion/held_position_prices.py + tasks/plan_held_position_price_freshness.md.
"""

from __future__ import annotations

import re

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.db import Database
from scout.ingestion.held_position_prices import (
    _get_cached_price_ages,
    _is_cg_coin_id,
    _reset_cycle_counter_for_tests,
    _reset_warned_today_for_tests,
    _shape_for_cache_prices,
    fetch_held_position_prices,
)
from scout.ratelimit import coingecko_limiter

SIMPLE_PRICE_PATTERN = re.compile(r"https://api\.coingecko\.com/api/v3/simple/price")


@pytest.fixture(autouse=True)
async def _clear_rate_limit():
    await coingecko_limiter.reset()
    yield
    await coingecko_limiter.reset()


@pytest.fixture(autouse=True)
def _reset_counter():
    _reset_cycle_counter_for_tests()
    yield
    _reset_cycle_counter_for_tests()


@pytest.fixture(autouse=True)
def _reset_warned_today():
    """BL-NEW-HELD-POSITION-REFRESH-RATE-GAP: per-token WARN dedup is in-memory
    module-level state; reset between tests to prevent order-dependent
    flakes."""
    _reset_warned_today_for_tests()
    yield
    _reset_warned_today_for_tests()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


async def _insert_open_trade(db: Database, token_id: str, symbol: str = "TEST"):
    """Insert a minimal open paper_trades row for testing."""
    await db._conn.execute(
        """INSERT INTO paper_trades (
            token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at, signal_combo
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            symbol,
            symbol,
            "coingecko",
            "test",
            "{}",
            1.0,
            100.0,
            100.0,
            20.0,
            10.0,
            1.2,
            0.9,
            "open",
            "2026-05-12T00:00:00+00:00",
            "test",
        ),
    )
    await db._conn.commit()


# --- _is_cg_coin_id heuristic ---


@pytest.mark.parametrize(
    "token_id,expected",
    [
        ("payai-network-2", True),
        ("bitcoin", True),
        ("anon-alien", True),
        ("ribbita-by-virtuals", True),
        ("simple_underscore", True),
        ("0xabc123def456", False),  # EVM contract addr
        ("0x" + "a" * 40, False),  # full EVM addr
        (
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            False,
        ),  # Solana base58, > 60 chars
        ("", False),
        (None, False),
    ],
)
def test_is_cg_coin_id_heuristic(token_id, expected):
    assert _is_cg_coin_id(token_id) is expected


# --- _shape_for_cache_prices ---


def test_shape_for_cache_prices_converts_simple_price_response():
    response = {
        "bitcoin": {"usd": 100_000.0, "usd_market_cap": 2e12, "usd_24h_change": 1.5},
        "payai-network-2": {"usd": 0.01, "usd_market_cap": 1e7, "usd_24h_change": -5.0},
    }
    raw = _shape_for_cache_prices(response)
    assert len(raw) == 2
    by_id = {r["id"]: r for r in raw}
    assert by_id["bitcoin"]["current_price"] == 100_000.0
    assert by_id["bitcoin"]["market_cap"] == 2e12
    assert by_id["bitcoin"]["price_change_percentage_24h"] == 1.5
    # 7d is intentionally omitted; cache_prices() COALESCE preserves existing
    assert "price_change_percentage_7d_in_currency" not in by_id["bitcoin"]


def test_shape_for_cache_prices_skips_non_dict_entries():
    response = {"bitcoin": {"usd": 100.0}, "broken": None, "also-broken": "string"}
    raw = _shape_for_cache_prices(response)
    assert len(raw) == 1
    assert raw[0]["id"] == "bitcoin"


# --- fetch_held_position_prices integration ---


async def test_no_open_trades_returns_empty(db, settings_factory):
    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        with aioresponses():
            result = await fetch_held_position_prices(session, settings, db)
    assert result == []


async def test_all_contract_addr_held_tokens_skipped(db, settings_factory):
    await _insert_open_trade(db, "0xabc123", "TKN1")
    await _insert_open_trade(
        db, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" + "x", "TKN2"
    )
    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            result = await fetch_held_position_prices(session, settings, db)
            # No HTTP call should have fired
            assert len(m.requests) == 0
    assert result == []


async def test_mixed_cohort_only_cg_ids_in_batch(db, settings_factory):
    await _insert_open_trade(db, "bitcoin", "BTC")
    await _insert_open_trade(db, "0xabc123", "EVMTKN")
    await _insert_open_trade(db, "payai-network-2", "PAYAI")
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=True)
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                SIMPLE_PRICE_PATTERN,
                payload={
                    "bitcoin": {
                        "usd": 100_000.0,
                        "usd_market_cap": 2e12,
                        "usd_24h_change": 1.5,
                    },
                    "payai-network-2": {
                        "usd": 0.01,
                        "usd_market_cap": 1e7,
                        "usd_24h_change": -12.0,
                    },
                },
            )
            result = await fetch_held_position_prices(session, settings, db)
    assert len(result) == 2
    ids = {r["id"] for r in result}
    assert ids == {"bitcoin", "payai-network-2"}


async def test_cache_prices_preserves_existing_7d(db, settings_factory):
    """The cache_prices() enhancement: COALESCE on price_change_7d so the
    held-position lane (which lacks 7d in /simple/price) doesn't null out
    existing 7d values written by earlier markets/trending fetches."""
    # Seed an existing row with 7d data (from a previous markets fetch)
    seeded = [
        {
            "id": "bitcoin",
            "current_price": 90_000.0,
            "price_change_percentage_24h": 1.0,
            "price_change_percentage_7d_in_currency": 12.5,
            "market_cap": 1.8e12,
        }
    ]
    await db.cache_prices(seeded)

    # Held-position lane writes without 7d
    held_raw = [
        {
            "id": "bitcoin",
            "current_price": 100_000.0,
            "price_change_percentage_24h": 2.0,
            "market_cap": 2.0e12,
        }
    ]
    await db.cache_prices(held_raw)

    cached = await db.get_cached_prices(["bitcoin"])
    assert cached["bitcoin"]["usd"] == 100_000.0  # current_price refreshed
    assert cached["bitcoin"]["change_24h"] == 2.0  # 24h refreshed
    assert cached["bitcoin"]["change_7d"] == 12.5  # 7d preserved via COALESCE
    assert cached["bitcoin"]["market_cap"] == 2.0e12


async def test_disabled_flag_short_circuits(db, settings_factory):
    await _insert_open_trade(db, "bitcoin", "BTC")
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=False)
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            result = await fetch_held_position_prices(session, settings, db)
            assert len(m.requests) == 0
    assert result == []


async def test_cadence_throttling(db, settings_factory):
    """interval=3 means refresh fires on counter 3, 6, 9, ... (1-indexed)."""
    await _insert_open_trade(db, "bitcoin", "BTC")
    settings = settings_factory(
        HELD_POSITION_PRICE_REFRESH_ENABLED=True,
        HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES=3,
    )
    payload = {"bitcoin": {"usd": 100.0, "usd_market_cap": 1e9, "usd_24h_change": 0.0}}

    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(SIMPLE_PRICE_PATTERN, payload=payload, repeat=True)
            r1 = await fetch_held_position_prices(session, settings, db)  # counter=1
            r2 = await fetch_held_position_prices(session, settings, db)  # counter=2
            r3 = await fetch_held_position_prices(
                session, settings, db
            )  # counter=3 fires
            r4 = await fetch_held_position_prices(session, settings, db)  # counter=4

    assert r1 == []
    assert r2 == []
    assert len(r3) == 1
    assert r4 == []


async def test_aalien_never_cached_case_gets_row_created(db, settings_factory):
    """AALIEN-style: open trade exists but no price_cache row.
    First refresh should create the row via INSERT ON CONFLICT DO UPDATE."""
    await _insert_open_trade(db, "anon-alien", "AALIEN")
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=True)

    # Confirm no cache row exists pre-refresh
    pre = await db.get_cached_prices(["anon-alien"])
    assert pre == {}

    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                SIMPLE_PRICE_PATTERN,
                payload={
                    "anon-alien": {
                        "usd": 0.00035,
                        "usd_market_cap": 350_000,
                        "usd_24h_change": -2.0,
                    }
                },
            )
            raw = await fetch_held_position_prices(session, settings, db)

    # Caller (main.py) merges raw into all_raw and calls cache_prices.
    # Simulate that here:
    await db.cache_prices(raw)

    post = await db.get_cached_prices(["anon-alien"])
    assert "anon-alien" in post
    assert post["anon-alien"]["usd"] == 0.00035
    assert post["anon-alien"]["change_7d"] is None  # never had a 7d value


async def test_429_handled_gracefully(db, settings_factory, patch_module_sleep):
    """A 429 from CoinGecko should produce empty result, not raise."""
    patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")
    await _insert_open_trade(db, "bitcoin", "BTC")
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=True)

    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(SIMPLE_PRICE_PATTERN, status=429, repeat=True)
            result = await fetch_held_position_prices(session, settings, db)

    # _get_with_backoff returns None on persistent 429 → empty response
    # → _shape_for_cache_prices returns []
    assert result == []


# ---------------------------------------------------------------------------
# BL-NEW-HELD-POSITION-REFRESH-RATE-GAP (cycle 13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cached_price_ages_returns_aware_datetimes(db):
    from datetime import datetime, timezone
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("fresh-coin", 1.0, "2026-05-18T00:00:00+00:00"),
    )
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("stale-coin", 2.0, "2026-05-10T00:00:00+00:00"),
    )
    await db._conn.commit()
    ages = await _get_cached_price_ages(db, ["fresh-coin", "stale-coin", "missing-coin"])
    assert "fresh-coin" in ages
    assert "stale-coin" in ages
    assert "missing-coin" not in ages
    assert ages["fresh-coin"].tzinfo is not None
    assert ages["fresh-coin"] == datetime(2026, 5, 18, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_get_cached_price_ages_empty_input(db):
    ages = await _get_cached_price_ages(db, [])
    assert ages == {}


def test_held_position_settings_default_warn_hours():
    from scout.config import Settings
    s = Settings(TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="y")
    assert s.HELD_POSITION_STALE_WARN_HOURS == 24


def test_held_position_settings_warn_hours_validator():
    from scout.config import Settings
    # Floor: rejects v < 1
    with pytest.raises(ValueError, match=r"\[1, 168\]"):
        Settings(
            TELEGRAM_BOT_TOKEN="x",
            TELEGRAM_CHAT_ID="y",
            HELD_POSITION_STALE_WARN_HOURS=0,
        )
    # PR-#158 R2 IMPORTANT 2 fold: ceiling — rejects v > 168 (silent-suppress prevention)
    with pytest.raises(ValueError, match=r"\[1, 168\]"):
        Settings(
            TELEGRAM_BOT_TOKEN="x",
            TELEGRAM_CHAT_ID="y",
            HELD_POSITION_STALE_WARN_HOURS=999,
        )
    # Boundary: 168 inside range
    s = Settings(
        TELEGRAM_BOT_TOKEN="x",
        TELEGRAM_CHAT_ID="y",
        HELD_POSITION_STALE_WARN_HOURS=168,
    )
    assert s.HELD_POSITION_STALE_WARN_HOURS == 168


@pytest.mark.asyncio
async def test_stale_open_count_gauge_in_summary_log(
    db, settings_factory, patch_module_sleep
):
    """P1-fold (operator 2026-05-18): tokens stale-before-cycle that
    `/simple/price` does NOT return remain stale post-write; tokens stale
    before but returned this cycle are about to be refreshed by main.py's
    cache_prices(all_raw) write — must NOT count toward stale_open_count.
    """
    patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")
    await _insert_open_trade(db, "fresh-1", "F1")
    await _insert_open_trade(db, "fresh-2", "F2")
    await _insert_open_trade(db, "stale-1", "S1")
    await _insert_open_trade(db, "no-cache-1", "NC1")
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    fresh_iso = (now - timedelta(hours=1)).isoformat()
    stale_iso = (now - timedelta(hours=48)).isoformat()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("fresh-1", 1.0, fresh_iso),
    )
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("fresh-2", 1.0, fresh_iso),
    )
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("stale-1", 1.0, stale_iso),
    )
    await db._conn.commit()
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=True)

    from structlog.testing import capture_logs
    with capture_logs() as captured:
        async with aiohttp.ClientSession() as session:
            with aioresponses() as m:
                # Only fresh-1 and fresh-2 are returned. stale-1 + no-cache-1
                # are absent — they REMAIN stale post-write (correct false-positive-free
                # gauge behavior).
                m.get(SIMPLE_PRICE_PATTERN,
                      payload={"fresh-1": {"usd": 1.0}, "fresh-2": {"usd": 1.0}})
                await fetch_held_position_prices(session, settings, db)

    summary = [e for e in captured if e.get("event") == "held_position_refresh_summary"]
    assert len(summary) == 1
    assert summary[0]["stale_open_count"] == 2  # stale-1 + no-cache-1
    assert summary[0]["stale_open_pct"] == 50.0


@pytest.mark.asyncio
async def test_persistently_stale_token_emits_warn_once_per_day(
    db, settings_factory, patch_module_sleep
):
    """P1-fold (operator 2026-05-18): WARN must NOT fire for a stale token
    that `/simple/price` is about to refresh this cycle. Use a payload
    that omits the token so it remains stale post-write.
    """
    patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")
    await _insert_open_trade(db, "ancient-coin", "AC")
    from datetime import datetime, timezone, timedelta
    ancient_iso = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("ancient-coin", 1.0, ancient_iso),
    )
    await db._conn.commit()
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=True)

    from structlog.testing import capture_logs
    with capture_logs() as captured:
        async with aiohttp.ClientSession() as session:
            with aioresponses() as m:
                # CG returns EMPTY — ancient-coin remains stale post-write,
                # so the WARN correctly fires.
                m.get(SIMPLE_PRICE_PATTERN, payload={}, repeat=True)
                await fetch_held_position_prices(session, settings, db)
                await fetch_held_position_prices(session, settings, db)

    warn_events = [e for e in captured if e.get("event") == "held_position_token_persistently_stale"]
    assert len(warn_events) == 1
    assert warn_events[0]["token_id"] == "ancient-coin"
    assert warn_events[0]["cache_age_hours"] >= 71.5
    assert warn_events[0]["warn_threshold_hours"] == 24


@pytest.mark.asyncio
async def test_stale_count_failure_does_not_block_summary_log(
    db, settings_factory, patch_module_sleep, monkeypatch
):
    patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")
    await _insert_open_trade(db, "test-coin", "TC")
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=True)
    import scout.ingestion.held_position_prices as mod
    async def _broken(*args, **kwargs):
        raise RuntimeError("simulated DB failure")
    monkeypatch.setattr(mod, "_get_cached_price_ages", _broken)

    from structlog.testing import capture_logs
    with capture_logs() as captured:
        async with aiohttp.ClientSession() as session:
            with aioresponses() as m:
                m.get(SIMPLE_PRICE_PATTERN, payload={"test-coin": {"usd": 1.0}})
                await fetch_held_position_prices(session, settings, db)

    summary = [e for e in captured if e.get("event") == "held_position_refresh_summary"]
    assert len(summary) == 1
    assert summary[0]["stale_open_count"] is None


# ----------------------------------------------------------------------
# PR-#158 reviewer-fold tests (R1 C1, R2 IMPORTANT 1, R3 I2)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warn_payload_includes_paper_trade_id_symbol_and_consequence(
    db, settings_factory, patch_module_sleep
):
    """PR-#158 R3 I2 fold: WARN must include paper_trade_id + symbol +
    consequence so operator at 3am knows what is actually broken downstream."""
    patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")
    await _insert_open_trade(db, "ancient-coin", "AC")
    from datetime import datetime, timezone, timedelta
    ancient_iso = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("ancient-coin", 1.0, ancient_iso),
    )
    await db._conn.commit()
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=True)

    from structlog.testing import capture_logs
    with capture_logs() as captured:
        async with aiohttp.ClientSession() as session:
            with aioresponses() as m:
                # P1 fold: must NOT include ancient-coin in /simple/price
                # response; otherwise the WARN would be a false positive
                # (cache will be fresh post-cache_prices write).
                m.get(SIMPLE_PRICE_PATTERN, payload={})
                await fetch_held_position_prices(session, settings, db)

    warn_events = [
        e for e in captured if e.get("event") == "held_position_token_persistently_stale"
    ]
    assert len(warn_events) == 1
    evt = warn_events[0]
    assert evt["token_id"] == "ancient-coin"
    assert evt["paper_trade_id"] is not None and isinstance(evt["paper_trade_id"], int)
    assert evt["symbol"] == "AC"
    assert evt["consequence"] == "trailing_stop_evaluator_cannot_fire_price_exits"


@pytest.mark.asyncio
async def test_simple_price_missing_ids_surfaced_in_summary(
    db, settings_factory, patch_module_sleep
):
    """PR-#158 R1 C1 fold: surface the specific token_ids /simple/price did
    not return, so post-deploy data discriminates stale-source vs other
    hypotheses (truncation, ID-mismatch, transient-failure).
    """
    patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")
    await _insert_open_trade(db, "returned-coin", "R1")
    await _insert_open_trade(db, "missing-coin-a", "MA")
    await _insert_open_trade(db, "missing-coin-b", "MB")
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=True)

    from structlog.testing import capture_logs
    with capture_logs() as captured:
        async with aiohttp.ClientSession() as session:
            with aioresponses() as m:
                # CG returns only one of the three requested IDs
                m.get(SIMPLE_PRICE_PATTERN, payload={"returned-coin": {"usd": 1.0}})
                await fetch_held_position_prices(session, settings, db)

    summary = [e for e in captured if e.get("event") == "held_position_refresh_summary"]
    assert len(summary) == 1
    missing = summary[0]["simple_price_missing_ids"]
    assert sorted(missing) == ["missing-coin-a", "missing-coin-b"]
    assert summary[0]["not_found_count"] == 2


@pytest.mark.asyncio
async def test_warned_today_prunes_entries_older_than_7d(
    db, settings_factory, patch_module_sleep
):
    """PR-#158 R2 IMPORTANT 1 fold: _warned_today must prune entries
    older than 7d to bound memory growth from closed-position residue.
    """
    patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")
    await _insert_open_trade(db, "fresh-token", "FT")
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=True)

    import scout.ingestion.held_position_prices as mod
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # Seed _warned_today with an 8d-old entry for a token NOT in the open cohort
    mod._warned_today["closed-position-residue"] = now - timedelta(days=8)
    mod._warned_today["recent-entry"] = now - timedelta(days=2)

    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(SIMPLE_PRICE_PATTERN, payload={"fresh-token": {"usd": 1.0}})
            await fetch_held_position_prices(session, settings, db)

    # 8d entry pruned; 2d entry retained
    assert "closed-position-residue" not in mod._warned_today
    assert "recent-entry" in mod._warned_today


@pytest.mark.asyncio
async def test_get_held_trade_metadata_returns_id_and_symbol(db):
    """PR-#158 R3 I2 fold: helper returns one (paper_trade_id, symbol) per
    open token_id; tokens with no open trade are absent from result."""
    from scout.ingestion.held_position_prices import _get_held_trade_metadata
    await _insert_open_trade(db, "alpha-coin", "ALPHA")
    await _insert_open_trade(db, "beta-coin", "BETA")
    meta = await _get_held_trade_metadata(db, ["alpha-coin", "beta-coin", "not-open"])
    assert "alpha-coin" in meta
    assert meta["alpha-coin"][1] == "ALPHA"
    assert isinstance(meta["alpha-coin"][0], int)
    assert "beta-coin" in meta
    assert meta["beta-coin"][1] == "BETA"
    assert "not-open" not in meta


@pytest.mark.asyncio
async def test_get_held_trade_metadata_empty_input(db):
    from scout.ingestion.held_position_prices import _get_held_trade_metadata
    assert await _get_held_trade_metadata(db, []) == {}


@pytest.mark.asyncio
async def test_metadata_query_failure_does_not_block_warn(
    db, settings_factory, patch_module_sleep, monkeypatch
):
    """PR-#158 R3 NIT fold: metadata query failure must NOT break the WARN
    block — pre-fetched as empty dict, downstream uses (None, None) fallback.
    """
    patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")
    await _insert_open_trade(db, "ancient-coin", "AC")
    from datetime import datetime, timezone, timedelta
    ancient_iso = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("ancient-coin", 1.0, ancient_iso),
    )
    await db._conn.commit()

    import scout.ingestion.held_position_prices as mod
    async def _broken(*args, **kwargs):
        raise RuntimeError("simulated metadata DB failure")
    monkeypatch.setattr(mod, "_get_held_trade_metadata", _broken)

    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=True)
    from structlog.testing import capture_logs
    with capture_logs() as captured:
        async with aiohttp.ClientSession() as session:
            with aioresponses() as m:
                # P1 fold: omit ancient-coin so it remains stale post-write
                # (otherwise WARN would be a false positive).
                m.get(SIMPLE_PRICE_PATTERN, payload={})
                await fetch_held_position_prices(session, settings, db)

    warn_events = [
        e for e in captured if e.get("event") == "held_position_token_persistently_stale"
    ]
    assert len(warn_events) == 1
    # Metadata query failed → paper_trade_id + symbol fall back to None
    assert warn_events[0]["paper_trade_id"] is None
    assert warn_events[0]["symbol"] is None
    # consequence still present
    assert warn_events[0]["consequence"] == "trailing_stop_evaluator_cannot_fire_price_exits"


# ----------------------------------------------------------------------
# Operator-flagged fold (2026-05-18): stale-before-cache-write false positives
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_before_refresh_but_returned_by_simple_price_does_NOT_warn(
    db, settings_factory, patch_module_sleep
):
    """Operator-flagged P1 (2026-05-18): a token that was stale BEFORE this
    cycle but `/simple/price` returned data for THIS cycle will be cache-
    fresh after main.py's `db.cache_prices(all_raw)` write. The visibility
    block must NOT count it toward stale_open_count nor fire the WARN —
    doing so would be a false positive because the consequence
    (`trailing_stop_evaluator_cannot_fire_price_exits`) does not hold.
    """
    patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")
    await _insert_open_trade(db, "will-be-refreshed", "REF")
    from datetime import datetime, timezone, timedelta
    ancient_iso = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("will-be-refreshed", 1.0, ancient_iso),
    )
    await db._conn.commit()
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=True)

    from structlog.testing import capture_logs
    with capture_logs() as captured:
        async with aiohttp.ClientSession() as session:
            with aioresponses() as m:
                # CG DOES return data for the stale token — it's about to be refreshed.
                m.get(
                    SIMPLE_PRICE_PATTERN,
                    payload={"will-be-refreshed": {"usd": 2.0}},
                )
                await fetch_held_position_prices(session, settings, db)

    # No WARN — the token is being refreshed this cycle.
    warn_events = [
        e for e in captured if e.get("event") == "held_position_token_persistently_stale"
    ]
    assert warn_events == [], (
        "Stale token returned by /simple/price this cycle must NOT warn (false positive)"
    )
    # Gauge count is 0 — the only held token is being refreshed.
    summary = [e for e in captured if e.get("event") == "held_position_refresh_summary"]
    assert len(summary) == 1
    assert summary[0]["stale_open_count"] == 0
    assert summary[0]["stale_open_pct"] == 0.0


@pytest.mark.asyncio
async def test_stale_before_refresh_and_missing_from_simple_price_warns(
    db, settings_factory, patch_module_sleep
):
    """Operator-flagged P1 (2026-05-18): a token that was stale BEFORE this
    cycle AND is absent from `/simple/price` response IS still stale post-write.
    WARN must fire and gauge must count it. This is the TRUE positive case.
    """
    patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")
    await _insert_open_trade(db, "still-stale", "SS")
    from datetime import datetime, timezone, timedelta
    ancient_iso = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("still-stale", 1.0, ancient_iso),
    )
    await db._conn.commit()
    settings = settings_factory(HELD_POSITION_PRICE_REFRESH_ENABLED=True)

    from structlog.testing import capture_logs
    with capture_logs() as captured:
        async with aiohttp.ClientSession() as session:
            with aioresponses() as m:
                # CG does NOT return — token remains stale post-write.
                m.get(SIMPLE_PRICE_PATTERN, payload={})
                await fetch_held_position_prices(session, settings, db)

    warn_events = [
        e for e in captured if e.get("event") == "held_position_token_persistently_stale"
    ]
    assert len(warn_events) == 1
    assert warn_events[0]["token_id"] == "still-stale"
    summary = [e for e in captured if e.get("event") == "held_position_refresh_summary"]
    assert summary[0]["stale_open_count"] == 1


@pytest.mark.asyncio
async def test_get_cached_price_ages_normalizes_naive_isoformat_to_utc(db):
    """Operator-flagged P2 (2026-05-18): SQLite-style `datetime('now')`
    produces an ISO string with NO timezone offset. `datetime.fromisoformat`
    on such a value returns a naive datetime. Subtracting naive from
    tz-aware `datetime.now(timezone.utc)` raises TypeError, which the
    outer try/except would silently swallow → stale_open_count=None.

    Helper must mirror evaluator.py:74-77 — attach UTC when tzinfo missing.
    """
    from scout.ingestion.held_position_prices import _get_cached_price_ages
    from datetime import datetime, timezone
    # SQLite-style naive ISO (no offset)
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("naive-coin", 1.0, "2026-05-18 10:00:00"),
    )
    # Python-style aware ISO (+00:00 suffix)
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("aware-coin", 1.0, "2026-05-18T10:00:00+00:00"),
    )
    await db._conn.commit()
    ages = await _get_cached_price_ages(db, ["naive-coin", "aware-coin"])
    assert ages["naive-coin"].tzinfo is not None
    assert ages["aware-coin"].tzinfo is not None
    # Both must be subtractable from a tz-aware now without raising.
    now = datetime.now(timezone.utc)
    _ = (now - ages["naive-coin"]).total_seconds()
    _ = (now - ages["aware-coin"]).total_seconds()
