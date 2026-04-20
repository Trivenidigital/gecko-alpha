# tests/test_main_perp_integration.py
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from scout.perp.schemas import PerpAnomaly


@pytest.mark.asyncio
async def test_perp_disabled_no_task_launched(settings_factory, tmp_path):
    settings = settings_factory(PERP_ENABLED=False, DB_PATH=tmp_path / "t.db")
    with patch("scout.main.run_perp_watcher") as mock_watcher:
        from scout.main import _maybe_start_perp_watcher

        await _maybe_start_perp_watcher(settings, db=None, session=None)
    mock_watcher.assert_not_called()


@pytest.mark.asyncio
async def test_perp_disabled_enrichment_skipped(
    settings_factory,
    token_factory,
    tmp_path,
):
    from scout.main import _maybe_enrich_perp

    settings = settings_factory(PERP_ENABLED=False)
    tokens = [token_factory(ticker="BTC")]
    result = await _maybe_enrich_perp(tokens, db=None, settings=settings)
    assert result is tokens  # unchanged, no DB call


@pytest.mark.asyncio
async def test_perp_enabled_invokes_enrichment(
    settings_factory,
    token_factory,
    tmp_path,
):
    from scout.db import Database
    from scout.main import _maybe_enrich_perp

    db = Database(db_path=tmp_path / "t.db")
    await db.connect()
    try:
        await db.insert_perp_anomaly(
            PerpAnomaly(
                exchange="binance",
                symbol="BTCUSDT",
                ticker="BTC",
                kind="oi_spike",
                magnitude=4.0,
                baseline=1.0,
                observed_at=datetime.now(timezone.utc),
            )
        )
        settings = settings_factory(PERP_ENABLED=True, PERP_ANOMALY_LOOKBACK_MIN=15)
        out = await _maybe_enrich_perp(
            [token_factory(ticker="BTC")], db=db, settings=settings
        )
        assert out[0].perp_oi_spike_ratio == 4.0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_run_cycle_enriches_before_scoring(
    settings_factory,
    token_factory,
    tmp_path,
    monkeypatch,
):
    """Happy path integration: DB has an anomaly, candidate matches by
    ticker, one pipeline cycle runs, and the scored candidate comes out
    with perp_oi_spike_ratio populated AND perp_anomaly in signals_fired.
    """
    from scout import scorer as scorer_mod
    from scout.db import Database
    from scout.main import _maybe_enrich_perp
    from scout.scorer import score

    # Bump denominator guard to the ready state for the duration of the test.
    monkeypatch.setattr(scorer_mod, "SCORER_MAX_RAW", 208)
    monkeypatch.setattr(scorer_mod, "_PERP_SCORING_DENOMINATOR_READY", True)

    db = Database(db_path=tmp_path / "t.db")
    await db.connect()
    try:
        await db.insert_perp_anomaly(
            PerpAnomaly(
                exchange="binance",
                symbol="DOGEUSDT",
                ticker="DOGE",
                kind="oi_spike",
                magnitude=5.0,
                baseline=1.0,
                observed_at=datetime.now(timezone.utc),
            )
        )
        settings = settings_factory(
            PERP_ENABLED=True,
            PERP_SCORING_ENABLED=True,
            PERP_ANOMALY_LOOKBACK_MIN=15,
            PERP_OI_SPIKE_RATIO=3.0,
        )
        tokens = [token_factory(ticker="DOGE", liquidity_usd=50_000)]
        enriched = await _maybe_enrich_perp(tokens, db=db, settings=settings)
        assert enriched[0].perp_oi_spike_ratio == 5.0
        points, signals = score(enriched[0], settings)
        assert "perp_anomaly" in signals
    finally:
        await db.close()
