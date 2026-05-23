#!/usr/bin/env python3
"""Runtime contract + smoke validator for /api/live_candidates.

Closes the post-PR-#229 gap: response_model validation passed in unit tests
yet 500'd in prod against real predictions.counter_flags shape. This
script makes one HTTP call against a configurable URL, validates the
response against the frozen V1 contract (see
`tasks/plan_live_candidates_contract_smoke_2026_05_23.md`), and exits
0/non-zero.

Run modes:
    python scripts/check_live_candidates_contract.py --url http://localhost:8000
    python scripts/check_live_candidates_contract.py --url http://localhost:8000 --json
    python scripts/check_live_candidates_contract.py --url http://localhost:8000 --verbose

The validator is the firewall keeping advice-tone / per-source-KOL-ranking
drift out of the cockpit. There is no `--skip-banned-language` flag; if a
CRITICAL fires, fix the producer.

Exit codes:
  0 — all CRITICAL checks pass (WARNINGs allowed)
  1 — at least one CRITICAL failure
  2 — HTTP error (non-200, timeout, connection refused)
  3 — JSON parse error
  4 — argparse / config error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

EXIT_OK = 0
EXIT_CRITICAL = 1
EXIT_HTTP = 2
EXIT_JSON = 3
EXIT_CONFIG = 4

ALLOWED_VERDICTS = {"candidate_review", "watch", "blocked", "data_insufficient"}
ALLOWED_ENTRY_QUALITIES = {
    "fresh_entry",
    "acceptable_pullback",
    "already_faded",
    "already_ran",
    "too_stale",
    "data_insufficient",
}

DATA_INSUFFICIENT_RISK_REASONS = {
    "no_price_snapshot_for_token_id",
    "price_timestamp_unparseable",
    "opened_at_unparseable",
    "entry_price_missing_or_invalid",
    "actionable_null_pre_cutover",
    "price_is_stale",
}

BANNED_IMPERATIVES_V1 = (
    "buy now",
    "sell now",
    "trade now",
    "go long",
    "short this",
    "enter here",
    "entry signal",
    "execute trade",
    "ape in",
    "aping",
    "send it",
    "dump it",
    "take profit",
    "lock in profit",
    "lock in gains",
    "lock in",
    "secure profit",
    "cut losses",
    "stop loss now",
    "load up",
    "loading bags",
    "bagging",
    # V1 additions per Vector-B I3 (unambiguous trader-speak)
    "secured",
    "confirmed entry",
    "entry point",
    "cash out",
    "take the w",
    "pump and dump",
    "paper hands",
    "diamond hands",
    "dip buy",
    "dip-buying",
    "dca in",
    "dca-ing",
    "bag the dip",
    # Removed `accumulate` (Vector-B C3 dual-use: "accumulating losses" / "errors
    # accumulating" are legitimate non-trading prose).
)

# "easy" / "easy money" prefix series — co-occurrence to reduce false-positive risk on
# bare "easy" while keeping the actionable-tone leak surface covered.
BANNED_HYPE_V1 = (
    "moon",
    "mooning",
    "100x",
    "10x",
    "1000x",
    "gem",
    "hidden gem",
    "alpha leak",
    "this is the one",
    "do not miss",
    "don't sleep",
    "last chance",
    "huge upside",
    "easy money",
    "easy 10x",
    "easy 100x",
    "free money",
    "bullish af",
    "printing money",
    "printing gains",
    "lambo",
    "top pick",
    "best buy",
    "strong buy",
    "must buy",
    "floor is in",
    "breakout confirmed",
    "next leg up",
    # Removed `printing` bare (Vector-B C2 dual-use: "tx receipt printing",
    # "logger printing trace"). `printing money` / `printing gains` keep
    # the actionable-tone coverage.
    # Removed `winner` bare (Vector-B C1 false-positive on "category_winner"
    # / "no clear winner among comparable pools"). `top_pick`, `best buy`,
    # `strong buy`, `must buy` continue to cover the actionable-tone surface.
)

BANNED_TOKENS = tuple(BANNED_IMPERATIVES_V1) + tuple(BANNED_HYPE_V1)

# Fields whose string value is an identifier or controlled enum — exempt
# from the recursive banned-language scan. Everything else gets scanned.
SCAN_EXEMPT_STRING_FIELDS = frozenset({
    "generated_at",
    "verdict",
    "entry_quality",
    "chain",
    "symbol",
    "name",
    "token_id",
    "opened_at",
    "price_updated_at",
})

# Per-source / KOL ranking field-name firewall. Matched via re.search against
# lowercased key name (not fullmatch — Vector-A I2 + Vector-B I2 convergent:
# `recommended` etc. would slip if only fullmatch is used since
# `is_recommended` / `recommended_score` are stealth variants).
KOL_RANKING_FIELD_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"^kol_(rank|score|weight)$",
        r"^source_(rank|score|weight|priority)$",
        r"^channel_(rank|score|weight|trust)$",
        r"^tg_.*_(rank|score|weight)$",
        r"^x_.*_(rank|score|weight)$",
        r"^influencer_.*$",
        # Vector-B I1 additions: caller/poster/account/tweet vocabulary
        r"^caller_(rank|score|weight|authority|credibility|clout|list)$",
        r"^top_callers$",
        r"^poster_(rank|score|weight|reputation)$",
        r"^account_(credibility|reputation|trust)$",
        r"^tweet_(rank|score|credibility|weight)$",
        r"^user_(reputation|credibility)$",
        r"^narrative_call_(rank|score)$",
        # Vector-A I2 + Vector-B I2: widen `recommended` to match prefixed/suffixed variants
        r"^(is_)?recommend(ed|ation).*$",
        r".*_recommended$",
        # Closed-set markers for "top N picks" / ranks
        r"^top_pick.*$",
        r"^top_n$",
        r"^highest_(rank|score).*$",
        # Vector-B I1 extras: stealth weighting names
        r"^weighted_by_kol$",
        r"^kol_id_weighted$",
    )
)

DISCLAIMER_RE = re.compile(
    r"\bnot\s+(trading|investment|financial)\s+advice\b",
    re.IGNORECASE,
)

EXPECTED_TOP_LEVEL_KEYS = frozenset({"meta", "rows"})
EXPECTED_ROW_KEYS = frozenset({
    "disclaimer",
    "token_id",
    "symbol",
    "name",
    "chain",
    "open_trade_ids",
    "recent_trade_ids",
    "surfaces",
    "actionable",
    "would_be_live",
    "opened_at",
    "entry_price",
    "pct_from_entry",
    "current_price",
    "market_cap",
    "price_change_24h",
    "price_updated_at",
    "price_is_stale",
    "narrative_fit_score",
    "counter_risk_score",
    "counter_flags",
    "latest_chain_match",
    "entry_quality",
    "verdict",
    "inclusion_reasons",
    "risk_reasons",
})
ALLOWED_SEVERITIES = {"critical", "high", "medium", "low", "info"}


class Result:
    """Accumulator for contract checks."""

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
    """NFKC + strip Cf format chars + casefold + collapse whitespace runs.

    Defends against zero-width-space / zero-width-joiner / homoglyph bypass
    attempts on the banned-token scan. NFKC alone leaves U+200B etc.
    intact (they're category Cf), so strip those explicitly before casefold.
    """
    normalized = unicodedata.normalize("NFKC", value)
    stripped = "".join(
        ch for ch in normalized if unicodedata.category(ch) != "Cf"
    )
    folded = stripped.casefold()
    collapsed = re.sub(r"\s+", " ", folded)
    return collapsed


def _scan_string_for_banned(text: str, path: str, result: Result) -> None:
    """Scan a single string leaf for banned tokens; record CRITICAL on hit."""
    if not isinstance(text, str):
        return
    normalized = _normalize_text(text)
    for token in BANNED_TOKENS:
        if token in normalized:
            result.critical(
                f"banned-language: token {token!r} found in {path} "
                f"(normalized text: {normalized!r})"
            )


def _walk_strings(value, path: str, result: Result) -> None:
    """Recursively walk value; scan every string leaf except exempt fields.

    Per Vector-A I3: exemption applies to the FIELD regardless of value shape.
    If a future change makes an exempt field (e.g. `chain`) structured (a dict),
    we still skip the entire subtree — otherwise a chain name like "moonbeam"
    inside `chain.name` would trip the banned-token scan.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            child_path = f"{path}.{k}" if path else k
            if k in SCAN_EXEMPT_STRING_FIELDS:
                continue
            _walk_strings(v, child_path, result)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _walk_strings(item, f"{path}[{i}]", result)
    elif isinstance(value, str):
        _scan_string_for_banned(value, path, result)


def _parse_iso(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _check_meta(meta, requested_limit: int, requested_window: int,
                result: Result) -> None:
    if not isinstance(meta, dict):
        result.critical(f"meta must be object; got {type(meta).__name__}")
        return

    for flag in ("read_only", "experimental"):
        v = meta.get(flag)
        if v is not True:
            result.critical(
                f"meta.{flag} must be True (got {v!r}); "
                f"flip only via signed operator decision + coordinated "
                f"validator update"
            )

    nta = meta.get("not_trade_advice")
    if nta is not True:
        result.critical(
            f"meta.not_trade_advice must be True (got {nta!r}); "
            f"this flag has no promotion path — it stays True forever"
        )

    generated_at = meta.get("generated_at")
    dt = _parse_iso(generated_at)
    if dt is None:
        result.critical(
            f"meta.generated_at must be ISO8601 (got {generated_at!r})"
        )
    else:
        skew = abs((datetime.now(timezone.utc) - dt).total_seconds())
        if skew > 60:
            result.critical(
                f"meta.generated_at drift {skew:.1f}s exceeds 60s window"
            )
        else:
            result.ok()

    # Per Vector-A I1: isinstance(True, int) is True in Python; explicitly
    # exclude bool so `rows_returned=True` doesn't silently slip through.
    rows_returned = meta.get("rows_returned")
    if (
        not isinstance(rows_returned, int)
        or isinstance(rows_returned, bool)
        or rows_returned < 0
    ):
        result.critical(
            f"meta.rows_returned must be int>=0 (got {rows_returned!r})"
        )

    open_scanned = meta.get("open_trades_scanned")
    if (
        not isinstance(open_scanned, int)
        or isinstance(open_scanned, bool)
        or open_scanned < 0
    ):
        result.critical(
            f"meta.open_trades_scanned must be int>=0 (got {open_scanned!r})"
        )

    window = meta.get("window_hours")
    if window != requested_window:
        result.critical(
            f"meta.window_hours {window!r} != requested {requested_window}"
        )

    limit = meta.get("limit")
    if limit != requested_limit:
        result.critical(
            f"meta.limit {limit!r} != requested {requested_limit}"
        )


def _check_row(row, idx: int, result: Result) -> None:
    if not isinstance(row, dict):
        result.critical(f"rows[{idx}] must be object; got {type(row).__name__}")
        return

    # AC #14: KOL/source-ranking field firewall (regex on key name).
    # Uses re.search so the patterns can be either anchored (^...$ for exact
    # match) or unanchored (.*_recommended$ for suffix variants). Per Vector-A
    # I2 + Vector-B I2: fullmatch alone was too narrow for is_recommended /
    # recommended_score / *_recommended variants.
    for key in row.keys():
        if not isinstance(key, str):
            continue
        lower = key.casefold()
        for pattern in KOL_RANKING_FIELD_PATTERNS:
            if pattern.search(lower):
                result.critical(
                    f"rows[{idx}].{key}: per-source/KOL ranking field "
                    f"detected (matches {pattern.pattern!r}); operator's "
                    f"pinned safety stance forbids source ranking until "
                    f"price coverage becomes rankable"
                )

    # Per Vector-B C4: unknown row-level keys are CRITICAL, not WARNING.
    # A creatively-named field like `caller_authority_score` could slip past
    # the KOL regex set AND be silently demoted to WARNING — that's CRITICAL-
    # equivalent risk demoted. Closed-set drift IS the operator's pinned risk
    # model; new row keys require operator-signed schema bump + coordinated
    # validator update.
    unknown_row_keys = set(row.keys()) - EXPECTED_ROW_KEYS
    if unknown_row_keys:
        result.critical(
            f"rows[{idx}]: unknown row-level keys {sorted(unknown_row_keys)!r} "
            f"— closed-set drift requires operator-signed schema bump and "
            f"coordinated validator update before merge"
        )

    token_id = row.get("token_id")
    if not isinstance(token_id, str) or not token_id:
        result.critical(f"rows[{idx}].token_id must be non-empty str")

    disclaimer = row.get("disclaimer")
    if not isinstance(disclaimer, str) or len(disclaimer) < 20:
        result.critical(
            f"rows[{idx}].disclaimer must be non-empty str of length >= 20"
        )
    elif not DISCLAIMER_RE.search(disclaimer):
        result.critical(
            f"rows[{idx}].disclaimer must match "
            f"r'\\bnot\\s+(trading|investment|financial)\\s+advice\\b' "
            f"(got {disclaimer!r})"
        )

    verdict = row.get("verdict")
    if verdict not in ALLOWED_VERDICTS:
        result.critical(
            f"rows[{idx}].verdict {verdict!r} not in {sorted(ALLOWED_VERDICTS)!r}"
        )

    entry_quality = row.get("entry_quality")
    if entry_quality not in ALLOWED_ENTRY_QUALITIES:
        result.critical(
            f"rows[{idx}].entry_quality {entry_quality!r} not in "
            f"{sorted(ALLOWED_ENTRY_QUALITIES)!r}"
        )

    actionable = row.get("actionable")
    if actionable is not None and actionable not in (0, 1):
        result.critical(
            f"rows[{idx}].actionable must be in {{0, 1, None}} "
            f"(got {actionable!r})"
        )

    would_be_live = row.get("would_be_live")
    if would_be_live is not None and would_be_live not in (0, 1):
        result.critical(
            f"rows[{idx}].would_be_live must be in {{0, 1, None}} "
            f"(got {would_be_live!r})"
        )

    price_is_stale = row.get("price_is_stale")
    if not isinstance(price_is_stale, bool):
        result.critical(
            f"rows[{idx}].price_is_stale must be bool "
            f"(got {type(price_is_stale).__name__})"
        )

    for str_or_none_field in ("symbol", "name", "chain"):
        v = row.get(str_or_none_field)
        if v is not None and not isinstance(v, str):
            result.critical(
                f"rows[{idx}].{str_or_none_field} must be str|None "
                f"(got {type(v).__name__})"
            )

    for ts_field in ("opened_at", "price_updated_at"):
        v = row.get(ts_field)
        if v is not None and (not isinstance(v, str) or _parse_iso(v) is None):
            result.critical(
                f"rows[{idx}].{ts_field} {v!r} is not None and not ISO8601"
            )

    for numeric_field in (
        "entry_price", "pct_from_entry", "current_price",
        "market_cap", "price_change_24h",
    ):
        v = row.get(numeric_field)
        # isinstance(True, int) is True in Python, so explicitly exclude bool.
        # Vector-A N1: parenthesize for style consistency with the int_or_none loop.
        if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool)):
            result.critical(
                f"rows[{idx}].{numeric_field} must be number|None "
                f"(got {type(v).__name__})"
            )

    for int_or_none_field in ("narrative_fit_score", "counter_risk_score"):
        v = row.get(int_or_none_field)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool)):
            result.critical(
                f"rows[{idx}].{int_or_none_field} must be int|None "
                f"(got {type(v).__name__})"
            )

    for list_field in ("open_trade_ids", "recent_trade_ids"):
        v = row.get(list_field)
        if not isinstance(v, list) or any(
            not isinstance(x, int) or isinstance(x, bool) for x in v
        ):
            result.critical(
                f"rows[{idx}].{list_field} must be list[int] "
                f"(got {v!r})"
            )

    for str_list_field in ("surfaces", "inclusion_reasons", "risk_reasons"):
        v = row.get(str_list_field)
        if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
            result.critical(
                f"rows[{idx}].{str_list_field} must be list[str] "
                f"(got {v!r})"
            )

    counter_flags = row.get("counter_flags")
    if not isinstance(counter_flags, list):
        result.critical(
            f"rows[{idx}].counter_flags must be list (got {type(counter_flags).__name__})"
        )
    else:
        for j, item in enumerate(counter_flags):
            if not isinstance(item, (dict, str)):
                result.critical(
                    f"rows[{idx}].counter_flags[{j}] must be dict|str "
                    f"(got {type(item).__name__}); this is the #229 regression"
                )
            if isinstance(item, dict):
                sev = item.get("severity")
                if sev is not None and sev not in ALLOWED_SEVERITIES:
                    result.warn(
                        f"rows[{idx}].counter_flags[{j}].severity {sev!r} "
                        f"not in {sorted(ALLOWED_SEVERITIES)!r}"
                    )

    latest_chain_match = row.get("latest_chain_match")
    if latest_chain_match is not None and not isinstance(latest_chain_match, dict):
        result.critical(
            f"rows[{idx}].latest_chain_match must be dict|None "
            f"(got {type(latest_chain_match).__name__})"
        )

    # AC #13: candidate_review invariant
    if verdict == "candidate_review":
        if not (
            actionable == 1
            and would_be_live == 1
            and entry_quality in ("fresh_entry", "acceptable_pullback")
        ):
            result.critical(
                f"rows[{idx}] verdict=candidate_review violates invariant "
                f"(actionable={actionable!r}, would_be_live={would_be_live!r}, "
                f"entry_quality={entry_quality!r})"
            )

    # AC #12: data_insufficient invariant
    if verdict == "data_insufficient":
        risk_reasons = row.get("risk_reasons") or []
        eq = row.get("entry_quality")
        has_qualifying_reason = bool(
            set(risk_reasons) & DATA_INSUFFICIENT_RISK_REASONS
        ) or eq in ("too_stale", "data_insufficient")
        if not has_qualifying_reason:
            result.warn(
                f"rows[{idx}] verdict=data_insufficient but no qualifying "
                f"risk_reason / entry_quality found; schema drift?"
            )


