#!/usr/bin/env python3
"""Runtime contract + smoke validator for /api/trade_inbox."""

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

_DEFAULT_LIMIT_PER_GROUP = 10
_DEFAULT_WINDOW_HOURS = 36

EXPECTED_GROUPS = ("act_now", "watch", "already_ran", "blocked")
EXPECTED_TOP_LEVEL_KEYS = frozenset({"meta", "groups"})
EXPECTED_META_KEYS = frozenset({
    "read_only",
    "not_trade_advice",
    "experimental",
    "generated_at",
    "window_hours",
    "limit_per_group",
    "rows_returned",
    "source_limit",
    "source_rows_considered",
    "open_trades_scanned",
    "paper_rows_considered",
    "tracker_rows_considered",
    "tracker_rows_promoted",
    "tracker_source_truncated",
    "source_truncated",
    "group_counts",
    "group_hidden_counts",
    "block_reason_counts",
    "stale_warning_count",
    "hard_stale_count",
    "source",
})
EXPECTED_ROW_KEYS = frozenset({
    "token_id",
    "symbol",
    "name",
    "chain",
    "source_corpus",
    "group",
    "action_label",
    "window_state",
    "trade_score",
    "sort_key",
    "why_now",
    "inclusion_reasons",
    "risk_reasons",
    "surfaces",
    "open_trade_ids",
    "recent_trade_ids",
    "actionable",
    "would_be_live",
    "block_reason_primary",
    "opened_at",
    "opened_age_hours",
    "pct_from_entry",
    "price_change_24h",
    "market_cap",
    "current_price",
    "entry_quality",
    "verdict",
    "price_updated_at",
    "price_is_stale",
    "price_staleness_minutes",
})

ALLOWED_SOURCE_CORPUS = {"paper", "tracker"}
ALLOWED_ACTION_LABELS = {
    "REVIEW_NOW",
    "WATCH_PULLBACK",
    "TOO_LATE",
    "BLOCKED",
    "DATA_MISSING",
}
ALLOWED_WINDOW_STATES = {"open", "closing", "late", "closed", "unknown"}
ALLOWED_ENTRY_QUALITIES = {
    None,
    "fresh_entry",
    "acceptable_pullback",
    "already_faded",
    "already_ran",
    "too_stale",
    "data_insufficient",
}
ALLOWED_VERDICTS = {
    None,
    "candidate_review",
    "watch",
    "blocked",
    "data_insufficient",
}

BANNED_TOKENS = (
    "buy now",
    "sell now",
    "trade now",
    "go long",
    "short this",
    "enter here",
    "entry signal",
    "execute trade",
    "ape in",
    "take profit",
    "lock in",
    "secure profit",
    "cut losses",
    "moon",
    "mooning",
    "100x",
    "10x",
    "1000x",
    "hidden gem",
    "alpha leak",
    "do not miss",
    "last chance",
    "easy money",
    "free money",
    "strong buy",
    "must buy",
    "breakout confirmed",
)

BANNED_TOKEN_PATTERNS = tuple(
    re.compile(r"\b" + r"[\s_-]*".join(map(re.escape, token.split())) + r"\b")
    for token in BANNED_TOKENS
)

SCAN_EXEMPT_STRING_FIELDS = frozenset({
    "token_id",
    "symbol",
    "name",
    "chain",
    "source_corpus",
    "group",
    "action_label",
    "window_state",
    "entry_quality",
    "verdict",
    "opened_at",
    "price_updated_at",
    "generated_at",
    "source",
    "surfaces",
})

FORBIDDEN_FIELD_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"kol",
        r"source_?(rank|score|weight|priority|trust|confidence)",
        r"caller_?(rank|score|weight|authority|credibility|clout)",
        r"channel_?(rank|score|weight|trust)",
        r"tweet_?(rank|score|weight|credibility)",
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

FORBIDDEN_VALUE_TOKENS = (
    "kol_rank",
    "source_score",
    "caller_weight",
    "operator_priority",
    "alert_level",
    "notify_candidate",
    "recommended_by_kol",
    "recommended_action",
    "trade_now",
    "watch_breakout",
    "research_only",
    "signal_to_send",
)

