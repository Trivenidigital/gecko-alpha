#!/usr/bin/env python3
"""Idempotent live-smoke for the Today's Focus block-reason readable-translation path.

PR #307 deployed a client-side readable-translation layer
(`dashboard/frontend/todayFocusFacts.js`) that maps machine block-reason
values to factual labels (e.g., `NO_PRICE` -> "Price snapshot missing").
The unit-test fixtures cover every known machine value, but the
end-to-end live path is only exercised when a production row has
`block_cause IS NOT NULL` and `block_reason_primary IS NOT NULL`.

At PR #307 deploy time (2026-05-28T17:01Z) the live cohort had zero
blocked rows, so the readable-translation path was not live-verified.

This script is idempotent. Run it whenever:
- After a deploy that touches the helpers or contract checker
- On a schedule (cron) to catch the first blocked row that lands
- Manually when a blocked row is observed in the dashboard

Reports one of:
- PASS — at least one blocked row was rendered with a known-mapped reason
- PASS_WITH_UNMAPPED — a blocked row exists but reason is not in the
  helper's REASON_LABELS, so deny-by-default ("Unmapped reason") was
  the live path. This is the safety net working as designed; not a
  failure, but it does flag a new machine value that should be added
  to the translation table for friendlier copy next time.
- DEFERRED — no blocked rows in the queried window; smoke cannot
  exercise the live path until one appears.

Exit codes:
- 0: PASS, PASS_WITH_UNMAPPED, or DEFERRED (no live path to verify)
- 2: a blocked row's value contains a banned token (real failure)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Iterable


# Mirrors REASON_LABELS keys in dashboard/frontend/todayFocusFacts.js.
# Keep this list in sync; if it drifts, this script will overstate
# PASS_WITH_UNMAPPED cases (false-positive flags, not silent failures).
KNOWN_REASON_KEYS = frozenset({
    "NO_PRICE",
    "STALE_PRICE",
    "NOT_ACTIONABLE",
    "BAD_TIMESTAMP",
    "DATA_INSUFFICIENT",
    "tracker_only_no_paper_trade",
    "detected_price_missing_or_invalid",
    "price_timestamp_unparseable",
    "entry_price_missing_or_invalid",
    "no_price_snapshot_for_token_id",
})

# Subset of BANNED_PATTERNS used as a last-line factual scan. Full list
# lives in scripts/check_todays_focus_contract.py; we use the most
# severe subset here for fast smoke.
BANNED_SUBSTRINGS = (
    "trade now",
    "act now",
    "action required",
    "watch breakout",
    "entry is late",
    "strong buy",
    "must buy",
    "take profit",
)


def _fetch(url: str, window_hours: int, timeout: float) -> dict:
    full = f"{url.rstrip('/')}/api/todays_focus?window_hours={window_hours}"
    req = urllib.request.Request(full, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _scan_value(text: str) -> list[str]:
    found = []
    lowered = text.lower()
    for needle in BANNED_SUBSTRINGS:
        if needle in lowered:
            found.append(needle)
    return found


def _blocked_rows(rows: Iterable[dict]) -> list[dict]:
    return [r for r in rows if r.get("block_cause")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000",
        help="Dashboard base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=36,
        help="Today's Focus window (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON status object on stdout",
    )
    args = parser.parse_args()

    try:
        payload = _fetch(args.url, args.window_hours, args.timeout)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        msg = f"ERROR: cannot fetch /api/todays_focus: {exc}"
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}))
        else:
            print(msg, file=sys.stderr)
        return 2

    rows = payload.get("rows", [])
    blocked = _blocked_rows(rows)

    if not blocked:
        result = {
            "status": "deferred",
            "reason": "no_blocked_rows_in_window",
            "focus_rows": len(rows),
            "blocked_rows": 0,
            "window_hours": args.window_hours,
        }
        if args.json:
            print(json.dumps(result))
        else:
            print(
                f"DEFERRED: {len(rows)} focus rows, 0 blocked. "
                f"Live translation path cannot be exercised yet. "
                f"Re-run when a blocked row appears."
            )
        return 0

    unmapped = []
    banned_hits = []
    mapped = []

    for row in blocked:
        reason = row.get("block_reason_primary")
        if reason and reason not in KNOWN_REASON_KEYS:
            unmapped.append({
                "symbol": row.get("symbol"),
                "token_id": row.get("token_id"),
                "block_cause": row.get("block_cause"),
                "block_reason_primary": reason,
            })
        else:
            mapped.append({
                "symbol": row.get("symbol"),
                "block_cause": row.get("block_cause"),
                "block_reason_primary": reason,
            })

        # Banned-substring scan against block-reason-bearing string fields.
        for field in ("block_cause", "block_reason_primary"):
            v = row.get(field)
            if isinstance(v, str):
                hits = _scan_value(v)
                if hits:
                    banned_hits.append({
                        "symbol": row.get("symbol"),
                        "field": field,
                        "value": v,
                        "matches": hits,
                    })

    if banned_hits:
        result = {
            "status": "fail",
            "reason": "banned_substring_in_block_field",
            "banned_hits": banned_hits,
        }
        if args.json:
            print(json.dumps(result))
        else:
            print(
                f"FAIL: {len(banned_hits)} blocked-row field(s) contain banned substring(s): "
                f"{banned_hits}"
            )
        return 2

    status = "pass_with_unmapped" if unmapped else "pass"
    result = {
        "status": status,
        "focus_rows": len(rows),
        "blocked_rows": len(blocked),
        "mapped_rows": len(mapped),
        "unmapped_rows": len(unmapped),
        "mapped": mapped,
        "unmapped": unmapped,
    }
    if args.json:
        print(json.dumps(result))
    else:
        if unmapped:
            print(
                f"PASS_WITH_UNMAPPED: {len(blocked)} blocked rows, "
                f"{len(mapped)} known-mapped, {len(unmapped)} fall to "
                f"deny-by-default 'Unmapped reason'. Consider adding the "
                f"following to REASON_LABELS for friendlier copy:"
            )
            for entry in unmapped:
                print(f"  {entry['symbol']} block_reason={entry['block_reason_primary']!r}")
        else:
            print(
                f"PASS: {len(blocked)} blocked row(s) verified — all reasons "
                f"map to factual REASON_LABELS entries."
            )
            for entry in mapped:
                print(
                    f"  {entry['symbol']} block_cause={entry['block_cause']!r} "
                    f"block_reason={entry['block_reason_primary']!r}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
