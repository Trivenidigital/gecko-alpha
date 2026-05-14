"""Tests for BL-NEW-INGEST-WATCHDOG wiring and alert dispatch."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scout.heartbeat import IngestSourceSample, IngestWatchdogEvent
from scout.main import _dispatch_ingest_watchdog_events, run_cycle
from tests.test_main_cryptopanic_integration import _mk_db, _mk_settings


def _capture_main_logs(monkeypatch):
    from scout import main as main_module

    captured: list[tuple[str, dict]] = []

    class _CapLogger:
        def info(self, event, **kwargs):
            captured.append((event, kwargs))

        def warning(self, event, **kwargs):
            captured.append((event, kwargs))

        def error(self, event, **kwargs):
            captured.append((event, kwargs))

        def exception(self, event, **kwargs):
            captured.append((event, kwargs))

    monkeypatch.setattr(main_module, "logger", _CapLogger())
    return captured


@pytest.mark.asyncio
async def test_dispatch_ingest_watchdog_events_uses_plain_text_telegram(monkeypatch):
    settings = _mk_settings()
    session = AsyncMock()
    sent = []

    async def _fake_send(text, passed_session, passed_settings, **kwargs):
        sent.append((text, passed_session, passed_settings, kwargs))

    monkeypatch.setattr("scout.main.alerter.send_telegram_message", _fake_send)

    await _dispatch_ingest_watchdog_events(
        [
            IngestWatchdogEvent(
                kind="starved",
                source="coingecko:markets",
                consecutive_empty_cycles=5,
                threshold=5,
                last_success_at=None,
                error="no raw data",
            )
        ],
        session,
        settings,
        dry_run=False,
    )

    assert len(sent) == 1
    assert sent[0][1] is session
    assert sent[0][2] is settings
    assert sent[0][3]["parse_mode"] is None
    assert sent[0][3]["raise_on_failure"] is True
    assert "coingecko:markets" in sent[0][0]


@pytest.mark.asyncio
async def test_dispatch_ingest_watchdog_events_dry_run_logs_without_telegram(
    monkeypatch,
):
    settings = _mk_settings()
    session = AsyncMock()
    captured = _capture_main_logs(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr("scout.main.alerter.send_telegram_message", send)

    await _dispatch_ingest_watchdog_events(
        [
            IngestWatchdogEvent(
                kind="recovered",
                source="dexscreener:boosts",
                consecutive_empty_cycles=0,
                threshold=5,
                last_success_at="2026-05-14T18:00:00+00:00",
                error=None,
            )
        ],
        session,
        settings,
        dry_run=True,
    )

    send.assert_not_awaited()
    assert captured[0][0] == "ingest_watchdog_alert_dry_run"


@pytest.mark.asyncio
async def test_dispatch_ingest_watchdog_events_logs_failure_without_crashing(
    monkeypatch,
):
    settings = _mk_settings()
    session = AsyncMock()
    captured = _capture_main_logs(monkeypatch)

    async def _fake_send(*args, **kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr("scout.main.alerter.send_telegram_message", _fake_send)

    await _dispatch_ingest_watchdog_events(
        [
            IngestWatchdogEvent(
                kind="starved",
                source="geckoterminal:ethereum",
                consecutive_empty_cycles=5,
                threshold=5,
                last_success_at=None,
                error="404",
            )
        ],
        session,
        settings,
        dry_run=False,
    )

    assert any(event == "ingest_watchdog_alert_failed" for event, _ in captured)


@pytest.mark.asyncio
async def test_run_cycle_observes_source_samples_and_dispatches_events(monkeypatch):
    db = _mk_db()
    settings = _mk_settings(
        VOLUME_SPIKE_ENABLED=False,
        GAINERS_TRACKER_ENABLED=False,
        LOSERS_TRACKER_ENABLED=False,
        MOMENTUM_7D_ENABLED=False,
        VELOCITY_ALERTS_ENABLED=False,
        COUNTER_ENABLED=False,
    )
    session = MagicMock()
    event = IngestWatchdogEvent(
        kind="starved",
        source="coingecko:markets",
        consecutive_empty_cycles=5,
        threshold=5,
        last_success_at=None,
        error=None,
    )
    observed_batches = []

    def _fake_observe(samples, passed_settings):
        observed_batches.append(list(samples))
        return [event]

    dispatch = AsyncMock()
    monkeypatch.setattr("scout.main.observe_ingest_sources", _fake_observe)
    monkeypatch.setattr("scout.main._dispatch_ingest_watchdog_events", dispatch)
    monkeypatch.setattr(
        "scout.main._dex_module.get_last_watchdog_samples",
        lambda: [IngestSourceSample(source="dexscreener:boosts", raw_count=3)],
    )
    monkeypatch.setattr(
        "scout.main._gt_module.get_last_watchdog_samples",
        lambda: [IngestSourceSample(source="geckoterminal:solana", raw_count=8)],
    )
    monkeypatch.setattr(
        "scout.main._cg_module.get_last_watchdog_samples",
        lambda: [IngestSourceSample(source="coingecko:markets", raw_count=0)],
    )

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
            return_value=[],
        ),
        patch(
            "scout.main.fetch_held_position_prices",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("scout.main.aggregate", return_value=[]),
    ):
        await run_cycle(settings, db, session, dry_run=True)

    assert [sample.source for sample in observed_batches[0]] == [
        "dexscreener:boosts",
        "geckoterminal:solana",
        "coingecko:markets",
    ]
    dispatch.assert_awaited_once_with([event], session, settings, dry_run=True)


def test_cycle_geckoterminal_exception_uses_chain_source_keys():
    from scout.main import _ingest_watchdog_samples_from_cycle

    settings = _mk_settings(CHAINS=["solana", "ethereum"])

    samples = _ingest_watchdog_samples_from_cycle(
        settings=settings,
        dex_error=None,
        gecko_error=RuntimeError("gt exploded"),
        cg_movers_error=None,
        cg_trending_error=None,
        cg_by_volume_error=None,
        cg_midcap_error=None,
    )

    sources = {sample.source: sample for sample in samples}
    assert sources["geckoterminal:solana"].error == "gt exploded"
    assert sources["geckoterminal:ethereum"].error == "gt exploded"
    assert "geckoterminal" not in sources