FORBIDDEN_VALUE_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"\bkol[\s_-]*rank\b",
        r"\bsource[\s_-]*rank(?:\b|[\s_-])",
        r"\bsource[\s_-]*score\b",
        r"\bcaller[\s_-]*weight\b",
        r"\burgency(?:\b|[\s_-])",
        r"\bpriority(?:\b|[\s_-])",
        r"\balert(?:\b|[\s_-])",
        r"\boperator[\s_-]*priority\b",
        r"\balert[\s_-]*level\b",
        r"\bnotify[\s_-]*candidate\b",
        r"\brecommend(?:ed|ation)?[\s_-]*(?:by[\s_-]*kol|action)?\b",
        r"\btrade[\s_-]*now\b",
        r"\bwatch[\s_-]*breakout\b",
        r"\bresearch[\s_-]*only\b",
        r"\bsignal[\s_-]*to[\s_-]*send\b",
    )
)


class Result:
    def __init__(self) -> None:
        self.criticals: list[str] = []
        self.warnings: list[str] = []
        self.passed: int = 0

    def critical(self, msg: str) -> None:
        self.criticals.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def ok(self) -> None:
        self.passed += 1

    @property
    def is_clean(self) -> bool:
        return not self.criticals


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    stripped = "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf")
    folded = stripped.casefold()
    return re.sub(r"\s+", " ", folded)


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


def _is_non_bool_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_non_bool_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_key_firewall(key: str, path: str, result: Result) -> None:
    lower = key.casefold()
    for pattern in FORBIDDEN_FIELD_PATTERNS:
        if pattern.search(lower):
            result.critical(
                f"{path}: forbidden ranking/urgency/alert field {key!r} "
                f"matches {pattern.pattern!r}"
            )


def _scan_string_value(text: str, path: str, result: Result) -> None:
    normalized = _normalize_text(text)
    for token, pattern in zip(BANNED_TOKENS, BANNED_TOKEN_PATTERNS, strict=True):
        if pattern.search(normalized):
            result.critical(
                f"banned-language: token {token!r} found in {path} "
                f"(normalized text: {normalized!r})"
            )
    for token in FORBIDDEN_VALUE_TOKENS:
        if token in normalized:
            result.critical(
                f"{path}: forbidden ranking/urgency/alert value token "
                f"{token!r} found in {normalized!r}"
            )
    for pattern in FORBIDDEN_VALUE_PATTERNS:
        if pattern.search(normalized):
            result.critical(
                f"{path}: forbidden ranking/urgency/alert value "
                f"matches {pattern.pattern!r} in {normalized!r}"
            )


def _walk_payload(value, path: str, result: Result) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str):
                _check_key_firewall(key, child_path, result)
                if key in SCAN_EXEMPT_STRING_FIELDS:
                    _walk_forbidden_contract_values(child, child_path, result)
                    continue
            _walk_payload(child, child_path, result)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            _walk_payload(child, f"{path}[{idx}]", result)
    elif isinstance(value, str):
        _scan_string_value(value, path, result)


def _walk_forbidden_contract_values(value, path: str, result: Result) -> None:
    """Scan exempt identifier fields only for ranking/urgency/alert tokens."""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str):
                _check_key_firewall(key, child_path, result)
            _walk_forbidden_contract_values(child, child_path, result)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            _walk_forbidden_contract_values(child, f"{path}[{idx}]", result)
    elif isinstance(value, str):
        normalized = _normalize_text(value)
        for token in FORBIDDEN_VALUE_TOKENS:
            if token in normalized:
                result.critical(
                    f"{path}: forbidden ranking/urgency/alert value token "
                    f"{token!r} found in {normalized!r}"
                )
        for pattern in FORBIDDEN_VALUE_PATTERNS:
            if pattern.search(normalized):
                result.critical(
                    f"{path}: forbidden ranking/urgency/alert value "
                    f"matches {pattern.pattern!r} in {normalized!r}"
                )


def _check_exact_keys(obj: dict, expected: frozenset[str], path: str, result: Result) -> None:
    keys = set(obj.keys())
    missing = expected - keys
    unknown = keys - expected
    if missing:
        result.critical(f"{path}: missing keys {sorted(missing)!r}")
    if unknown:
        label = "row keys" if path.startswith("groups.") else f"{path.split('.')[-1]} keys"
        result.critical(f"{path}: unknown {label} {sorted(unknown)!r}")


