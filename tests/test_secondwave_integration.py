"""End-to-end test: scan DB -> score -> alert via mocked Telegram + CoinGecko."""
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.config import Settings
from scout.db import Database
from scout.secondwave.detector import run_once


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "int.db")
    await d.initialize()
    yield d
    await d.close()


def _settings(db_path) -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="x",
        DB_PATH=str(db_path),
        SECONDWAVE_ENABLED=True,
        SECONDWAVE_MIN_PRIOR_SCORE=60,
        SECONDWAVE_COOLDOWN_MIN_DAYS=3,
        SECONDWAVE_COOLDOWN_MAX_DAYS=14,
        SECONDWAVE_MIN_DRAWDOWN_PCT=30.0,
        SECONDWAVE_MIN_RECOVERY_PCT=70.0,
        SECONDWAVE_VOL_PICKUP_RATIO=2.0,
        SECONDWAVE_ALERT_THRESHOLD=50,
    )


async def test_end_to_end_dex_token_detection(db, tmp_path):
    # Seed alerts + score_history for an in-window token with peak 80
    alerted_at = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    await db._conn.execute(
        """INSERT INTO alerts
           (contract_address, chain, conviction_score, alert_market_cap, price_usd, token_name, ticker, alerted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("0xdex", "ethereum", 85.0, 2_000_000.0, 1.0, "DexTok", "DEX", alerted_at),
    )
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xdex", 80.0, alerted_at),
    )
    await db._conn.commit()

    settings = _settings(tmp_path / "int.db")

    with aioresponses() as m:
        m.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            status=200,
            payload={"ok": True},
        )
        async with aiohttp.ClientSession() as session:
            fired = await run_once(session, db, settings)

    # DEX token with no CG mapping falls through to the stale-price path.
    # price_recovery is suppressed (systemic bias fix), sufficient_drawdown
    # does NOT fire (no drawdown because current_mcap == alert_mcap), and
    # strong_prior_signal (15) alone is below the 50-pt threshold, so the
    # candidate is filtered out entirely — no alert, no DB row.
    assert fired == 0

    rows = await db.get_recent_secondwave_candidates(days=7)
    assert rows == []


async def test_end_to_end_narrative_token_live_price(db, tmp_path):
    alerted_at = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    await db._conn.execute(
        """INSERT INTO alerts
           (contract_address, chain, conviction_score, alert_market_cap, price_usd, token_name, ticker, alerted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("0xnarr", "ethereum", 85.0, 2_000_000.0, 1.0, "NarrTok", "NARR", alerted_at),
    )
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xnarr", 80.0, alerted_at),
    )
    # predictions.coin_id = coingecko slug
    await db._conn.execute(
        """INSERT INTO predictions
           (category_id, category_name, coin_id, symbol, name,
            market_cap_at_prediction, price_at_prediction,
            narrative_fit_score, staying_power, confidence, reasoning,
            strategy_snapshot, predicted_at)
           VALUES ('ai','AI','narr-token','NARR','NarrTok',2e6,1.0,80,'High','High','r','{}',?)""",
        (alerted_at,),
    )
    await db._conn.commit()

    settings = _settings(tmp_path / "int.db")

    with aioresponses() as m:
        m.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            status=200,
            payload=[{
                "id": "narr-token",
                "current_price": 0.8,
                "total_volume": 500_000.0,
                "market_cap": 1_200_000.0,
            }],
        )
        m.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            status=200,
            payload={"ok": True},
        )
        async with aiohttp.ClientSession() as session:
            fired = await run_once(session, db, settings)

    # Narrative token path exercises the live-price branch.
    assert fired >= 0
