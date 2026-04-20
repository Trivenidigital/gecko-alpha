# tests/test_perp_enrichment.py
import pytest
from datetime import datetime, timezone, timedelta
from scout.db import Database
from scout.perp.enrichment import enrich_candidates_with_perp_anomalies
from scout.perp.schemas import PerpAnomaly


@pytest.fixture
async def db(tmp_path):
    database = Database(db_path=tmp_path / "t.db")
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_enrich_matches_by_ticker_case_insensitive(
    db, token_factory, settings_factory
):
    settings = settings_factory(PERP_ANOMALY_LOOKBACK_MIN=15)
    now = datetime.now(timezone.utc)
    await db.insert_perp_anomaly(
        PerpAnomaly(
            exchange="binance",
            symbol="DOGEUSDT",
            ticker="DOGE",
            kind="oi_spike",
            magnitude=4.5,
            baseline=1.0,
            observed_at=now,
        )
    )
    tokens = [
        token_factory(ticker="doge"),
        token_factory(ticker="SHIB"),
    ]
    enriched = await enrich_candidates_with_perp_anomalies(tokens, db, settings)
    assert enriched[0].perp_oi_spike_ratio == 4.5
    assert enriched[0].perp_exchange == "binance"
    assert enriched[0].perp_last_anomaly_at is not None
    assert enriched[1].perp_oi_spike_ratio is None


@pytest.mark.asyncio
async def test_enrich_funding_flip_sets_flag(db, token_factory, settings_factory):
    settings = settings_factory(PERP_ANOMALY_LOOKBACK_MIN=15)
    now = datetime.now(timezone.utc)
    await db.insert_perp_anomaly(
        PerpAnomaly(
            exchange="bybit",
            symbol="ETHUSDT",
            ticker="ETH",
            kind="funding_flip",
            magnitude=0.08,
            baseline=0.0001,
            observed_at=now,
        )
    )
    enriched = await enrich_candidates_with_perp_anomalies(
        [token_factory(ticker="ETH")],
        db,
        settings,
    )
    assert enriched[0].perp_funding_flip is True


@pytest.mark.asyncio
async def test_enrich_ignores_old_anomalies(db, token_factory, settings_factory):
    settings = settings_factory(PERP_ANOMALY_LOOKBACK_MIN=15)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    await db.insert_perp_anomaly(
        PerpAnomaly(
            exchange="binance",
            symbol="BTCUSDT",
            ticker="BTC",
            kind="oi_spike",
            magnitude=5.0,
            baseline=1.0,
            observed_at=old,
        )
    )
    enriched = await enrich_candidates_with_perp_anomalies(
        [token_factory(ticker="BTC")],
        db,
        settings,
    )
    assert enriched[0].perp_oi_spike_ratio is None