def _check_group_count_map(value, path: str, result: Result) -> dict[str, int] | None:
    if not isinstance(value, dict):
        result.critical(f"meta.{path} must be object")
        return None
    if set(value.keys()) != set(EXPECTED_GROUPS):
        result.critical(
            f"meta.{path} keys {sorted(value.keys())!r} != {sorted(EXPECTED_GROUPS)!r}"
        )
    cleaned: dict[str, int] = {}
    for group in EXPECTED_GROUPS:
        count = value.get(group)
        if not _is_non_bool_int(count) or count < 0:
            result.critical(f"meta.{path}.{group} must be non-bool int>=0")
        elif isinstance(count, int):
            cleaned[group] = count
    return cleaned


def _check_meta(meta, *, requested_limit_per_group: int, requested_window: int, result: Result) -> None:
    if not isinstance(meta, dict):
        result.critical(f"meta must be object; got {type(meta).__name__}")
        return
    _check_exact_keys(meta, EXPECTED_META_KEYS, "meta", result)

    for flag in ("read_only", "not_trade_advice", "experimental"):
        if meta.get(flag) is not True:
            result.critical(f"meta.{flag} must be True (got {meta.get(flag)!r})")

    if _parse_iso(meta.get("generated_at")) is None:
        result.critical(f"meta.generated_at must be ISO8601 (got {meta.get('generated_at')!r})")

    if meta.get("window_hours") != requested_window:
        result.critical(
            f"meta.window_hours {meta.get('window_hours')!r} != requested {requested_window}"
        )
    if meta.get("limit_per_group") != requested_limit_per_group:
        result.critical(
            f"meta.limit_per_group {meta.get('limit_per_group')!r} != requested {requested_limit_per_group}"
        )
    if "limit" in meta:
        result.critical("meta.limit is not part of /api/trade_inbox contract")

    if meta.get("source") != "live_candidates":
        result.critical(f"meta.source must be 'live_candidates' (got {meta.get('source')!r})")

    for field in (
        "rows_returned",
        "source_limit",
        "source_rows_considered",
        "open_trades_scanned",
        "paper_rows_considered",
        "tracker_rows_considered",
        "tracker_rows_promoted",
        "stale_warning_count",
        "hard_stale_count",
    ):
        value = meta.get(field)
        if not _is_non_bool_int(value) or value < 0:
            result.critical(f"meta.{field} must be non-bool int>=0 (got {value!r})")

    for field in ("tracker_source_truncated", "source_truncated"):
        if not isinstance(meta.get(field), bool):
            result.critical(f"meta.{field} must be bool (got {meta.get(field)!r})")

    _check_group_count_map(meta.get("group_counts"), "group_counts", result)
    _check_group_count_map(meta.get("group_hidden_counts"), "group_hidden_counts", result)

    block_counts = meta.get("block_reason_counts")
    if not isinstance(block_counts, dict):
        result.critical("meta.block_reason_counts must be object")
    else:
        for key, value in block_counts.items():
            if not isinstance(key, str) or not _is_non_bool_int(value) or value < 0:
                result.critical("meta.block_reason_counts must map str -> int>=0")
                break


def _check_row_types(row: dict, path: str, result: Result) -> None:
    for field in ("token_id", "source_corpus", "group", "action_label", "window_state"):
        value = row.get(field)
        if not isinstance(value, str) or not value:
            result.critical(f"{path}.{field} must be non-empty str")

    for field in (
        "symbol",
        "name",
        "chain",
        "block_reason_primary",
        "opened_at",
        "price_updated_at",
        "entry_quality",
        "verdict",
    ):
        value = row.get(field)
        if value is not None and not isinstance(value, str):
            result.critical(f"{path}.{field} must be str|None")

    for field in ("opened_at", "price_updated_at"):
        value = row.get(field)
        if value is not None and _parse_iso(value) is None:
            result.critical(f"{path}.{field} {value!r} is not ISO8601")

    for field in (
        "trade_score",
        "opened_age_hours",
        "pct_from_entry",
        "price_change_24h",
        "market_cap",
        "current_price",
        "price_staleness_minutes",
    ):
        value = row.get(field)
        if value is not None and not _is_non_bool_number(value):
            result.critical(f"{path}.{field} must be number|None")

    if not isinstance(row.get("price_is_stale"), bool):
        result.critical(f"{path}.price_is_stale must be bool")

    for field in ("open_trade_ids", "recent_trade_ids"):
        value = row.get(field)
        if not isinstance(value, list) or any(not _is_non_bool_int(x) for x in value):
            result.critical(f"{path}.{field} must be list[int]")

    for field in ("why_now", "inclusion_reasons", "risk_reasons", "surfaces"):
        value = row.get(field)
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            result.critical(f"{path}.{field} must be list[str]")

    sort_key = row.get("sort_key")
    if not isinstance(sort_key, list) or any(
        not isinstance(x, (str, int, float)) or isinstance(x, bool)
        for x in (sort_key if isinstance(sort_key, list) else [])
    ):
        result.critical(f"{path}.sort_key must be list[str|int|float]")

    for field in ("actionable", "would_be_live"):
        value = row.get(field)
        if value is not None and (not _is_non_bool_int(value) or value not in (0, 1)):
            result.critical(f"{path}.{field} must be 0|1|None")


