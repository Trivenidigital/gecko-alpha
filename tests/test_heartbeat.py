"""Tests for BL-033 periodic heartbeat logging."""

from datetime import datetime, timedelta, timezone

import structlog

from scout import main as main_module
from scout.main import _heartbeat_stats, _maybe_emit_heartbeat, _reset_heartbeat_stats


class _FakeSettings:
    HEARTBEAT_INTERVAL_SECONDS = 300


def _capture_logs(monkeypatch):
    """Replace module logger with a capturing list."""
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


def test_first_call_seeds_state_without_logging(monkeypatch):
    _reset_heartbeat_stats()
    captured = _capture_logs(monkeypatch)

    emitted = _maybe_emit_heartbeat(_FakeSettings())

    assert emitted is False
    assert _heartbeat_stats["started_at"] is not None
    assert _heartbeat_stats["last_heartbeat_at"] is not None
    assert captured == []


def test_no_log_before_interval_elapsed(monkeypatch):
    _reset_heartbeat_stats()
    captured = _capture_logs(monkeypatch)

    # Seed
    _maybe_emit_heartbeat(_FakeSettings())
    # Second call immediately after: not enough elapsed
    emitted = _maybe_emit_heartbeat(_FakeSettings())

    assert emitted is False
    assert captured == []


def test_emits_heartbeat_after_interval(monkeypatch):
    _reset_heartbeat_stats()
    captured = _capture_logs(monkeypatch)

    # Seed state with a timestamp 10 minutes in the past
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    _heartbeat_stats["started_at"] = past
    _heartbeat_stats["last_heartbeat_at"] = past
    _heartbeat_stats["tokens_scanned"] = 42
    _heartbeat_stats["candidates_promoted"] = 7
    _heartbeat_stats["alerts_fired"] = 2
    _heartbeat_stats["narrative_predictions"] = 5
    _heartbeat_stats["counter_scores_memecoin"] = 3
    _heartbeat_stats["counter_scores_narrative"] = 4

    emitted = _maybe_emit_heartbeat(_FakeSettings())

    assert emitted is True
    assert len(captured) == 1
    event, payload = captured[0]
    assert event == "heartbeat"
    assert payload["tokens_scanned"] == 42
    assert payload["candidates_promoted"] == 7
    assert payload["alerts_fired"] == 2
    assert payload["narrative_predictions"] == 5
    assert payload["counter_scores_memecoin"] == 3
    assert payload["counter_scores_narrative"] == 4
    assert payload["uptime_minutes"] >= 9.0
    # last_heartbeat_at is advanced
    assert _heartbeat_stats["last_heartbeat_at"] > past


def test_state_preserved_across_calls(monkeypatch):
    _reset_heartbeat_stats()
    _capture_logs(monkeypatch)

    _maybe_emit_heartbeat(_FakeSettings())
    _heartbeat_stats["tokens_scanned"] += 5
    _maybe_emit_heartbeat(_FakeSettings())
    _heartbeat_stats["tokens_scanned"] += 3

    assert _heartbeat_stats["tokens_scanned"] == 8
    assert _heartbeat_stats["started_at"] is not None


def test_custom_interval_respected(monkeypatch):
    _reset_heartbeat_stats()
    captured = _capture_logs(monkeypatch)

    class _FastSettings:
        HEARTBEAT_INTERVAL_SECONDS = 1

    # Seed with timestamp 2 seconds ago
    past = datetime.now(timezone.utc) - timedelta(seconds=2)
    _heartbeat_stats["started_at"] = past
    _heartbeat_stats["last_heartbeat_at"] = past

    emitted = _maybe_emit_heartbeat(_FastSettings())
    assert emitted is True
    assert captured[0][0] == "heartbeat"



def test_memecoin_and_narrative_counters_independent(monkeypatch):
    """Incrementing one counter must not affect the other."""
    _reset_heartbeat_stats()
    _capture_logs(monkeypatch)

    _heartbeat_stats["counter_scores_memecoin"] += 1
    _heartbeat_stats["counter_scores_memecoin"] += 1
    _heartbeat_stats["counter_scores_narrative"] += 5

    assert _heartbeat_stats["counter_scores_memecoin"] == 2
    assert _heartbeat_stats["counter_scores_narrative"] == 5

    # Reset clears both
    _reset_heartbeat_stats()
    assert _heartbeat_stats["counter_scores_memecoin"] == 0
    assert _heartbeat_stats["counter_scores_narrative"] == 0
