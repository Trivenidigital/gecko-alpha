"""Round 17: pipeline_config_resolved log on startup.

Adds a structured ``pipeline_config_resolved`` event right after
``scanner_starting`` (PR #247) so operators see top-level
feature-flag + threshold values for every deploy. Mirrors the existing
``paper_trading_config_resolved`` event that fires later from
TradingEngine init.

Operators can `journalctl -u gecko-pipeline.service | grep
pipeline_config_resolved` to verify a deploy applied expected flags
— catches the silent-flag-drift class of incident.
"""

from __future__ import annotations

import inspect

from scout import main as scout_main


def test_main_emits_pipeline_config_resolved():
    """Static guard: main() must emit the structured config-summary event."""
    src = inspect.getsource(scout_main.main)
    assert '"pipeline_config_resolved"' in src, (
        "scout/main.py main() must emit a 'pipeline_config_resolved' "
        "structured log event with feature-flag + threshold values."
    )


def test_pipeline_config_resolved_includes_top_level_flags():
    """Static guard: critical feature flags must be in the event."""
    src = inspect.getsource(scout_main.main)
    expected_kwargs = [
        "scan_interval_seconds=",
        "heartbeat_interval_seconds=",
        "min_score=",
        "conviction_threshold=",
        "chains_enabled=",
        "narrative_enabled=",
        "secondwave_enabled=",
        "briefing_enabled=",
        "tg_social_enabled=",
        "lunarcrush_enabled=",
        "cryptopanic_enabled=",
        "live_trading_enabled=",
        "ingest_watchdog_enabled=",
        "counter_enabled=",
    ]
    missing = [kw for kw in expected_kwargs if kw not in src]
    assert not missing, (
        "pipeline_config_resolved missing expected kwargs: "
        + ", ".join(missing)
    )


def test_pipeline_config_resolved_does_not_log_secrets():
    """Static guard: never log SECRET / TOKEN / API_KEY fields."""
    src = inspect.getsource(scout_main.main)
    # Find the pipeline_config_resolved block.
    start = src.find('"pipeline_config_resolved"')
    assert start != -1
    # Block runs to next `logger.info(` or to end of function.
    block_end = src.find("logger.info", start + 10)
    block = src[start : block_end if block_end > 0 else start + 2000]

    forbidden_substrings = ["TOKEN", "SECRET", "API_KEY", "PASSWORD", "PRIVATE"]
    for needle in forbidden_substrings:
        assert needle not in block.upper(), (
            f"pipeline_config_resolved block references {needle!r} — "
            "config-summary log must NEVER include secret fields"
        )