def _check_row(row, group: str, idx: int, result: Result) -> None:
    path = f"groups.{group}[{idx}]"
    if not isinstance(row, dict):
        result.critical(f"{path} must be object; got {type(row).__name__}")
        return

    _check_exact_keys(row, EXPECTED_ROW_KEYS, path, result)
    _check_row_types(row, path, result)

    if row.get("group") != group:
        result.critical(
            f"{path}.group {row.get('group')!r} does not match enclosing group {group!r}"
        )

    if row.get("source_corpus") not in ALLOWED_SOURCE_CORPUS:
        result.critical(f"{path}.source_corpus {row.get('source_corpus')!r} invalid")
    if row.get("action_label") not in ALLOWED_ACTION_LABELS:
        result.critical(f"{path}.action_label {row.get('action_label')!r} invalid")
    if row.get("window_state") not in ALLOWED_WINDOW_STATES:
        result.critical(f"{path}.window_state {row.get('window_state')!r} invalid")
    if row.get("entry_quality") not in ALLOWED_ENTRY_QUALITIES:
        result.critical(f"{path}.entry_quality {row.get('entry_quality')!r} invalid")
    if row.get("verdict") not in ALLOWED_VERDICTS:
        result.critical(f"{path}.verdict {row.get('verdict')!r} invalid")

    inclusion = row.get("inclusion_reasons") if isinstance(row.get("inclusion_reasons"), list) else []
    risk = row.get("risk_reasons") if isinstance(row.get("risk_reasons"), list) else []
    surfaces = row.get("surfaces") if isinstance(row.get("surfaces"), list) else []

    if row.get("source_corpus") == "paper":
        if not row.get("open_trade_ids"):
            result.critical(f"{path}: paper row must have non-empty open_trade_ids")
        if "open_paper_trade" not in inclusion:
            result.critical(f"{path}: paper row must include open_paper_trade")
        if "tracker_only_no_paper_trade" in risk:
            result.critical(f"{path}: paper row cannot carry tracker_only_no_paper_trade")

    if row.get("source_corpus") == "tracker":
        if row.get("open_trade_ids") != []:
            result.critical(f"{path}: tracker row must have open_trade_ids == []")
        if row.get("recent_trade_ids") != []:
            result.critical(f"{path}: tracker row must have recent_trade_ids == []")
        if "top_gainers_tracker" not in surfaces:
            result.critical(f"{path}: tracker row must include top_gainers_tracker surface")
        if "tracker_promotion" not in inclusion or "top_gainers_tracker" not in inclusion:
            result.critical(
                f"{path}: tracker row must include tracker_promotion and top_gainers_tracker"
            )
        if "tracker_only_no_paper_trade" not in risk:
            result.critical(f"{path}: tracker row must include tracker_only_no_paper_trade")
        if row.get("actionable") is not None:
            result.critical(f"{path}: tracker row actionable must be None")
        if row.get("would_be_live") is not None:
            result.critical(f"{path}: tracker row would_be_live must be None")
        if group == "act_now":
            result.critical(f"{path}: tracker row cannot be in act_now")


