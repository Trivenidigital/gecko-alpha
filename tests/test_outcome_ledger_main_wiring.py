"""scout.main wiring for the signal outcome ledger (P0, edge-audit).

Verifies the candidate-alert emission site (run_cycle) and the hourly
labeler hook (_run_hourly_maintenance). Lives separately from
tests/test_outcome_ledger.py because importing scout.main pulls aiohttp,
which aborts on Windows dev boxes (OPENSSL_Applink); CI runs it on Linux.

Mirrors the GA-05 test scaffolding in tests/test_main.py.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scout.main import _run_hourly_maintenance, run_cycle
from scout.models import CandidateToken


@pytest.fixture
def mock_settings():
    with patch("scout.main.Settings") as MockSettings:
        settings = MagicMock()
        settings.SCAN_INTERVAL_SECONDS = 60
        settings.MIN_SCORE = 60
        settings.DB_PATH = ":memory:"
        settings.PERP_ENABLED = False
        settings.LEDGER_ENABLED = True
        MockSettings.return_value = settings
        yield settings


@pytest.fixture
def mock_db():
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


def _token() -> CandidateToken:
    return CandidateToken(
        contract_address="0xtest",
        chain="solana",
        token_name="Test",
        ticker="TST",
        token_age_days=1,
        market_cap_usd=50000,
        liquidity_usd=10000,
        volume_24h_usd=80000,
        holder_count=100,
        holder_growth_1h=25,
    )


def _enter_cycle_patches(stack: ExitStack, token, send_alert_cm):
    """Enter the standard run_cycle pipeline patches (test_main.py parity)."""
    for cm in (
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
            return_value=(True, 78.0, token),
        ),
        patch("scout.main.is_safe", new_callable=AsyncMock, return_value=True),
    ):
        stack.enter_context(cm)
    return stack.enter_context(send_alert_cm)


async def test_delivered_alert_records_ledger_emission(
    mock_db, mock_session, mock_settings
):
    """The ledger record fires AFTER confirmed delivery, with the candidate's
    liquidity and a price_cache-resolved anchor."""
    token = _token()
    with ExitStack() as stack:
        _enter_cycle_patches(
            stack, token, patch("scout.main.send_alert", new_callable=AsyncMock)
        )
        mock_record = stack.enter_context(
            patch("scout.main.record_emission", new_callable=AsyncMock)
        )
        stack.enter_context(
            patch(
                "scout.main.price_and_age_from_cache",
                new_callable=AsyncMock,
                return_value=(0.5, 42.0),
            )
        )
        stats = await run_cycle(mock_settings, mock_db, mock_session, dry_run=False)

    assert stats["alerts_fired"] == 1
    mock_record.assert_awaited_once()
    kwargs = mock_record.await_args.kwargs
    assert kwargs["kind"] == "alert"
    assert kwargs["token_id"] == "0xtest"
    assert kwargs["surface"] == "candidate_alert"
    assert kwargs["price"] == 0.5
    # c1: the cache-resolved anchor's age at emission rides along.
    assert kwargs["anchor_cache_age_seconds"] == pytest.approx(42.0)
    assert kwargs["liquidity"] == pytest.approx(10000.0)
    assert kwargs["liquidity_source"] == "candidate"
    assert kwargs["gate_verdicts"]["conviction_score"] == pytest.approx(78.0)


async def test_failed_send_records_nothing(mock_db, mock_session, mock_settings):
    """GA-05 parity: a failed delivery writes neither the alerts row nor a
    ledger row."""
    from scout.exceptions import AlertDeliveryError

    token = _token()
    with ExitStack() as stack:
        _enter_cycle_patches(
            stack,
            token,
            patch(
                "scout.main.send_alert",
                new_callable=AsyncMock,
                side_effect=AlertDeliveryError("Telegram send failed: 502"),
            ),
        )
        mock_record = stack.enter_context(
            patch("scout.main.record_emission", new_callable=AsyncMock)
        )
        stats = await run_cycle(mock_settings, mock_db, mock_session, dry_run=False)

    assert stats["alerts_fired"] == 0
    mock_record.assert_not_awaited()


async def test_ledger_failure_does_not_break_alert_path(
    mock_db, mock_session, mock_settings
):
    """Belt-and-braces: even a raising record_emission leaves the alert
    delivered + claimed (host path unaffected)."""
    token = _token()
    with ExitStack() as stack:
        _enter_cycle_patches(
            stack, token, patch("scout.main.send_alert", new_callable=AsyncMock)
        )
        stack.enter_context(
            patch(
                "scout.main.record_emission",
                new_callable=AsyncMock,
                side_effect=RuntimeError("ledger exploded"),
            )
        )
        stack.enter_context(
            patch(
                "scout.main.price_and_age_from_cache",
                new_callable=AsyncMock,
                return_value=(None, None),
            )
        )
        stats = await run_cycle(mock_settings, mock_db, mock_session, dry_run=False)

    assert stats["alerts_fired"] == 1
    mock_db.log_alert.assert_called_once()


async def test_hourly_maintenance_invokes_labeler(mock_db, mock_session):
    """label_pending runs inside the hourly pass (fail-soft, always called)."""
    settings = MagicMock()
    settings.DB_PATH = MagicMock()
    settings.DB_PATH.exists.return_value = False
    settings.CRYPTOPANIC_ENABLED = False
    settings.SQLITE_WAL_PROFILE_ENABLED = False
    settings.SQLITE_WAL_CHECKPOINT_ENABLED = False
    settings.SQLITE_INCREMENTAL_VACUUM_ENABLED = False
    settings.SQLITE_STALE_READER_WATCHDOG_ENABLED = False
    settings.CONVICTION_PROSPECTIVE_ENABLED = False
    settings.DEX_INSTRUMENTATION_ENABLED = False
    settings.NARRATIVE_ENABLED = False
    mock_db.get_unchecked_alerts = AsyncMock(return_value=[])

    logger = MagicMock()
    with patch(
        "scout.main.label_pending", new_callable=AsyncMock, return_value={}
    ) as mock_label:
        await _run_hourly_maintenance(mock_db, mock_session, settings, logger)

    mock_label.assert_awaited_once_with(mock_db, settings)


async def test_hourly_maintenance_runs_the_recompute_coverage_probe(
    mock_db, mock_session
):
    """The probe must actually be CALLED from the hourly pass.

    Without this, deleting the probe call -- or just the one line that
    escalates -- leaves the entire suite green. Review demonstrated exactly
    that: two mutants, `drop the logger.error` and `drop the call from
    main.py`, both survived every test written for the probe. The probe's whole
    value is that one branch, so the branch is what has to be pinned.
    """
    settings = MagicMock()
    settings.DB_PATH = MagicMock()
    settings.DB_PATH.exists.return_value = False
    settings.CRYPTOPANIC_ENABLED = False
    settings.SQLITE_WAL_PROFILE_ENABLED = False
    settings.SQLITE_WAL_CHECKPOINT_ENABLED = False
    settings.SQLITE_INCREMENTAL_VACUUM_ENABLED = False
    settings.SQLITE_STALE_READER_WATCHDOG_ENABLED = False
    settings.CONVICTION_PROSPECTIVE_ENABLED = False
    settings.DEX_INSTRUMENTATION_ENABLED = False
    settings.NARRATIVE_ENABLED = False
    mock_db.get_unchecked_alerts = AsyncMock(return_value=[])
    mock_db.chain_identity_recompute_coverage_probe = AsyncMock(
        return_value={
            "overlay_rows": 0,
            "credit_bearing_legacy_rows": 7,
            "credit_recovered": 0,
            "uncovered": 7,
            "not_recovering": True,
            "per_surface": {},
        }
    )

    logger = MagicMock()
    with patch("scout.main.label_pending", new_callable=AsyncMock, return_value={}):
        await _run_hourly_maintenance(mock_db, mock_session, settings, logger)

    mock_db.chain_identity_recompute_coverage_probe.assert_awaited_once()

    # And the ESCALATION fired -- not merely the unconditional info line.
    escalations = [
        c
        for c in logger.error.call_args_list
        if c.args and c.args[0] == "chain_identity_recompute_NOT_RECOVERING"
    ]
    assert len(escalations) == 1, (
        f"expected exactly one NOT_RECOVERING escalation, got "
        f"{[c.args[0] for c in logger.error.call_args_list if c.args]}"
    )


async def test_hourly_maintenance_does_NOT_escalate_when_credit_is_recovering(
    mock_db, mock_session
):
    """The other half: a healthy overlay must not page.

    Asserting only that the error fires would pass just as happily if the code
    escalated unconditionally, which is the shape that gets an alarm muted.
    """
    settings = MagicMock()
    settings.DB_PATH = MagicMock()
    settings.DB_PATH.exists.return_value = False
    settings.CRYPTOPANIC_ENABLED = False
    settings.SQLITE_WAL_PROFILE_ENABLED = False
    settings.SQLITE_WAL_CHECKPOINT_ENABLED = False
    settings.SQLITE_INCREMENTAL_VACUUM_ENABLED = False
    settings.SQLITE_STALE_READER_WATCHDOG_ENABLED = False
    settings.CONVICTION_PROSPECTIVE_ENABLED = False
    settings.DEX_INSTRUMENTATION_ENABLED = False
    settings.NARRATIVE_ENABLED = False
    mock_db.get_unchecked_alerts = AsyncMock(return_value=[])
    mock_db.chain_identity_recompute_coverage_probe = AsyncMock(
        return_value={
            "overlay_rows": 900,
            "credit_bearing_legacy_rows": 1000,
            "credit_recovered": 540,
            "uncovered": 460,
            "not_recovering": False,
            "per_surface": {},
        }
    )

    logger = MagicMock()
    with patch("scout.main.label_pending", new_callable=AsyncMock, return_value={}):
        await _run_hourly_maintenance(mock_db, mock_session, settings, logger)

    assert not [
        c
        for c in logger.error.call_args_list
        if c.args and c.args[0] == "chain_identity_recompute_NOT_RECOVERING"
    ], "partial coverage paged; that is how an alarm gets muted"
