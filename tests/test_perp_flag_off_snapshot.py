"""Provability test: shadow mode (PERP_ENABLED=true, SCORING=false) MUST
produce byte-identical scorer output to fully-disabled mode. This is the
contract that lets operators flip PERP_ENABLED=true in production without
affecting scoring.

Unlike a naive direct-score comparison, this test goes through the FULL
enrichment path with a populated DB, so a regression where enrichment
writes different CandidateToken fields under PERP_ENABLED=true would be
caught here (BLOCKER-7).
"""

import pytest
from datetime import datetime, timezone
from scout import scorer as scorer_mod
from scout.db import Database
from scout.main import _maybe_enrich_perp
from scout.perp.schemas import PerpAnomaly
from scout.scorer import score


def _corpus(token_factory):
    return [
        token_factory(ticker="BTC", liquidity_usd=50_000),
        token_factory(ticker="DOGE", liquidity_usd=50_000),
        token_factory(ticker="PEPE", liquidity_usd=50_000),
    ]


@pytest.mark.asyncio
async def test_shadow_mode_scorer_is_byte_identical_to_disabled(
    token_factory,
    settings_factory,
    tmp_path,
):
    assert scorer_mod.SCORER_MAX_RAW < 203, (
        "This test asserts flag-off behavior with denominator guard active. "
        "When recalibration PR bumps SCORER_MAX_RAW to 203, re-run this test "
        "comparing PERP_SCORING_ENABLED=false vs true instead."
    )
    db = Database(db_path=tmp_path / "t.db")
    await db.connect()
    try:
        now = datetime.now(timezone.utc)
        await db.insert_perp_anomalies_batch(
            [
                PerpAnomaly(
                    exchange="binance",
                    symbol="BTCUSDT",
                    ticker="BTC",
                    kind="oi_spike",
                    magnitude=5.0,
                    baseline=1.0,
                    observed_at=now,
                ),
                PerpAnomaly(
                    exchange="bybit",
                    symbol="DOGEUSDT",
                    ticker="DOGE",
                    kind="funding_flip",
                    magnitude=0.1,
                    baseline=0.0001,
                    observed_at=now,
                ),
            ]
        )
        disabled = settings_factory(
            PERP_ENABLED=False,
            PERP_SCORING_ENABLED=False,
            PERP_ANOMALY_LOOKBACK_MIN=15,
        )
        shadow = settings_factory(
            PERP_ENABLED=True,
            PERP_SCORING_ENABLED=False,
            PERP_ANOMALY_LOOKBACK_MIN=15,
        )
        disabled_tokens = await _maybe_enrich_perp(
            _corpus(token_factory), db=db, settings=disabled
        )
        shadow_tokens = await _maybe_enrich_perp(
            _corpus(token_factory), db=db, settings=shadow
        )
        disabled_out = [
            (pts, tuple(sorted(sig)))
            for pts, sig in (score(t, disabled) for t in disabled_tokens)
        ]
        shadow_out = [
            (pts, tuple(sorted(sig)))
            for pts, sig in (score(t, shadow) for t in shadow_tokens)
        ]
        assert disabled_out == shadow_out
    finally:
        await db.close()