def _check_group_meta(payload: dict, flat_rows: list[dict], result: Result) -> None:
    meta = payload.get("meta")
    groups = payload.get("groups")
    if not isinstance(meta, dict) or not isinstance(groups, dict):
        return

    rows_returned = sum(len(rows) for rows in groups.values() if isinstance(rows, list))
    returned_tracker = sum(
        1 for row in flat_rows if row.get("source_corpus") == "tracker"
    )
    returned_paper = sum(1 for row in flat_rows if row.get("source_corpus") == "paper")
    returned_blocked_reason_counts: dict[str, int] = {}
    for row in flat_rows:
        reason = row.get("block_reason_primary")
        if row.get("group") == "blocked" and isinstance(reason, str) and reason:
            returned_blocked_reason_counts[reason] = (
                returned_blocked_reason_counts.get(reason, 0) + 1
            )

    if meta.get("rows_returned") != rows_returned:
        result.critical(
            f"meta.rows_returned {meta.get('rows_returned')!r} != returned rows {rows_returned}"
        )

    group_counts = meta.get("group_counts")
    group_hidden = meta.get("group_hidden_counts")
    if isinstance(group_counts, dict) and isinstance(group_hidden, dict):
        for group in EXPECTED_GROUPS:
            actual = len(groups.get(group) or [])
            if group in group_counts and group in group_hidden:
                if group_hidden[group] != group_counts[group] - actual:
                    result.critical(f"meta.group_hidden_counts.{group} does not match count minus returned")
        if all(_is_non_bool_int(group_counts.get(g)) for g in EXPECTED_GROUPS):
            if sum(group_counts[g] for g in EXPECTED_GROUPS) != meta.get("source_rows_considered"):
                result.critical("sum(group_counts) must equal meta.source_rows_considered")
        if all(_is_non_bool_int(group_hidden.get(g)) for g in EXPECTED_GROUPS):
            if sum(group_hidden[g] for g in EXPECTED_GROUPS) != (
                meta.get("source_rows_considered") - rows_returned
            ):
                result.critical("sum(group_hidden_counts) must equal source_rows_considered - rows_returned")

    paper_considered = meta.get("paper_rows_considered")
    tracker_promoted = meta.get("tracker_rows_promoted")
    source_considered = meta.get("source_rows_considered")
    tracker_considered = meta.get("tracker_rows_considered")
    open_scanned = meta.get("open_trades_scanned")

    if all(_is_non_bool_int(v) for v in (paper_considered, tracker_promoted, source_considered)):
        if paper_considered + tracker_promoted != source_considered:
            result.critical("paper_rows_considered + tracker_rows_promoted must equal source_rows_considered")
    if _is_non_bool_int(tracker_promoted) and tracker_promoted < returned_tracker:
        result.critical("tracker_rows_promoted must be >= returned tracker rows")
    if all(_is_non_bool_int(v) for v in (tracker_considered, tracker_promoted)):
        if tracker_considered < tracker_promoted:
            result.critical("tracker_rows_considered must be >= tracker_rows_promoted")
    if all(_is_non_bool_int(v) for v in (open_scanned, paper_considered)):
        if open_scanned < paper_considered:
            result.critical("open_trades_scanned must be >= paper_rows_considered")
    if _is_non_bool_int(paper_considered) and paper_considered < returned_paper:
        result.critical("paper_rows_considered must be >= returned paper rows")

    block_counts = meta.get("block_reason_counts")
    if isinstance(block_counts, dict) and isinstance(group_counts, dict) and isinstance(group_hidden, dict):
        total_block_reasons = sum(
            v for v in block_counts.values() if _is_non_bool_int(v)
        )
        returned_blocked_with_reason = sum(returned_blocked_reason_counts.values())
        if total_block_reasons < returned_blocked_with_reason:
            result.critical("meta.block_reason_counts must cover returned blocked rows")
        for reason, returned_count in returned_blocked_reason_counts.items():
            recorded_count = block_counts.get(reason)
            if not _is_non_bool_int(recorded_count) or recorded_count < returned_count:
                result.critical(
                    f"meta.block_reason_counts[{reason!r}] must be >= returned blocked rows for that reason"
                )
        blocked_group_count = group_counts.get("blocked")
        if _is_non_bool_int(blocked_group_count) and total_block_reasons > blocked_group_count:
            result.critical("meta.block_reason_counts total cannot exceed group_counts.blocked")
        if group_hidden.get("blocked") == 0 and block_counts != returned_blocked_reason_counts:
            result.critical(
                "meta.block_reason_counts must exactly match returned blocked reasons when no blocked rows are hidden"
            )


