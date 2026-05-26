#!/usr/bin/env python3
"""Aggregate smoke validator for dashboard read-only contract firewalls."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_CRITICAL = 1
EXIT_HTTP = 2
EXIT_JSON = 3
EXIT_CONFIG = 4

_EXIT_PRIORITY = (EXIT_CRITICAL, EXIT_HTTP, EXIT_JSON, EXIT_CONFIG)


def _load_checker(module_name: str, filename: str):
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_LIVE_CHECKER = _load_checker("check_live_candidates_contract", "check_live_candidates_contract.py")
_TRADE_CHECKER = _load_checker("check_trade_inbox_contract", "check_trade_inbox_contract.py")


def _result_summary(result, exit_code: int) -> dict:
    criticals = list(getattr(result, "criticals", []) or [])
    warnings = list(getattr(result, "warnings", []) or [])
    return {
        "status": "ok" if exit_code == EXIT_OK else "fail",
        "exit_code": exit_code,
        "critical_count": len(criticals),
        "warning_count": len(warnings),
        "criticals": criticals,
        "warnings": warnings,
        "passed": getattr(result, "passed", 0),
    }


def _aggregate_exit_code(exit_codes: list[int]) -> int:
    if all(code == EXIT_OK for code in exit_codes):
        return EXIT_OK
    for code in _EXIT_PRIORITY:
        if code in exit_codes:
            return code
    return EXIT_CRITICAL


def _print_text(summary: dict, *, verbose: bool) -> None:
    exit_code = summary["exit_code"]
    if exit_code == EXIT_OK:
        print("OK: dashboard contracts clean (live_candidates=0, trade_inbox=0)")
        if not verbose:
            return
    else:
        print(f"FAIL: dashboard contract smoke failed (exit {exit_code})")
        if not verbose:
            return

    if not verbose:
        return

    for check_name, check in summary["checks"].items():
        for msg in check["criticals"]:
            print(f"{check_name} CRITICAL: {msg}")
        for msg in check["warnings"]:
            print(f"{check_name} WARNING: {msg}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate runtime contract smoke validator for dashboard endpoints",
    )
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--live-limit", type=int, default=20)
    parser.add_argument("--trade-limit-per-group", type=int, default=10)
    parser.add_argument("--window-hours", type=int, default=36)
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.live_limit < 1 or args.live_limit > 50:
        print("--live-limit must be in [1, 50]", file=sys.stderr)
        return EXIT_CONFIG
    if args.trade_limit_per_group < 1 or args.trade_limit_per_group > 100:
        print("--trade-limit-per-group must be in [1, 100]", file=sys.stderr)
        return EXIT_CONFIG
    if args.window_hours < 6 or args.window_hours > 72:
        print("--window-hours must be in [6, 72]", file=sys.stderr)
        return EXIT_CONFIG

    live_result, live_exit = _LIVE_CHECKER.fetch_and_validate(
        args.url,
        timeout_sec=args.timeout_sec,
        slo_ms=3000,
        limit=args.live_limit,
        window_hours=args.window_hours,
    )
    trade_result, trade_exit = _TRADE_CHECKER.fetch_and_validate(
        args.url,
        timeout_sec=args.timeout_sec,
        limit_per_group=args.trade_limit_per_group,
        window_hours=args.window_hours,
    )

    exit_code = _aggregate_exit_code([live_exit, trade_exit])
    summary = {
        "status": "ok" if exit_code == EXIT_OK else "fail",
        "exit_code": exit_code,
        "url": args.url,
        "checks": {
            "live_candidates": _result_summary(live_result, live_exit),
            "trade_inbox": _result_summary(trade_result, trade_exit),
        },
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_text(summary, verbose=args.verbose or exit_code != EXIT_OK)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
