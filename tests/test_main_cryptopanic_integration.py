"""Integration tests for CryptoPanic wiring into scout.main.run_cycle (BL-053)."""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.main import run_cycle
from scout.models import CandidateToken
from scout.news.schemas import CryptoPanicPost

# aioresponses matches full URL including querystring unless given a regex.
_CP_URL = re.compile(r"https://cryptopanic\.com/api/v1/posts/.*")


def _mk_token(**overrides) -> CandidateToken:
    defaults = dict(
        contract_address="0xtest",
        chain="solana",
        token_name="Test",
        ticker="TST",
        token_age_days=1.0,
        market_cap_usd=50000.0,
        liquidity_usd=10000.0,
        volume_24h_usd=80000.0,
        holder_count=100,
        holder_growth_1h=25,
    )
    defaults.update(overrides)
    return CandidateToken(**defaults)


def _mk_db() -> AsyncMock:
    db = AsyncMock()
    db.upsert_candidate = AsyncMock()
    db.log_alert = AsyncMock()
    db.get_previous_holder_count = AsyncMock(return_value=None)
    db.log_holder_snapshot = AsyncMock()
    db.log_score = AsyncMock()
    db.get_recent_scores = AsyncMock(return_value=[])
    db.get_vol_7d_avg = AsyncMock(return_value=None)
    db.log_volume_snapshot = AsyncMock()
    db.was_recently_alerted = AsyncMock(return_value=False)
    db.cache_prices = AsyncMock(return_value=0)
    db.insert_cryptopanic_post = AsyncMock(return_value=1)
    return db


def _mk_settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.SCAN_INTERVAL_SECONDS = 60
    s.MIN_SCORE = 60
    s.DB_PATH = ":memory:"
    s.CRYPTOPANIC_ENABLED = False
    s.CRYPTOPANIC_API_TOKEN = ""
    s.CRYPTOPANIC_FETCH_FILTER = "hot"
    s.CRYPTOPANIC_MACRO_MIN_CURRENCIES = 4
    s.CRYPTOPANIC_SCORING_ENABLED = False
    s.VOLUME_SPIKE_ENABLED = False
    s.GAINERS_TRACKER_ENABLED = False
    s.LOSERS_TRACKER_ENABLED = False
    s.MOMENTUM_7D_ENABLED = False
    s.VELOCITY_ALERTS_ENABLED = False
    s.CONVICTION_THRESHOLD = 60
    s.COUNTER_ENABLED = False
    s.PERP_ENABLED = False
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