def validate_payload(
    payload,
    *,
    requested_limit_per_group: int = 20,
    requested_window: int = 36,
) -> Result:
    result = Result()
    if not isinstance(payload, dict):
        result.critical(f"payload must be object; got {type(payload).__name__}")
        return result

    _check_exact_keys(payload, EXPECTED_TOP_LEVEL_KEYS, "top-level", result)
    _walk_payload(payload, "", result)

    groups = payload.get("groups")
    if not isinstance(groups, dict):
        result.critical(f"groups must be object; got {type(groups).__name__}")
        groups = {}
    elif set(groups.keys()) != set(EXPECTED_GROUPS):
        result.critical(
            f"groups keys {sorted(groups.keys())!r} != {sorted(EXPECTED_GROUPS)!r}"
        )

    _check_meta(
        payload.get("meta"),
        requested_limit_per_group=requested_limit_per_group,
        requested_window=requested_window,
        result=result,
    )

    flat_rows: list[dict] = []
    for group in EXPECTED_GROUPS:
        rows = groups.get(group, []) if isinstance(groups, dict) else []
        if not isinstance(rows, list):
            result.critical(f"groups.{group} must be list")
            continue
        for idx, row in enumerate(rows):
            _check_row(row, group, idx, result)
            if isinstance(row, dict):
                flat_rows.append(row)

    token_ids = [
        row.get("token_id")
        for row in flat_rows
        if isinstance(row.get("token_id"), str)
    ]
    if len(token_ids) != len(set(token_ids)):
        result.critical("duplicate token_id rows are not allowed in Trade Inbox")

    corpus_token_pairs = [
        (row.get("source_corpus"), row.get("token_id"))
        for row in flat_rows
        if isinstance(row.get("source_corpus"), str)
        and isinstance(row.get("token_id"), str)
    ]
    if len(corpus_token_pairs) != len(set(corpus_token_pairs)):
        result.critical("duplicate (source_corpus, token_id) rows are not allowed")

    _check_group_meta(payload, flat_rows, result)
    if result.is_clean:
        result.ok()
    return result


def fetch_and_validate(
    url: str,
    *,
    timeout_sec: float = 10.0,
    limit_per_group: int = 20,
    window_hours: int = 36,
) -> tuple[Result, int]:
    query = urlencode({
        "limit_per_group": limit_per_group,
        "window_hours": window_hours,
    })
    target = f"{url.rstrip('/')}/api/trade_inbox?{query}"
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

    result = validate_payload(
        payload,
        requested_limit_per_group=limit_per_group,
        requested_window=window_hours,
    )
    elapsed_ms = (time.monotonic() - started) * 1000
    if elapsed_ms > 3000:
        result.warn(f"response latency {elapsed_ms:.0f}ms exceeds provisional 3000ms SLO")
    return result, EXIT_OK if result.is_clean else EXIT_CRITICAL


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Runtime contract + smoke validator for /api/trade_inbox",
    )
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--limit-per-group", type=int, default=_DEFAULT_LIMIT_PER_GROUP)
    parser.add_argument("--window-hours", type=int, default=_DEFAULT_WINDOW_HOURS)
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.limit_per_group < 1 or args.limit_per_group > 100:
        print("--limit-per-group must be in [1, 100]", file=sys.stderr)
        return EXIT_CONFIG
    if args.window_hours < 6 or args.window_hours > 72:
        print("--window-hours must be in [6, 72]", file=sys.stderr)
        return EXIT_CONFIG

    result, exit_code = fetch_and_validate(
        args.url,
        timeout_sec=args.timeout_sec,
        limit_per_group=args.limit_per_group,
        window_hours=args.window_hours,
    )
    if args.json:
        print(json.dumps({
            "status": "ok" if exit_code == EXIT_OK else "fail",
            "exit_code": exit_code,
            "critical_count": len(result.criticals),
            "warning_count": len(result.warnings),
            "criticals": result.criticals,
            "warnings": result.warnings,
            "passed": result.passed,
        }, indent=2, sort_keys=True))
    else:
        label = "OK" if exit_code == EXIT_OK else "FAIL"
        print(f"{label}: {len(result.criticals)} critical(s), {len(result.warnings)} warning(s)")
        if args.verbose or exit_code != EXIT_OK:
            for msg in result.criticals:
                print(f"CRITICAL: {msg}")
            for msg in result.warnings:
                print(f"WARNING: {msg}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
