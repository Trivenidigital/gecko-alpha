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
        "cryptopanic_enabled=",
        "live_trading_enabled=",
        "ingest_watchdog_enabled=",
        "counter_enabled=",
    ]
    missing = [kw for kw in expected_kwargs if kw not in src]
    assert (
        not missing
    ), "pipeline_config_resolved missing expected kwargs: " + ", ".join(missing)


def test_pipeline_config_resolved_does_not_log_secrets():
    """Static guard via AST: never log SECRET / TOKEN / API_KEY fields.

    Walks the AST for the pipeline_config_resolved Call node and checks
    every kwarg name + every string-literal value for forbidden
    substrings. Future kwarg additions cannot accidentally leak secrets.
    """
    import ast
    from pathlib import Path

    src = Path(scout_main.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    # Find the specific logger.info("pipeline_config_resolved", ...) call.
    target: ast.Call | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            first = node.args[0]
            if (
                isinstance(first, ast.Constant)
                and first.value == "pipeline_config_resolved"
            ):
                target = node
                break
    assert target is not None, "pipeline_config_resolved log call not found"

    forbidden_substrings = ["TOKEN", "SECRET", "API_KEY", "PASSWORD", "PRIVATE"]
    offenders: list[str] = []
    for kw in target.keywords:
        kw_name = (kw.arg or "").upper()
        for needle in forbidden_substrings:
            if needle in kw_name:
                offenders.append(f"kwarg name {kw.arg!r}")
        # Also check string-literal values inside getattr() calls.
        if isinstance(kw.value, ast.Call):
            for sub in ast.walk(kw.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    for needle in forbidden_substrings:
                        if needle in sub.value.upper():
                            offenders.append(f"literal {sub.value!r} in {kw.arg!r}")
    assert (
        not offenders
    ), "pipeline_config_resolved kwargs reference secret-shaped names: " + ", ".join(
        offenders
    )
