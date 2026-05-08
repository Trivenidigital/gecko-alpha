"""BL-NEW-LIVE-HYBRID M1 v2.1: routing layer tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.live.routing import RoutingLayer

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


def _settings(**overrides):
    from scout.config import Settings

    return Settings(_env_file=None, **_REQUIRED, **overrides)


async def _seed_paper(db: Database, token_id: str = "tok") -> int:
    """Insert a paper_trades row + return its id (for live_trades FK)."""
    cur = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES (?, 'X', 'x', 'ethereum', 'first_signal', '{}',
                   100, 50, 0.5, 120, 80, 'open',
                   '2026-05-08T00:00:00+00:00')""",
        (token_id,),
    )
    return cur.lastrowid


@pytest.mark.asyncio
async def test_aggregator_guard_rejects_when_token_already_open(tmp_path):
    """M1-blocker: open live_trades.symbol='BILL' + cap=1 → empty list."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db, token_id="bill_paper")
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, created_at)
           VALUES (?, 'bill', 'BILL', 'binance', 'BILLUSDT', 'gainers_early',
                   '50.0', 'open', '2026-05-08T00:00:00+00:00')""",
        (paper_id,),
    )
    await db._conn.commit()
    s = _settings(LIVE_MAX_OPEN_POSITIONS_PER_TOKEN=1)
    routing = RoutingLayer(db=db, settings=s, adapters={})
    candidates = await routing.get_candidates(
        canonical="BILL", chain_hint="solana",
        signal_type="chain_completed", size_usd=50.0,
    )
    assert candidates == []
    await db.close()


@pytest.mark.asyncio
async def test_aggregator_guard_uses_symbol_not_coin_id(tmp_path):
    """Plan-stage structural reviewer pin: aggregator-guard queries by
    SYMBOL (ticker), NOT by coin_id (CoinGecko slug). For BTC, the
    CoinGecko slug is 'bitcoin' but the canonical ticker is 'BTC'.
    Query must compare 'BTC' to row.symbol='BTC', not to row.coin_id=
    'bitcoin'."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db, token_id="btc_paper")
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, created_at)
           VALUES (?, 'bitcoin', 'BTC', 'binance', 'BTCUSDT', 'gainers_early',
                   '50.0', 'open', '2026-05-08T00:00:00+00:00')""",
        (paper_id,),
    )
    await db._conn.commit()
    s = _settings(LIVE_MAX_OPEN_POSITIONS_PER_TOKEN=1)
    routing = RoutingLayer(db=db, settings=s, adapters={})
    candidates = await routing.get_candidates(
        canonical="BTC", chain_hint=None,
        signal_type="gainers_early", size_usd=50.0,
    )
    assert candidates == [], (
        "guard must fire when symbol matches, regardless of coin_id divergence"
    )
    await db.close()


