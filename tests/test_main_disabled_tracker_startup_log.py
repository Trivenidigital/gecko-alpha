"""A flag-disabled tracker must announce itself once at boot.

`GAINERS_TRACKER_ENABLED=false` (and its `TRENDING_SNAPSHOT_ENABLED` sibling)
gates the tracker's per-cycle work behind a plain `if settings.FLAG:`, so
turning it off produces no "skipped" line — it produces NOTHING. A deliberately
disabled lane and a lane whose loop has died are then the same absence in
journald. One boot-time line discriminates them.

The counterpart to "exactly one line" is "and nothing per cycle": the fix must
not be re-implemented at the per-cycle branch, where it would emit thousands of
identical lines a day.
"""

from __future__ import annotations

import ast
import pathlib

import structlog

from scout.main import log_disabled_trackers


def test_disabled_gainers_tracker_logs_once(settings_factory):
    with structlog.testing.capture_logs() as logs:
        log_disabled_trackers(settings_factory(GAINERS_TRACKER_ENABLED=False))

    entries = [e for e in logs if e.get("event") == "gainers_tracker_disabled_by_flag"]
    assert len(entries) == 1, f"expected exactly one disabled line, got {len(entries)}"
    assert entries[0]["flag"] == "GAINERS_TRACKER_ENABLED"
    assert entries[0]["log_level"] == "info", (
        "the operator has to see this without raising the log level — it is the "
        "only evidence the lane is off by choice"
    )
    assert entries[0]["detail"], "must say what stops running, not just that it is off"


def test_disabled_trending_tracker_logs_once(settings_factory):
    with structlog.testing.capture_logs() as logs:
        log_disabled_trackers(settings_factory(TRENDING_SNAPSHOT_ENABLED=False))

    entries = [e for e in logs if e.get("event") == "trending_tracker_disabled_by_flag"]
    assert len(entries) == 1
    assert entries[0]["flag"] == "TRENDING_SNAPSHOT_ENABLED"


def test_enabled_trackers_log_nothing(settings_factory):
    """The enabled case stays silent — it is already provable from the
    tracker's own per-cycle events, and `pipeline_config_resolved` carries the
    resolved flags either way."""
    with structlog.testing.capture_logs() as logs:
        log_disabled_trackers(
            settings_factory(
                GAINERS_TRACKER_ENABLED=True, TRENDING_SNAPSHOT_ENABLED=True
            )
        )

    assert [e for e in logs if e.get("event", "").endswith("_disabled_by_flag")] == []


def test_both_disabled_emit_one_line_each(settings_factory):
    with structlog.testing.capture_logs() as logs:
        log_disabled_trackers(
            settings_factory(
                GAINERS_TRACKER_ENABLED=False, TRENDING_SNAPSHOT_ENABLED=False
            )
        )

    events = sorted(e["event"] for e in logs if e.get("event", "").endswith("_by_flag"))
    assert events == [
        "gainers_tracker_disabled_by_flag",
        "trending_tracker_disabled_by_flag",
    ]


def test_repeated_calls_do_not_accumulate_state(settings_factory):
    """The helper is a pure function of the settings — "once" is enforced by
    calling it once at boot, not by a module-level latch that a second process
    or a test would silently inherit."""
    settings = settings_factory(GAINERS_TRACKER_ENABLED=False)
    for _ in range(2):
        with structlog.testing.capture_logs() as logs:
            log_disabled_trackers(settings)
        assert (
            len(
                [
                    e
                    for e in logs
                    if e.get("event") == "gainers_tracker_disabled_by_flag"
                ]
            )
            == 1
        )


def test_disabled_line_is_emitted_from_startup_not_from_a_cycle():
    """Static guard: `log_disabled_trackers` must be called from `main`, the
    once-per-process path — never from `run_cycle`, which would emit the same
    line thousands of times a day and re-create the noise this replaced."""
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "scout" / "main.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    callers: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "log_disabled_trackers"
            ):
                callers.append(node.name)

    assert callers == [
        "main"
    ], f"log_disabled_trackers must be called only from main(), found: {callers}"
