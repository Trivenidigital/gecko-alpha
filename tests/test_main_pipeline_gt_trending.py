"""Integration test: gt_trending signal propagates through run_cycle (BL-052).

Patches only the ingestion layer + side-effect stages (enrich, is_safe,
evaluate, send_alert). The aggregator and scorer run for real so we verify
that a token with gt_trending_rank=1 exits the scorer with "gt_trending"
in signals_fired.
"""

from unittest.mock import AsyncMock, patch

import pytest

from scout.main import run_cycle
from scout.models import CandidateToken

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    """Minimal async DB mock — mirrors test_main.py's mock_db fixture."""
    db = AsyncMock()
    db.initialize = AsyncMock()
    db.close = AsyncMock()
    db.upsert_candidate = AsyncMock()
    db.log_alert = AsyncMock()
    db.get_daily_mirofish_count = AsyncMock(return_value=0)
    db.get_daily_alert_count = AsyncMock(return_value=0)
    db.get_previous_holder_count = AsyncMock(return_value=None)
    db.log_holder_snapshot = AsyncMock()
    db.log_score = AsyncMock()
    db.get_recent_scores = AsyncMock(return_value=[])
    db.get_vol_7d_avg = AsyncMock(return_value=None)
    db.log_volume_snapshot = AsyncMock()
    db.was_recently_alerted = AsyncMock(return_value=False)
    return db


@pytest.fixture
def mock_session():
    return AsyncMock()


@pytest.fixture
def trending_token():
    """A GeckoTerminal trending token with gt_trending_rank=1.

    liquidity_usd=20_000 is above MIN_LIQUIDITY_USD (15_000) so it passes
    the hard disqualifier in the scorer.
    market_cap_usd=50_000 falls in the 10k-100k tier (Signal 2 fires).
    chain="solana" triggers the solana_bonus (Signal 11).
    Together with gt_trending (Signal 10) the token accumulates enough raw
    points to produce a non-zero quant_score, proving the real scorer ran.
    """
    return CandidateToken(
        contract_address="0xtarget",
        chain="solana",
        token_name="Target",
        ticker="TGT",
        market_cap_usd=50_000.0,
        liquidity_usd=20_000.0,
        volume_24h_usd=10_000.0,
        holder_count=50,
        holder_growth_1h=0,
        gt_trending_rank=1,
    )


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


async def test_gt_trending_signal_propagates_through_run_cycle(
    mock_db, mock_session, settings_factory, trending_token
):
    """End-to-end: a GT-ranked token enters run_cycle and "gt_trending" fires.

    The real aggregate() and score() functions execute; only ingestion
    sources and expensive I/O side-effects are patched.
    """
    # Use a real Settings instance so numeric comparisons inside score()
    # (e.g. token.gt_trending_rank <= settings.GT_TRENDING_TOP_N) work
    # correctly. MagicMock cannot be compared with integers.
    settings = settings_factory(
        MIN_SCORE=1,  # ensure the token is promoted even with a low score
    )

    with (
        patch("scout.main.fetch_trending", new_callable=AsyncMock, return_value=[]),
        patch(
            "scout.main.fetch_trending_pools",
            new_callable=AsyncMock,
            return_value=[trending_token],
        ),
        patch(
            "scout.main.cg_fetch_top_movers", new_callable=AsyncMock, return_value=[]
        ),
        patch("scout.main.cg_fetch_trending", new_callable=AsyncMock, return_value=[]),
        patch("scout.main.cg_fetch_by_volume", new_callable=AsyncMock, return_value=[]),
        # enrich_holders: identity pass-through (return the token unchanged)
        patch(
            "scout.main.enrich_holders",
            new_callable=AsyncMock,
            side_effect=lambda t, s, st: t,
        ),
        # evaluate/gate: approve the token so it advances past the gate
        patch(
            "scout.main.evaluate",
            new_callable=AsyncMock,
            return_value=(True, 80.0, trending_token),
        ),
        patch("scout.main.is_safe", new_callable=AsyncMock, return_value=True),
        patch("scout.main.send_alert", new_callable=AsyncMock),
    ):
        await run_cycle(settings, mock_db, mock_session, dry_run=True)

    # Find the upsert_candidate call that carried our token (identified by
    # contract_address). run_cycle calls upsert_candidate twice per token:
    # once after scoring and once after evaluate(). We inspect every call.
    all_upserted_tokens = [
        call.args[0]
        for call in mock_db.upsert_candidate.call_args_list
        if call.args
        and hasattr(call.args[0], "contract_address")
        and call.args[0].contract_address == "0xtarget"
    ]

    assert all_upserted_tokens, (
        "upsert_candidate was never called with contract_address='0xtarget' — "
        "the token did not enter the scoring stage"
    )

    # The first upsert (post-score) carries signals_fired populated by the
    # real scorer. At least one of the upserted token copies must list
    # "gt_trending" in signals_fired.
    signals_across_calls = [
        t.signals_fired for t in all_upserted_tokens if t.signals_fired is not None
    ]

    assert (
        signals_across_calls
    ), "signals_fired was None on all upserted token copies — scorer did not run"

    any_gt_trending = any("gt_trending" in signals for signals in signals_across_calls)
    assert any_gt_trending, (
        f"'gt_trending' not found in signals_fired. "
        f"Signals observed: {signals_across_calls}"
    )