@pytest.mark.asyncio
async def test_no_venue_when_listings_empty_and_no_adapters(tmp_path):
    """Empty venue_listings + no adapters → empty result + no exception."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = _settings()
    routing = RoutingLayer(db=db, settings=s, adapters={})
    candidates = await routing.get_candidates(
        canonical="UNKNOWN", chain_hint=None,
        signal_type="first_signal", size_usd=50.0,
    )
    assert candidates == []
    await db.close()


@pytest.mark.asyncio
async def test_seeded_listings_produce_candidates(tmp_path):
    """venue_listings + venue_health rows → RouteCandidate yielded."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO venue_listings
           (venue, canonical, venue_pair, quote, asset_class, refreshed_at)
           VALUES ('binance', 'BTC', 'BTCUSDT', 'USDT', 'spot', ?)""",
        (now_iso,),
    )
    await db._conn.execute(
        """INSERT INTO venue_health
           (venue, probe_at, rest_responsive, ws_connected,
            auth_ok, last_balance_fetch_ok, last_quote_mid_price,
            last_depth_at_size_bps)
           VALUES ('binance', ?, 1, 1, 1, 1, 50000.0, 25.0)""",
        (now_iso,),
    )
    await db._conn.commit()
    s = _settings()
    routing = RoutingLayer(db=db, settings=s, adapters={})
    candidates = await routing.get_candidates(
        canonical="BTC", chain_hint=None,
        signal_type="first_signal", size_usd=50.0,
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.venue == "binance"
    assert c.venue_pair == "BTCUSDT"
    assert c.expected_fill_price == 50000.0
    assert c.expected_slippage_bps == 25.0
    assert c.venue_health_score == 1.0
    await db.close()


@pytest.mark.asyncio
async def test_unhealthy_venues_excluded(tmp_path):
    """auth_ok=0 OR rest_responsive=0 OR is_dormant=1 → venue dropped."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO venue_listings
           (venue, canonical, venue_pair, quote, asset_class, refreshed_at)
           VALUES ('binance', 'BTC', 'BTCUSDT', 'USDT', 'spot', ?)""",
        (now_iso,),
    )
    await db._conn.execute(
        """INSERT INTO venue_health
           (venue, probe_at, rest_responsive, ws_connected,
            auth_ok, last_balance_fetch_ok, is_dormant)
           VALUES ('binance', ?, 1, 1, 0, 0, 0)""",
        (now_iso,),
    )
    await db._conn.commit()
    s = _settings()
    routing = RoutingLayer(db=db, settings=s, adapters={})
    candidates = await routing.get_candidates(
        canonical="BTC", chain_hint=None,
        signal_type="first_signal", size_usd=50.0,
    )
    assert candidates == []
    await db.close()


@pytest.mark.asyncio
async def test_override_prepend_keeps_other_candidates_as_fallback(tmp_path):
    """Override venue prepended; non-override venues remain in fallback
    position. LIVE_OVERRIDE_REPLACE_ONLY=False (default)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now_iso = datetime.now(timezone.utc).isoformat()
    expires = "2099-01-01T00:00:00+00:00"
    for venue in ("binance", "kraken"):
        await db._conn.execute(
            """INSERT INTO venue_listings
               (venue, canonical, venue_pair, quote, asset_class, refreshed_at)
               VALUES (?, 'BTC', 'BTCUSDT', 'USDT', 'spot', ?)""",
            (venue, now_iso),
        )
        await db._conn.execute(
            """INSERT INTO venue_health
               (venue, probe_at, rest_responsive, ws_connected,
                auth_ok, last_balance_fetch_ok)
               VALUES (?, ?, 1, 1, 1, 1)""",
            (venue, now_iso),
        )
    await db._conn.execute(
        """INSERT INTO live_operator_overrides
           (override_type, venue, canonical, set_at, expires_at)
           VALUES ('allow_stack', 'kraken', 'BTC', ?, ?)""",
        (now_iso, expires),
    )
    await db._conn.commit()
    s = _settings(LIVE_OVERRIDE_REPLACE_ONLY=False)
    routing = RoutingLayer(db=db, settings=s, adapters={})
    candidates = await routing.get_candidates(
        canonical="BTC", chain_hint=None,
        signal_type="first_signal", size_usd=50.0,
    )
    venues = [c.venue for c in candidates]
    assert venues[0] == "kraken", f"override should be first; got {venues}"
    assert "binance" in venues, "non-override should remain"
    await db.close()


@pytest.mark.asyncio
async def test_override_replace_drops_other_candidates(tmp_path):
    """LIVE_OVERRIDE_REPLACE_ONLY=True → only override venues kept."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now_iso = datetime.now(timezone.utc).isoformat()
    expires = "2099-01-01T00:00:00+00:00"
    for venue in ("binance", "kraken"):
        await db._conn.execute(
            """INSERT INTO venue_listings
               (venue, canonical, venue_pair, quote, asset_class, refreshed_at)
               VALUES (?, 'BTC', 'BTCUSDT', 'USDT', 'spot', ?)""",
            (venue, now_iso),
        )
        await db._conn.execute(
            """INSERT INTO venue_health
               (venue, probe_at, rest_responsive, ws_connected,
                auth_ok, last_balance_fetch_ok)
               VALUES (?, ?, 1, 1, 1, 1)""",
            (venue, now_iso),
        )
    await db._conn.execute(
        """INSERT INTO live_operator_overrides
           (override_type, venue, canonical, set_at, expires_at)
           VALUES ('allow_stack', 'kraken', 'BTC', ?, ?)""",
        (now_iso, expires),
    )
    await db._conn.commit()
    s = _settings(LIVE_OVERRIDE_REPLACE_ONLY=True)
    routing = RoutingLayer(db=db, settings=s, adapters={})
    candidates = await routing.get_candidates(
        canonical="BTC", chain_hint=None,
        signal_type="first_signal", size_usd=50.0,
    )
    venues = [c.venue for c in candidates]
    assert venues == ["kraken"]
    await db.close()