def validate_payload(payload, *, requested_limit: int = 20,
                     requested_window: int = 36) -> Result:
    """Run all contract checks against a parsed-payload dict.

    Returns a Result accumulator; caller decides exit code.
    """
    result = Result()

    if not isinstance(payload, dict):
        result.critical(f"payload must be object; got {type(payload).__name__}")
        return result

    missing = EXPECTED_TOP_LEVEL_KEYS - set(payload.keys())
    if missing:
        result.critical(
            f"top-level missing required keys: {sorted(missing)!r}"
        )
        return result

    unknown_top = set(payload.keys()) - EXPECTED_TOP_LEVEL_KEYS
    if unknown_top:
        result.warn(
            f"top-level unknown keys (forward-compat): {sorted(unknown_top)!r}"
        )

    _check_meta(
        payload.get("meta"),
        requested_limit=requested_limit,
        requested_window=requested_window,
        result=result,
    )

    rows = payload.get("rows")
    if not isinstance(rows, list):
        result.critical(f"rows must be list (got {type(rows).__name__})")
        return result

    meta = payload.get("meta") or {}
    declared_rows_returned = meta.get("rows_returned")
    if isinstance(declared_rows_returned, int):
        if declared_rows_returned != len(rows):
            result.critical(
                f"meta.rows_returned {declared_rows_returned} != "
                f"len(rows) {len(rows)}"
            )

    for i, row in enumerate(rows):
        _check_row(row, i, result)

    # Recursive banned-language scan on the entire payload.
    # AC #11: applies uniformly regardless of verdict.
    _walk_strings(payload, "", result)

    return result


