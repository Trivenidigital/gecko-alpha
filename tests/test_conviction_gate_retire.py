"""Tests for the conviction-gate RETIREMENT slice (backlog SIG-01 / NAR-01 / ALR-05).

The conviction gate is retired at the flag/callsite level (no code deleted):
`Settings.CONVICTION_GATE_ENABLED` defaults False, and the extracted
`scout.main._run_conviction_gate_and_alert` helper no-ops (emitting one
`conviction_gate_retired` log per cycle) when the flag is off, skipping
gate.evaluate + MiroFish enqueue + send_alert entirely.

CI note: `scout.main` imports aiohttp at module level (as does `tests/test_main.py`),
which on some Windows setups fails collection with the OPENSSL_Uplink error (INF-08).
The tests themselves are pure mock/`capture_logs` unit tests; correctness is
validated by CI on Linux.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from structlog.testing import capture_logs

from scout.main import _run_conviction_gate_and_alert


def test_conviction_gate_disabled_by_default(settings_factory):
    """The gate ships retired: the flag defaults to False."""
    settings = settings_factory()
    assert settings.CONVICTION_GATE_ENABLED is False


async def test_retired_flag_off_skips_gate_and_alert(token_factory):
    """Flag off (default): no gate.evaluate / MiroFish / send_alert; exactly one
    `conviction_gate_retired` marker per cycle (not per candidate)."""
    settings = MagicMock()
    settings.CONVICTION_GATE_ENABLED = False
    scored = [
        (token_factory(quant_score=54), ["vol_liq_ratio"]),
        (token_factory(quant_score=50), ["momentum_ratio"]),
    ]
    stats = {"alerts_fired": 0}
    db = AsyncMock()
    session = AsyncMock()

    with (
        patch("scout.main.evaluate", new_callable=AsyncMock) as mock_eval,
        patch("scout.main.send_alert", new_callable=AsyncMock) as mock_send,
        capture_logs() as logs,
    ):
        await _run_conviction_gate_and_alert(
            scored, db, session, settings, dry_run=False, stats=stats
        )

    # gate.evaluate is the sole entry to MiroFish enqueue — not-called covers both.
    mock_eval.assert_not_called()
    mock_send.assert_not_called()
    db.upsert_candidate.assert_not_awaited()
    assert stats["alerts_fired"] == 0

    retired = [e for e in logs if e["event"] == "conviction_gate_retired"]
    assert len(retired) == 1  # one per cycle, not per candidate
    assert retired[0]["candidates_skipped"] == 2


async def test_flag_on_runs_gate_loop(token_factory):
    """Flag on (regression pin): the loop runs — gate.evaluate is called. A
    below-threshold verdict short-circuits before send_alert."""
    settings = MagicMock()
    settings.CONVICTION_GATE_ENABLED = True
    settings.CONVICTION_THRESHOLD = 75
    settings.COUNTER_ENABLED = False
    token = token_factory(quant_score=40, conviction_score=40.0)
    scored = [(token, ["vol_liq_ratio"])]
    stats = {"alerts_fired": 0}
    db = AsyncMock()
    session = AsyncMock()

    with (
        patch("scout.main.evaluate", new_callable=AsyncMock) as mock_eval,
        patch("scout.main.send_alert", new_callable=AsyncMock) as mock_send,
        patch("scout.main.safe_emit", new_callable=AsyncMock),
        capture_logs() as logs,
    ):
        mock_eval.return_value = (False, 40.0, token)
        await _run_conviction_gate_and_alert(
            scored, db, session, settings, dry_run=False, stats=stats
        )

    mock_eval.assert_awaited_once()
    db.upsert_candidate.assert_awaited_once()
    mock_send.assert_not_called()  # below threshold -> no alert
    assert stats["alerts_fired"] == 0
    assert not [e for e in logs if e["event"] == "conviction_gate_retired"]


async def test_flag_on_fires_alert_on_should_alert(token_factory):
    """Flag on, full path intact: an above-threshold, safe, non-duplicate token
    reaches send_alert and increments alerts_fired."""
    settings = MagicMock()
    settings.CONVICTION_GATE_ENABLED = True
    settings.CONVICTION_THRESHOLD = 75
    settings.COUNTER_ENABLED = False
    token = token_factory(quant_score=80, conviction_score=80.0)
    scored = [(token, ["vol_liq_ratio"])]
    stats = {"alerts_fired": 0}
    db = AsyncMock()
    db.was_recently_alerted = AsyncMock(return_value=False)
    session = AsyncMock()

    with (
        patch("scout.main.evaluate", new_callable=AsyncMock) as mock_eval,
        patch("scout.main.is_safe", new_callable=AsyncMock, return_value=True),
        patch("scout.main.send_alert", new_callable=AsyncMock) as mock_send,
        patch("scout.main.safe_emit", new_callable=AsyncMock),
        patch("scout.main.record_emission", new_callable=AsyncMock),
        patch(
            "scout.main.price_and_age_from_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
    ):
        mock_eval.return_value = (True, 80.0, token)
        await _run_conviction_gate_and_alert(
            scored, db, session, settings, dry_run=False, stats=stats
        )

    mock_eval.assert_awaited_once()
    mock_send.assert_awaited_once()
    db.log_alert.assert_awaited_once()
    assert stats["alerts_fired"] == 1
