# tests/test_perp_db.py
import pytest
from datetime import datetime, timezone, timedelta
from scout.db import Database
from scout.perp.schemas import PerpAnomaly


@pytest.fixture
async def db(tmp_path):
    path = tmp_path / "test.db"
    database = Database(db_path=path)
    await database.connect()
    yield database
    await database.close()


def _anomaly(
    ticker: str = "BTC",
    *,
    observed_at: datetime | None = None,
    kind: str = "oi_spike",
    exchange: str = "binance",
) -> PerpAnomaly:
    return PerpAnomaly(
        exchange=exchange,
        symbol=f"{ticker}USDT",
        ticker=ticker,
        kind=kind,
        magnitude=3.5,
        baseline=100.0,
        observed_at=observed_at or datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_insert_and_fetch_recent(db):
    a = _anomaly("BTC")
    await db.insert_perp_anomaly(a)
    since = datetime.now(timezone.utc) - timedelta(minutes=15)
    rows = await db.fetch_recent_perp_anomalies(tickers=["BTC"], since=since)
    assert len(rows) == 1 and rows[0].ticker == "BTC"


@pytest.mark.asyncio
async def test_batch_insert_is_atomic(db):
    batch = [_anomaly(t) for t in ("A", "B", "C")]
    inserted = await db.insert_perp_anomalies_batch(batch)
    assert inserted == 3
    rows = await db.fetch_recent_perp_anomalies(
        tickers=["A", "B", "C"],
        since=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert {r.ticker for r in rows} == {"A", "B", "C"}


@pytest.mark.asyncio
async def test_fetch_filters_by_ticker_and_time(db):
    await db.insert_perp_anomaly(_anomaly("BTC"))
    await db.insert_perp_anomaly(_anomaly("ETH"))
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    rows = await db.fetch_recent_perp_anomalies(tickers=["BTC"], since=since)
    assert {r.ticker for r in rows} == {"BTC"}


@pytest.mark.asyncio
async def test_fetch_empty_ticker_list_returns_empty(db):
    await db.insert_perp_anomaly(_anomaly("BTC"))
    rows = await db.fetch_recent_perp_anomalies(
        tickers=[],
        since=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert rows == []


@pytest.mark.asyncio
async def test_prune(db):
    old = datetime.now(timezone.utc) - timedelta(days=30)
    fresh = datetime.now(timezone.utc)
    await db.insert_perp_anomaly(_anomaly("OLD", observed_at=old))
    await db.insert_perp_anomaly(_anomaly("FRESH", observed_at=fresh))
    pruned = await db.prune_perp_anomalies(keep_days=7)
    assert pruned == 1
    rows = await db.fetch_recent_perp_anomalies(
        tickers=["OLD", "FRESH"],
        since=old - timedelta(days=1),
    )
    assert [r.ticker for r in rows] == ["FRESH"]


@pytest.mark.asyncio
async def test_insert_perp_anomaly_idempotent(db):
    """Same (exchange, symbol, kind, observed_at) inserted twice must yield
    exactly ONE row (UNIQUE + INSERT OR IGNORE -- replays on reconnect
    must not create duplicate rows).
    """
    ts = datetime.now(timezone.utc)
    a = _anomaly("BTC", observed_at=ts)
    await db.insert_perp_anomaly(a)
    await db.insert_perp_anomaly(a)  # exact duplicate
    rows = await db.fetch_recent_perp_anomalies(
        tickers=["BTC"],
        since=ts - timedelta(minutes=1),
    )
    assert len(rows) == 1