async def test_run_cycle_cryptopanic_disabled_skips_fetch():
    """With CRYPTOPANIC_ENABLED=False the fetcher is never awaited and DB is not written to."""
    token = _mk_token()
    db = _mk_db()
    settings = _mk_settings(CRYPTOPANIC_ENABLED=False)
    session = AsyncMock()

    with (
        patch(
            "scout.main.fetch_trending", new_callable=AsyncMock, return_value=[token]
        ),
        patch(
            "scout.main.fetch_trending_pools", new_callable=AsyncMock, return_value=[]
        ),
        patch(
            "scout.main.cg_fetch_top_movers", new_callable=AsyncMock, return_value=[]
        ),
        patch("scout.main.cg_fetch_trending", new_callable=AsyncMock, return_value=[]),
        patch("scout.main.cg_fetch_by_volume", new_callable=AsyncMock, return_value=[]),
        patch(
            "scout.main.enrich_holders",
            new_callable=AsyncMock,
            side_effect=lambda t, s, st: t,
        ),
        patch("scout.main.aggregate", return_value=[token]),
        patch("scout.main.score", return_value=(75, ["vol_liq_ratio"])),
        patch(
            "scout.main.evaluate",
            new_callable=AsyncMock,
            return_value=(False, 40.0, token),
        ),
        patch(
            "scout.main.fetch_cryptopanic_posts",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_fetch,
    ):
        stats = await run_cycle(settings, db, session, dry_run=True)

    mock_fetch.assert_not_called()
    db.insert_cryptopanic_post.assert_not_awaited()
    assert stats["tokens_scanned"] == 1


async def test_run_cycle_includes_midcap_gainers_in_aggregate_and_raw_cache():
    """Midcap rows reach both candidate scoring and raw-market signal surfaces."""
    midcap = _mk_token(
        contract_address="playnance-like",
        chain="coingecko",
        token_name="PlaynanceLike",
        ticker="GCOIN",
        market_cap_usd=90_000_000,
        volume_24h_usd=840_000,
        price_change_24h=96.4,
    )
    db = _mk_db()
    settings = _mk_settings(
        VOLUME_SPIKE_ENABLED=True,
        GAINERS_TRACKER_ENABLED=True,
        MOMENTUM_7D_ENABLED=True,
    )
    session = AsyncMock()

    with (
        patch("scout.main.fetch_trending", new_callable=AsyncMock, return_value=[]),
        patch(
            "scout.main.fetch_trending_pools", new_callable=AsyncMock, return_value=[]
        ),
        patch(
            "scout.main.cg_fetch_top_movers", new_callable=AsyncMock, return_value=[]
        ),
        patch("scout.main.cg_fetch_trending", new_callable=AsyncMock, return_value=[]),
        patch("scout.main.cg_fetch_by_volume", new_callable=AsyncMock, return_value=[]),
        patch(
            "scout.main.cg_fetch_midcap_gainers",
            new_callable=AsyncMock,
            return_value=[midcap],
        ),
        patch(
            "scout.main.fetch_held_position_prices",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "scout.main._cg_module.last_raw_markets",
            [],
        ),
        patch("scout.main._cg_module.last_raw_trending", []),
        patch("scout.main._cg_module.last_raw_by_volume", []),
        patch(
            "scout.main._cg_module.last_raw_midcap_gainers",
            [{"id": "playnance-like", "price_change_percentage_24h": 96.4}],
        ),
        patch("scout.main.record_volume", new_callable=AsyncMock) as mock_record_volume,
        patch("scout.main.detect_spikes", new_callable=AsyncMock, return_value=[]),
        patch(
            "scout.main.store_top_gainers", new_callable=AsyncMock, return_value=1
        ) as mock_store_top_gainers,
        patch("scout.main.detect_7d_momentum", new_callable=AsyncMock, return_value=[]),
        patch("scout.main.aggregate", return_value=[midcap]) as mock_aggregate,
        patch(
            "scout.main.enrich_holders",
            new_callable=AsyncMock,
            side_effect=lambda t, s, st: t,
        ),
        patch("scout.main.score", return_value=(40, [])),
    ):
        stats = await run_cycle(settings, db, session, dry_run=True)

    aggregate_tokens = mock_aggregate.call_args.args[0]
    assert [t.contract_address for t in aggregate_tokens] == ["playnance-like"]
    mock_record_volume.assert_awaited_once()
    assert mock_record_volume.call_args.args[1][0]["id"] == "playnance-like"
    mock_store_top_gainers.assert_awaited_once()
    assert mock_store_top_gainers.call_args.args[1][0]["id"] == "playnance-like"
    db.cache_prices.assert_awaited_once()
    assert db.cache_prices.call_args.args[0][0]["id"] == "playnance-like"
    assert stats["tokens_scanned"] == 1


async def test_run_cycle_cryptopanic_enabled_persists_and_tags():
    """With CRYPTOPANIC_ENABLED=True, fetched posts are persisted and candidates are tagged."""
    token = _mk_token(ticker="PEPE")
    db = _mk_db()
    settings = _mk_settings(
        CRYPTOPANIC_ENABLED=True,
        CRYPTOPANIC_API_TOKEN="tok",
    )
    session = AsyncMock()

    post = CryptoPanicPost(
        post_id=42,
        title="PEPE pumps",
        url="https://example.com/p42",
        published_at="2026-04-20T10:00:00Z",
        currencies=["PEPE"],
        votes_positive=5,
        votes_negative=0,
    )

    with (
        patch(
            "scout.main.fetch_trending", new_callable=AsyncMock, return_value=[token]
        ),
        patch(
            "scout.main.fetch_trending_pools", new_callable=AsyncMock, return_value=[]
        ),
        patch(
            "scout.main.cg_fetch_top_movers", new_callable=AsyncMock, return_value=[]
        ),
        patch("scout.main.cg_fetch_trending", new_callable=AsyncMock, return_value=[]),
        patch("scout.main.cg_fetch_by_volume", new_callable=AsyncMock, return_value=[]),
        patch(
            "scout.main.enrich_holders",
            new_callable=AsyncMock,
            side_effect=lambda t, s, st: t,
        ),
        patch("scout.main.aggregate", return_value=[token]),
        patch("scout.main.score", return_value=(75, ["vol_liq_ratio"])),
        patch(
            "scout.main.evaluate",
            new_callable=AsyncMock,
            return_value=(False, 40.0, token),
        ),
        patch(
            "scout.main.fetch_cryptopanic_posts",
            new_callable=AsyncMock,
            return_value=[post],
        ) as mock_fetch,
    ):
        stats = await run_cycle(settings, db, session, dry_run=True)

    mock_fetch.assert_awaited_once()
    db.insert_cryptopanic_post.assert_awaited()
    # upsert_candidate gets the tagged token (news_count_24h == 1, sentiment bullish)
    # The first upsert_candidate call is from the scoring stage; it receives the
    # enriched (tagged) token copy, so its `news_count_24h` must be 1.
    upsert_calls = db.upsert_candidate.await_args_list
    assert any(
        getattr(call.args[0], "news_count_24h", None) == 1 for call in upsert_calls
    ), "Expected at least one upserted token to be tagged (news_count_24h == 1)"
    assert stats["tokens_scanned"] == 1


async def test_run_cycle_cryptopanic_real_aioresponses(settings_factory):
    """End-to-end: fetcher hits a mocked CryptoPanic endpoint; posts persist + tag."""
    token = _mk_token(ticker="PEPE")
    db = _mk_db()
    settings = settings_factory(
        CRYPTOPANIC_ENABLED=True,
        CRYPTOPANIC_API_TOKEN="tok",
        VOLUME_SPIKE_ENABLED=False,
        GAINERS_TRACKER_ENABLED=False,
        LOSERS_TRACKER_ENABLED=False,
        MOMENTUM_7D_ENABLED=False,
        VELOCITY_ALERTS_ENABLED=False,
        COUNTER_ENABLED=False,
    )

    body = {
        "results": [
            {
                "id": 101,
                "title": "PEPE pump news",
                "url": "https://example.com/p101",
                "published_at": "2026-04-20T10:00:00Z",
                "currencies": [{"code": "PEPE"}],
                "votes": {"positive": 7, "negative": 1},
            }
        ]
    }

    async with aiohttp.ClientSession() as session:
        with (
            aioresponses() as m,
            patch(
                "scout.main.fetch_trending",
                new_callable=AsyncMock,
                return_value=[token],
            ),
            patch(
                "scout.main.fetch_trending_pools",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "scout.main.cg_fetch_top_movers",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "scout.main.cg_fetch_trending",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "scout.main.cg_fetch_by_volume",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "scout.main.enrich_holders",
                new_callable=AsyncMock,
                side_effect=lambda t, s, st: t,
            ),
            patch("scout.main.aggregate", return_value=[token]),
            patch("scout.main.score", return_value=(75, ["vol_liq_ratio"])),
            patch(
                "scout.main.evaluate",
                new_callable=AsyncMock,
                return_value=(False, 40.0, token),
            ),
        ):
            m.get(_CP_URL, payload=body, status=200, repeat=True)
            await run_cycle(settings, db, session, dry_run=True)

    db.insert_cryptopanic_post.assert_awaited()
    upsert_calls = db.upsert_candidate.await_args_list
    assert any(
        getattr(call.args[0], "news_count_24h", None) == 1 for call in upsert_calls
    )


@pytest.mark.slow
async def test_run_cycle_cryptopanic_fetch_hang_does_not_stall_cycle():
    """A hung CryptoPanic fetch is cancelled after 10s; cycle still completes."""
    token = _mk_token()
    db = _mk_db()
    settings = _mk_settings(
        CRYPTOPANIC_ENABLED=True,
        CRYPTOPANIC_API_TOKEN="tok",
    )
    session = AsyncMock()

    async def _hang(*args, **kwargs):
        # Sleep far longer than the wait_for cap (10s).
        await asyncio.sleep(120)
        return []

    with (
        patch(
            "scout.main.fetch_trending", new_callable=AsyncMock, return_value=[token]
        ),
        patch(
            "scout.main.fetch_trending_pools", new_callable=AsyncMock, return_value=[]
        ),
        patch(
            "scout.main.cg_fetch_top_movers", new_callable=AsyncMock, return_value=[]
        ),
        patch("scout.main.cg_fetch_trending", new_callable=AsyncMock, return_value=[]),
        patch("scout.main.cg_fetch_by_volume", new_callable=AsyncMock, return_value=[]),
        patch(
            "scout.main.enrich_holders",
            new_callable=AsyncMock,
            side_effect=lambda t, s, st: t,
        ),
        patch("scout.main.aggregate", return_value=[token]),
        patch("scout.main.score", return_value=(75, ["vol_liq_ratio"])),
        patch(
            "scout.main.evaluate",
            new_callable=AsyncMock,
            return_value=(False, 40.0, token),
        ),
        patch("scout.main.fetch_cryptopanic_posts", side_effect=_hang),
    ):
        stats = await run_cycle(settings, db, session, dry_run=True)

    db.insert_cryptopanic_post.assert_not_awaited()
    assert stats["tokens_scanned"] == 1
    # Candidates still flow through despite the CryptoPanic hang.
    assert db.upsert_candidate.await_count >= 1
