#!/usr/bin/env python3
"""Runtime contract + smoke validator for /api/todays_focus."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

EXIT_OK = 0
EXIT_CRITICAL = 1
EXIT_HTTP = 2
EXIT_JSON = 3
EXIT_CONFIG = 4

EXPECTED_TOP_LEVEL_KEYS = frozenset({"meta", "rows"})
EXPECTED_GROUPS = ("act_now", "watch", "already_ran", "blocked")
EXPECTED_META_KEYS = frozenset(
    {
        "read_only",
        "not_trade_advice",
        "visibility_only",
        "experimental",
        "not_for_alerting",
        "not_for_execution",
        "not_for_sizing",
        "not_for_source_ranking",
        "generated_at",
        "source_endpoint",
        "source_window_hours",
        "source_limit_per_group",
        "source_rows_considered",
        "source_group_counts",
        "source_truncated",
        "tracker_source_truncated",
        "max_rows",
        "paper_target",
        "tracker_target",
        "cache_ttl_minutes",
        "curation_policy",
        "rows_returned",
        "eligible_rows_considered",
        "empty_state",
    }
)
EXPECTED_ROW_KEYS = frozenset(
    {
        "row_key",
        "token_id",
        "symbol",
        "name",
        "chain",
        "source_corpus",
        "trade_inbox_group",
        "window_state",
        "verdict",
        "entry_quality",
        "surfaces",
        "opened_at",
        "opened_age_hours",
        "current_price",
        "market_cap",
        "price_change_24h",
        "price_updated_at",
        "price_is_stale",
        "price_staleness_minutes",
        "current_move_pct",
        "move_basis",
        "entry_quality_facts",
        "current_risk_facts",
        "counter_flag_facts",
        "inclusion_reasons",
        "risk_reasons",
        "block_reason_primary",
    }
)

ALLOWED_SOURCE_CORPUS = {"paper", "tracker"}
ALLOWED_WINDOW_STATES = {"open", "closing", "late", "closed", "unknown"}
ALLOWED_GROUPS = {"review", "followup", "moved", "blocked"}
ALLOWED_MOVE_BASIS = {"paper_entry", "tracker_detection"}
ALLOWED_ENTRY_QUALITIES = {
    None,
    "fresh_entry",
    "acceptable_pullback",
    "already_faded",
    "already_ran",
    "too_stale",
    "data_insufficient",
}
ALLOWED_VERDICTS = {None, "candidate_review", "watch", "blocked", "data_insufficient"}

FORBIDDEN_KEYS = {
    "action_label",
    "trade_score",
    "sort_key",
    "why_now",
}
FORBIDDEN_FIELD_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"recommend",
        r"top_pick",
        r"urgency",
        r"priority",
        r"alert",
        r"notify",
        r"operator_action",
        r"trade_now",
        r"watch_breakout",
        r"research_only",
        r"signal_to_send",
    )
)
BANNED_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bbuy\b",
        r"\bsell\b",
        r"\bconsider\b",
        r"\btrade[\s_-]*now\b",
        r"\bwatch[\s_-]*breakout\b",
        r"\bentry[\s_-]*is[\s_-]*late\b",
        r"\bpullback\b",
        r"\btarget\b",
        r"\bshould\b",
        r"\brecommend(?:ed|ation)?\b",
        r"\bgo[\s_-]*long\b",
        r"\benter[\s_-]*here\b",
        r"\btake[\s_-]*profit\b",
        r"\bstrong[\s_-]*buy\b",
        r"\bmust[\s_-]*buy\b",
        r"\burgency(?:\b|[\s_-])",
        r"\bpriority(?:\b|[\s_-])",
        r"\balert(?:\b|[\s_-])",
        r"\bnotify(?:\b|[\s_-])",
        r"\boperator[\s_-]*priority\b",
        r"\bresearch[\s_-]*only\b",
    )
)
FORBIDDEN_DIAGNOSTIC_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bsource[\s_-]*(?:rank|score|weight|priority|trust|confidence)\b",
        r"\bsource[\s_-]*rank(?:\b|[\s_-])",
        r"\bcaller[\s_-]*(?:rank|score|weight|authority|credibility|clout)\b",
        r"\bchannel[\s_-]*(?:rank|score|weight|trust)\b",
        r"\btweet[\s_-]*(?:rank|score|weight|credibility)\b",
        r"\burgency(?:\b|[\s_-])",
        r"\bpriority(?:\b|[\s_-])",
        r"\balert(?:\b|[\s_-])",
        r"\bnotify(?:\b|[\s_-])",
        r"\boperator[\s_-]*priority\b",
        r"\brecommend(?:ed|ation)?[\s_-]*(?:by[\s_-]*kol|action)?\b",
        r"\btrade[\s_-]*now\b",
        r"\bwatch[\s_-]*breakout\b",
        r"\bresearch[\s_-]*only\b",
        r"\bsignal[\s_-]*to[\s_-]*send\b",
    )
)
COPY_FIELDS = {
    "empty_state",
    "entry_quality_facts",
    "current_risk_facts",
    "counter_flag_facts",
}
ENUM_OR_ID_FIELDS = {
    "row_key",
    "token_id",
    "symbol",
    "name",
    "chain",
    "source_corpus",
    "trade_inbox_group",
    "window_state",
    "verdict",
    "entry_quality",
    "surfaces",
    "opened_at",
    "price_updated_at",
    "generated_at",
    "source_endpoint",
    "curation_policy",
    "move_basis",
    "inclusion_reasons",
    "risk_reasons",
    "block_reason_primary",
}


class Result:
    def __init__(self) -> None:
        self.criticals: list[str] = []
        self.warnings: list[str] = []
        self.passed = 0

    @property
    def is_clean(self) -> bool:
        return not self.criticals

    def critical(self, msg: str) -> None:
        self.criticals.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def ok(self) -> None:
        self.passed += 1


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    stripped = "".join(ch for ch in normalized if unicodedata.category(ch)[0] != "C")
    folded = stripped.casefold()
    return re.sub(r"\s+", " ", folded).strip()


def _parse_iso(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_key(key: str, path: str, result: Result) -> None:
    if key in FORBIDDEN_KEYS:
        result.critical(f"{path}: forbidden source field {key!r}")
    if key in EXPECTED_META_KEYS or key in EXPECTED_ROW_KEYS:
        return
    lower = key.casefold()
    for pattern in FORBIDDEN_FIELD_PATTERNS:
        if pattern.search(lower):
            result.critical(f"{path}: forbidden ranking/urgency/alert field {key!r}")


def _scan_copy(text: str, path: str, result: Result) -> None:
    normalized = _normalize_text(text)
    for pattern in BANNED_PATTERNS:
        if pattern.search(normalized):
            result.critical(
                f"banned-language: {path} matches {pattern.pattern!r} "
                f"in {normalized!r}"
            )


def _scan_diagnostic_value(text: str, path: str, result: Result) -> None:
    normalized = _normalize_text(text)
    for pattern in FORBIDDEN_DIAGNOSTIC_PATTERNS:
        if pattern.search(normalized):
            result.critical(
                f"{path}: forbidden alert/ranking diagnostic "
                f"matches {pattern.pattern!r} in {normalized!r}"
            )


def _walk_copy(value, path: str, result: Result) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str):
                _check_key(key, child_path, result)
            _walk_copy(child, child_path, result)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            _walk_copy(child, f"{path}[{idx}]", result)
    elif isinstance(value, str):
        _scan_copy(value, path, result)


def _walk_diagnostic_values(value, path: str, result: Result) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str):
                _check_key(key, child_path, result)
            _walk_diagnostic_values(child, child_path, result)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            _walk_diagnostic_values(child, f"{path}[{idx}]", result)
    elif isinstance(value, str):
        _scan_diagnostic_value(value, path, result)


def _walk_keys(value, path: str, result: Result) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str):
                _check_key(key, child_path, result)
                if key in COPY_FIELDS:
                    _walk_copy(child, child_path, result)
                elif key in ENUM_OR_ID_FIELDS:
                    _walk_diagnostic_values(child, child_path, result)
            _walk_keys(child, child_path, result)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            _walk_keys(child, f"{path}[{idx}]", result)


def _check_exact_keys(
    obj: dict, expected: frozenset[str], path: str, result: Result
) -> None:
    keys = set(obj.keys())
    missing = expected - keys
    unknown = keys - expected
    if missing:
        result.critical(f"{path}: missing keys {sorted(missing)!r}")
    if unknown:
        result.critical(f"{path}: unknown keys {sorted(unknown)!r}")


def _check_meta(meta, *, requested_window: int, result: Result) -> None:
    if not isinstance(meta, dict):
        result.critical(f"meta must be object; got {type(meta).__name__}")
        return
    _check_exact_keys(meta, EXPECTED_META_KEYS, "meta", result)
    for flag in (
        "read_only",
        "not_trade_advice",
        "visibility_only",
        "experimental",
        "not_for_alerting",
        "not_for_execution",
        "not_for_sizing",
        "not_for_source_ranking",
    ):
        if meta.get(flag) is not True:
            result.critical(f"meta.{flag} must be True")
    if _parse_iso(meta.get("generated_at")) is None:
        result.critical("meta.generated_at must be ISO8601")
    if meta.get("source_endpoint") != "/api/trade_inbox":
        result.critical("meta.source_endpoint must be /api/trade_inbox")
    if meta.get("source_window_hours") != requested_window:
        result.critical("meta.source_window_hours must match request")
    expected_ints = {
        "source_limit_per_group": 1,
        "source_rows_considered": 0,
        "max_rows": 1,
        "paper_target": 0,
        "tracker_target": 0,
        "cache_ttl_minutes": 1,
        "rows_returned": 0,
        "eligible_rows_considered": 0,
    }
    for field, minimum in expected_ints.items():
        value = meta.get(field)
        if not _is_int(value) or value < minimum:
            result.critical(f"meta.{field} must be int >= {minimum}")
    if meta.get("max_rows") != 5:
        result.critical("meta.max_rows must be 5")
    if meta.get("paper_target") != 3 or meta.get("tracker_target") != 2:
        result.critical("meta paper/tracker targets must be 3/2")
    if meta.get("cache_ttl_minutes") != 60:
        result.critical("meta.cache_ttl_minutes must be 60")
    if meta.get("curation_policy") != "fixed_recipe_3_paper_2_tracker_no_score":
        result.critical("meta.curation_policy drifted")
    for field in ("source_truncated", "tracker_source_truncated"):
        if not isinstance(meta.get(field), bool):
            result.critical(f"meta.{field} must be bool")
    counts = meta.get("source_group_counts")
    if not isinstance(counts, dict):
        result.critical("meta.source_group_counts must be object")
    elif set(counts) != set(EXPECTED_GROUPS):
        result.critical("meta.source_group_counts must contain all Trade Inbox groups")
    else:
        for group, count in counts.items():
            if not _is_int(count) or count < 0:
                result.critical(f"meta.source_group_counts.{group} must be int >= 0")
    if not isinstance(meta.get("empty_state"), str) or not meta["empty_state"]:
        result.critical("meta.empty_state must be non-empty string")


def _check_row(row, idx: int, result: Result) -> None:
    path = f"rows[{idx}]"
    if not isinstance(row, dict):
        result.critical(f"{path} must be object")
        return
    _check_exact_keys(row, EXPECTED_ROW_KEYS, path, result)

    for field in ("row_key", "token_id", "source_corpus", "trade_inbox_group"):
        if not isinstance(row.get(field), str) or not row[field]:
            result.critical(f"{path}.{field} must be non-empty str")
    for field in (
        "symbol",
        "name",
        "chain",
        "verdict",
        "entry_quality",
        "block_reason_primary",
    ):
        if row.get(field) is not None and not isinstance(row.get(field), str):
            result.critical(f"{path}.{field} must be str|None")
    if row.get("source_corpus") not in ALLOWED_SOURCE_CORPUS:
        result.critical(f"{path}.source_corpus invalid")
    if row.get("trade_inbox_group") not in ALLOWED_GROUPS:
        result.critical(f"{path}.trade_inbox_group invalid")
    if row.get("window_state") not in ALLOWED_WINDOW_STATES:
        result.critical(f"{path}.window_state invalid")
    if row.get("entry_quality") not in ALLOWED_ENTRY_QUALITIES:
        result.critical(f"{path}.entry_quality invalid")
    if row.get("verdict") not in ALLOWED_VERDICTS:
        result.critical(f"{path}.verdict invalid")
    if row.get("move_basis") not in ALLOWED_MOVE_BASIS:
        result.critical(f"{path}.move_basis invalid")
    if row.get("source_corpus") == "paper" and row.get("move_basis") != "paper_entry":
        result.critical(f"{path}: paper row must use paper_entry move_basis")
    if (
        row.get("source_corpus") == "tracker"
        and row.get("move_basis") != "tracker_detection"
    ):
        result.critical(f"{path}: tracker row must use tracker_detection move_basis")
    for field in ("opened_at", "price_updated_at"):
        value = row.get(field)
        if value is not None and _parse_iso(value) is None:
            result.critical(f"{path}.{field} must be ISO8601|None")
    if not isinstance(row.get("price_is_stale"), bool):
        result.critical(f"{path}.price_is_stale must be bool")
    for field in (
        "opened_age_hours",
        "current_price",
        "market_cap",
        "price_change_24h",
        "price_staleness_minutes",
        "current_move_pct",
    ):
        value = row.get(field)
        if value is not None and not _is_number(value):
            result.critical(f"{path}.{field} must be number|None")
    for field in (
        "surfaces",
        "entry_quality_facts",
        "current_risk_facts",
        "counter_flag_facts",
        "inclusion_reasons",
        "risk_reasons",
    ):
        value = row.get(field)
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            result.critical(f"{path}.{field} must be list[str]")


def validate_payload(payload, *, requested_window: int = 36) -> Result:
    result = Result()
    if not isinstance(payload, dict):
        result.critical(f"payload must be object; got {type(payload).__name__}")
        return result
    _check_exact_keys(payload, EXPECTED_TOP_LEVEL_KEYS, "top-level", result)
    _walk_keys(payload, "", result)
    _check_meta(payload.get("meta"), requested_window=requested_window, result=result)
    rows = payload.get("rows")
    if not isinstance(rows, list):
        result.critical("rows must be list")
        rows = []
    if len(rows) > 5:
        result.critical("rows must contain at most 5 items")
    row_keys: list[str] = []
    for idx, row in enumerate(rows):
        _check_row(row, idx, result)
        if isinstance(row, dict) and isinstance(row.get("row_key"), str):
            row_keys.append(row["row_key"])
    if len(row_keys) != len(set(row_keys)):
        result.critical("duplicate row_key rows are not allowed")
    meta = payload.get("meta")
    if isinstance(meta, dict):
        if meta.get("rows_returned") != len(rows):
            result.critical("meta.rows_returned must equal returned rows")
        eligible = meta.get("eligible_rows_considered")
        if _is_int(eligible) and eligible < len(rows):
            result.critical("meta.eligible_rows_considered must be >= rows_returned")
    if result.is_clean:
        result.ok()
    return result


def fetch_and_validate(
    url: str,
    *,
    timeout_sec: float = 10.0,
    window_hours: int = 36,
) -> tuple[Result, int]:
    query = urlencode({"window_hours": window_hours})
    target = f"{url.rstrip('/')}/api/todays_focus?{query}"
    started = time.monotonic()
    try:
        with urllib.request.urlopen(target, timeout=timeout_sec) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.HTTPError as exc:
        result = Result()
        result.critical(f"http error {exc.code}: {exc.reason} (url={target})")
        return result, EXIT_HTTP
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result = Result()
        result.critical(f"http fetch failed: {exc}")
        return result, EXIT_HTTP
    if status != 200:
        result = Result()
        result.critical(f"http status {status} != 200")
        return result, EXIT_HTTP
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        result = Result()
        result.critical(f"json parse failed: {exc}")
        return result, EXIT_JSON
    result = validate_payload(payload, requested_window=window_hours)
    elapsed_ms = (time.monotonic() - started) * 1000
    if elapsed_ms > 3000:
        result.warn(f"response latency {elapsed_ms:.0f}ms exceeds 3000ms SLO")
    return result, EXIT_OK if result.is_clean else EXIT_CRITICAL


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Runtime contract + smoke validator for /api/todays_focus",
    )
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--window-hours", type=int, default=36)
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.window_hours < 6 or args.window_hours > 72:
        print("--window-hours must be in [6, 72]", file=sys.stderr)
        return EXIT_CONFIG
    result, exit_code = fetch_and_validate(
        args.url,
        timeout_sec=args.timeout_sec,
        window_hours=args.window_hours,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "status": "ok" if exit_code == EXIT_OK else "fail",
                    "exit_code": exit_code,
                    "critical_count": len(result.criticals),
                    "warning_count": len(result.warnings),
                    "criticals": result.criticals,
                    "warnings": result.warnings,
                    "passed": result.passed,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        label = "OK" if exit_code == EXIT_OK else "FAIL"
        print(
            f"{label}: {len(result.criticals)} critical(s), "
            f"{len(result.warnings)} warning(s)"
        )
        if args.verbose or exit_code != EXIT_OK:
            for msg in result.criticals:
                print(f"CRITICAL: {msg}")
            for msg in result.warnings:
                print(f"WARNING: {msg}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