def fetch_and_validate(url: str, *, timeout_sec: float, slo_ms: int,
                       limit: int, window_hours: int) -> tuple[Result, int]:
    """Fetch endpoint + run validate_payload; return (result, exit_code)."""
    target = f"{url.rstrip('/')}/api/live_candidates?limit={limit}&window_hours={window_hours}"
    started = time.monotonic()
    try:
        with urllib.request.urlopen(target, timeout=timeout_sec) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.HTTPError as e:
        # Per Vector-A C1: record the http error so --json mode shows a
        # critical instead of an empty array with exit_code=2.
        r = Result()
        r.critical(f"http error {e.code}: {e.reason} (url={target})")
        return r, EXIT_HTTP
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        r = Result()
        r.critical(f"http fetch failed: {e}")
        return r, EXIT_HTTP
    latency_ms = (time.monotonic() - started) * 1000

    if status != 200:
        r = Result()
        r.critical(f"http status {status} != 200")
        return r, EXIT_HTTP

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        r = Result()
        r.critical(f"json parse failed: {e}")
        return r, EXIT_JSON

    result = validate_payload(
        payload, requested_limit=limit, requested_window=window_hours
    )

    if latency_ms > slo_ms:
        result.warn(
            f"response latency {latency_ms:.0f}ms exceeds provisional SLO "
            f"{slo_ms}ms"
        )

    exit_code = EXIT_OK if result.is_clean else EXIT_CRITICAL
    return result, exit_code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Runtime contract + smoke validator for /api/live_candidates",
    )
    parser.add_argument("--url", default="http://localhost:8000",
                        help="Base URL of the dashboard (no trailing slash)")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--window-hours", type=int, default=36)
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    parser.add_argument(
        "--slo-ms", type=int, default=3000,
        help=("Provisional latency budget; first 10 prod samples set the "
              "P95-based threshold before CI promotion"),
    )
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON summary")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-check detail")
    args = parser.parse_args(argv)

    if args.limit < 1 or args.limit > 50:
        print("--limit must be in [1, 50]", file=sys.stderr)
        return EXIT_CONFIG
    if args.window_hours < 6 or args.window_hours > 72:
        print("--window-hours must be in [6, 72]", file=sys.stderr)
        return EXIT_CONFIG

    result, exit_code = fetch_and_validate(
        args.url,
        timeout_sec=args.timeout_sec,
        slo_ms=args.slo_ms,
        limit=args.limit,
        window_hours=args.window_hours,
    )

    if args.json:
        summary = {
            "status": "ok" if exit_code == EXIT_OK else "fail",
            "exit_code": exit_code,
            "criticals": result.criticals,
            "warnings": result.warnings,
            "url": args.url,
        }
        print(json.dumps(summary, indent=2))
    else:
        if result.criticals:
            print(f"CRITICAL ({len(result.criticals)}):", file=sys.stderr)
            for msg in result.criticals:
                print(f"  - {msg}", file=sys.stderr)
        if result.warnings:
            print(f"WARNING ({len(result.warnings)}):", file=sys.stderr)
            for msg in result.warnings:
                print(f"  - {msg}", file=sys.stderr)
        status = "OK" if exit_code == EXIT_OK else f"FAIL (exit {exit_code})"
        print(f"{status} — {len(result.criticals)} criticals, "
              f"{len(result.warnings)} warnings")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
