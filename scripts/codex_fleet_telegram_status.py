#!/usr/bin/env python3
"""Fleet Telegram status digest for Codex/Hermes VPS automation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib import request


DEFAULT_REPOS = (
    "Trivenidigital/shift-agent",
    "Trivenidigital/ApexAgent",
    "Trivenidigital/gecko-alpha",
)
DEFAULT_HOSTS = (
    ("main-vps", "local"),
    ("vpin-vps", "vpin-brief"),
    ("srilu-vps", "srilu-brief"),
)
DEFAULT_ENV = Path("/etc/codex-telegram.env")


@dataclass
class EventSummary:
    distinct_prs: list[str]
    pr_event_count: int
    unmatched_branch_pushes: list[str]
    blocker_reports: list[str]
    outside_recent_prs: list[str]
    errors: list[str]


@dataclass
class HostStatus:
    name: str
    ok: bool
    details: list[str]
    errors: list[str]


def parse_time(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fmt_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def window_bounds(now: datetime | None = None, hours: int = 7) -> tuple[datetime, datetime]:
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = end.replace(microsecond=0)
    return end - timedelta(hours=hours), end


def _event_in_window(event: dict, start: datetime, end: datetime) -> bool:
    created = event.get("created_at")
    if not created:
        return False
    ts = parse_time(created)
    return start <= ts <= end


def _event_before_window_but_recent(event: dict, start: datetime) -> bool:
    created = event.get("created_at")
    if not created:
        return False
    ts = parse_time(created)
    return start - timedelta(minutes=20) <= ts < start


def summarize_github_events(
    events_by_repo: dict[str, list[dict]],
    start: datetime,
    end: datetime,
) -> EventSummary:
    pr_numbers: set[str] = set()
    pr_head_refs: set[tuple[str, str]] = set()
    pr_event_count = 0
    branch_pushes: set[tuple[str, str]] = set()
    outside_recent: list[str] = []

    for repo, events in events_by_repo.items():
        for event in events:
            event_type = event.get("type")
            payload = event.get("payload") or {}
            if _event_in_window(event, start, end):
                if event_type == "PullRequestEvent":
                    pr = payload.get("pull_request") or {}
                    number = pr.get("number")
                    if number is not None:
                        pr_numbers.add(f"{repo}#{number}")
                    head_ref = ((pr.get("head") or {}).get("ref") or "").strip()
                    if head_ref:
                        pr_head_refs.add((repo, head_ref))
                    pr_event_count += 1
                elif event_type == "PushEvent":
                    ref = (payload.get("ref") or "").strip()
                    if ref and ref not in {"main", "master"}:
                        branch_pushes.add((repo, ref))
            elif event_type == "PullRequestEvent" and _event_before_window_but_recent(
                event, start
            ):
                pr = payload.get("pull_request") or {}
                number = pr.get("number")
                created = event.get("created_at")
                if number is not None and created:
                    outside_recent.append(f"{repo}#{number} at {created}")

    unmatched = sorted(
        f"{repo}:{branch}" for repo, branch in branch_pushes if (repo, branch) not in pr_head_refs
    )
    return EventSummary(
        distinct_prs=sorted(pr_numbers, key=_pr_sort_key),
        pr_event_count=pr_event_count,
        unmatched_branch_pushes=unmatched,
        blocker_reports=[],
        outside_recent_prs=sorted(set(outside_recent)),
        errors=[],
    )


def _pr_sort_key(value: str) -> tuple[str, int]:
    repo, _, number = value.partition("#")
    try:
        return repo, int(number)
    except ValueError:
        return repo, 0


def plural(count: int, singular: str, plural_word: str | None = None) -> str:
    return singular if count == 1 else (plural_word or singular + "s")


def build_fleet_message(
    start: datetime,
    end: datetime,
    summary: EventSummary,
    host_statuses: Iterable[HostStatus],
) -> str:
    prs = ", ".join(summary.distinct_prs) if summary.distinct_prs else "none"
    lines = [
        "Codex/Hermes fleet status",
        "",
        f"In the rolling 7-hour window from Main VPS time ({fmt_time(start)} to {fmt_time(end)}):",
        "",
        f"{len(summary.distinct_prs)} distinct PRs delivered: {prs}.",
        "",
        "Also:",
        "",
        f"{summary.pr_event_count} PR opened/updated {plural(summary.pr_event_count, 'event')} total.",
        (
            f"{len(summary.unmatched_branch_pushes)} branches were pushed but PR creation "
            "failed/awaits manual PR creation."
        ),
        (
            f"{len(summary.blocker_reports)} run {plural(len(summary.blocker_reports), 'produced', 'produced')} "
            f"a blocker/no-PR {plural(len(summary.blocker_reports), 'report')}."
        ),
    ]
    if summary.outside_recent_prs:
        lines.append(
            f"{summary.outside_recent_prs[0]} was just outside the 7-hour window."
        )
    if summary.unmatched_branch_pushes:
        lines.append("Branches needing PR check: " + ", ".join(summary.unmatched_branch_pushes[:6]))
    if summary.blocker_reports:
        lines.append("Blockers: " + " | ".join(summary.blocker_reports[:4]))

    lines.extend(["", "VPS status:"])
    for host in host_statuses:
        state = "OK" if host.ok else "FAIL"
        detail = "; ".join(host.details[:4]) if host.details else "no details"
        lines.append(f"- {host.name}: {state} ({detail})")
        for err in host.errors[:2]:
            lines.append(f"  - {err}")

    if summary.errors:
        lines.extend(["", "Collection errors:"])
        lines.extend(f"- {err}" for err in summary.errors[:6])
    return "\n".join(lines).strip()


def run_command(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def collect_repo_events(repos: Iterable[str]) -> tuple[dict[str, list[dict]], list[str]]:
    events: dict[str, list[dict]] = {}
    errors: list[str] = []
    for repo in repos:
        result = run_command(
            ["gh", "api", f"repos/{repo}/events", "-F", "per_page=100"],
            timeout=25,
        )
        if result.returncode != 0:
            errors.append(f"{repo}: gh events failed rc={result.returncode}")
            events[repo] = []
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            errors.append(f"{repo}: gh events returned non-json")
            payload = []
        events[repo] = payload if isinstance(payload, list) else []
    return events, errors


def collect_blocker_reports(start: datetime) -> list[str]:
    since = fmt_time(start)
    result = run_command(
        [
            "journalctl",
            "-u",
            "codex-*",
            "--since",
            since,
            "--no-pager",
        ],
        timeout=20,
    )
    text = result.stdout.lower()
    hits: list[str] = []
    for line in text.splitlines():
        if any(token in line for token in ("blocker", "no-pr", "no pr", "pr creation failed")):
            hits.append(line.strip()[:180])
    return sorted(set(hits))


def collect_host_statuses(hosts: Iterable[tuple[str, str]]) -> list[HostStatus]:
    statuses: list[HostStatus] = []
    remote_script = (
        "set +H; "
        "echo hermes-gateway=$(systemctl is-active hermes-gateway 2>/dev/null || true); "
        "echo codex_timers=$(systemctl list-timers 'codex-*' --all --no-pager 2>/dev/null "
        "| awk 'NR>1 && $0 !~ /^$/ {c++} END{print c+0}'); "
        "echo failed_units=$(systemctl --failed --no-legend 2>/dev/null | wc -l); "
        "df -h / | awk 'NR==2{print \"disk=\"$5\" used,\"$4\" free\"}'"
    )
    for name, ssh_alias in hosts:
        if ssh_alias == "local":
            result = run_command(["bash", "-lc", remote_script], timeout=20)
        else:
            result = run_command(["ssh", "-o", "ConnectTimeout=8", ssh_alias, remote_script], timeout=25)
        if result.returncode != 0:
            statuses.append(
                HostStatus(name=name, ok=False, details=[], errors=[f"status probe rc={result.returncode}"])
            )
            continue
        details = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        errors = [line for line in details if line.endswith("=failed") or line == "failed_units=1"]
        failed_count = 0
        for line in details:
            if line.startswith("failed_units="):
                try:
                    failed_count = int(line.split("=", 1)[1])
                except ValueError:
                    failed_count = 1
        ok = failed_count == 0 and not any("=failed" in line for line in details)
        statuses.append(HostStatus(name=name, ok=ok, details=details, errors=errors))
    return statuses


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"").strip("'")
    return values


def send_telegram(message: str, env_path: Path = DEFAULT_ENV) -> None:
    env = load_env(env_path)
    token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(f"Telegram credentials missing in {env_path}")
    body = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
    req = request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=20) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Telegram send failed HTTP {resp.status}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="send message to Telegram")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--now", help="UTC timestamp override, e.g. 2026-05-23T14:41:00Z")
    parser.add_argument("--repo", action="append", dest="repos")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    start, end = window_bounds(now, hours=7)
    repos = tuple(args.repos or DEFAULT_REPOS)
    events_by_repo, errors = collect_repo_events(repos)
    summary = summarize_github_events(events_by_repo, start, end)
    summary.errors.extend(errors)
    summary.blocker_reports.extend(collect_blocker_reports(start))
    host_statuses = collect_host_statuses(DEFAULT_HOSTS)
    message = build_fleet_message(start, end, summary, host_statuses)
    print(message)
    if args.send:
        send_telegram(message, args.env_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
