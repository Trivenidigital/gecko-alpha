from __future__ import annotations

from datetime import datetime, timezone

from scripts.codex_fleet_telegram_status import (
    EventSummary,
    HostStatus,
    build_fleet_message,
    summarize_github_events,
    window_bounds,
)


def test_window_bounds_uses_main_vps_utc_and_rolling_seven_hours():
    now = datetime(2026, 5, 23, 14, 41, tzinfo=timezone.utc)

    start, end = window_bounds(now, hours=7)

    assert start.isoformat().replace("+00:00", "Z") == "2026-05-23T07:41:00Z"
    assert end.isoformat().replace("+00:00", "Z") == "2026-05-23T14:41:00Z"


def test_summarize_github_events_counts_pr_events_distinct_prs_and_unmatched_branches():
    start = datetime(2026, 5, 23, 7, 41, tzinfo=timezone.utc)
    end = datetime(2026, 5, 23, 14, 41, tzinfo=timezone.utc)
    events_by_repo = {
        "Trivenidigital/gecko-alpha": [
            {
                "type": "PullRequestEvent",
                "created_at": "2026-05-23T08:00:00Z",
                "payload": {
                    "action": "opened",
                    "pull_request": {
                        "number": 181,
                        "head": {"ref": "codex/actionability"},
                    },
                },
            },
            {
                "type": "PullRequestEvent",
                "created_at": "2026-05-23T09:00:00Z",
                "payload": {
                    "action": "synchronize",
                    "pull_request": {
                        "number": 181,
                        "head": {"ref": "codex/actionability"},
                    },
                },
            },
            {
                "type": "PullRequestEvent",
                "created_at": "2026-05-23T10:00:00Z",
                "payload": {
                    "action": "opened",
                    "pull_request": {
                        "number": 182,
                        "head": {"ref": "codex/source-calls"},
                    },
                },
            },
            {
                "type": "PullRequestEvent",
                "created_at": "2026-05-23T11:00:00Z",
                "payload": {
                    "action": "opened",
                    "pull_request": {
                        "number": 183,
                        "head": {"ref": "codex/cockpit"},
                    },
                },
            },
            {
                "type": "PushEvent",
                "created_at": "2026-05-23T12:00:00Z",
                "payload": {"ref": "codex/manual-pr-needed"},
            },
            {
                "type": "PushEvent",
                "created_at": "2026-05-23T12:15:00Z",
                "payload": {"ref": "main"},
            },
            {
                "type": "PullRequestEvent",
                "created_at": "2026-05-23T07:29:00Z",
                "payload": {
                    "action": "opened",
                    "pull_request": {
                        "number": 180,
                        "head": {"ref": "codex/outside-window"},
                    },
                },
            },
        ],
    }

    summary = summarize_github_events(events_by_repo, start, end)

    assert summary.pr_event_count == 4
    assert summary.distinct_prs == ["Trivenidigital/gecko-alpha#181", "Trivenidigital/gecko-alpha#182", "Trivenidigital/gecko-alpha#183"]
    assert summary.unmatched_branch_pushes == ["Trivenidigital/gecko-alpha:codex/manual-pr-needed"]
    assert summary.outside_recent_prs == ["Trivenidigital/gecko-alpha#180 at 2026-05-23T07:29:00Z"]


def test_build_fleet_message_matches_operator_style():
    start = datetime(2026, 5, 23, 7, 41, tzinfo=timezone.utc)
    end = datetime(2026, 5, 23, 14, 41, tzinfo=timezone.utc)
    summary = EventSummary(
        distinct_prs=["Trivenidigital/gecko-alpha#181", "Trivenidigital/gecko-alpha#182", "Trivenidigital/gecko-alpha#183"],
        pr_event_count=4,
        unmatched_branch_pushes=["Trivenidigital/gecko-alpha:codex/manual-pr-needed", "Trivenidigital/gecko-alpha:codex/blocked"],
        blocker_reports=["main-vps: blocker/no-PR report in codex-production-push-loop-main.service"],
        outside_recent_prs=["Trivenidigital/gecko-alpha#180 at 2026-05-23T07:29:00Z"],
        errors=[],
    )
    hosts = [
        HostStatus("main-vps", True, ["hermes-gateway=active", "codex timers=6 active"], []),
        HostStatus("vpin-vps", True, ["hermes-gateway=active", "codex timers=4 active"], []),
        HostStatus("srilu-vps", True, ["hermes-gateway=active", "gecko-pipeline=active"], []),
    ]

    message = build_fleet_message(start, end, summary, hosts)

    assert "In the rolling 7-hour window from Main VPS time (2026-05-23T07:41:00Z to 2026-05-23T14:41:00Z):" in message
    assert "3 distinct PRs delivered: Trivenidigital/gecko-alpha#181, Trivenidigital/gecko-alpha#182, Trivenidigital/gecko-alpha#183." in message
    assert "4 PR opened/updated events total" in message
    assert "2 branches were pushed but PR creation failed/awaits manual PR creation." in message
    assert "1 run produced a blocker/no-PR report." in message
    assert "Trivenidigital/gecko-alpha#180 at 2026-05-23T07:29:00Z was just outside the 7-hour window." in message
    assert "main-vps: OK" in message
    assert "parse_mode" not in message
