#!/usr/bin/env python3
"""ALR-03 universe-contamination report (READ-ONLY).

Counts CLOSED paper_trades whose token_id matches the universe exclude
patterns (tokenized equities / ETFs, e.g. `spy-bstocks-tokenized-stock`,
`qcom-tokenized-stock`) and sums their realised PnL. This is the
VALIDATE-clause backfill count for ALR-03: it quantifies how much the pre-fix
paper ENGINE contaminated paper_trades and every downstream PnL surface while
only the send-layer alert filter was active.

REPORT ONLY — the DB is opened read-only (`mode=ro`); no rows are ever deleted
or modified. Removing the contaminated rows (if desired) is a separate,
explicitly-approved operation and is intentionally NOT done here.

The universe definition is shared with the engine gate and the send-layer
alert filter via scout.token_ids.match_universe_exclude against the same
ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS list (one universe definition).

Usage:
    uv run python scripts/universe_contamination_report.py --db scout.db
    uv run python scripts/universe_contamination_report.py \
        --db scout.db --patterns=-tokenized-,-wrapped-

(A --patterns value starting with '-' must use the --patterns=VALUE form, or
argparse reads the leading '-' as another flag.)

Windows note: imports only sqlite3 + scout.token_ids (no aiohttp), so it runs
on Windows without the OpenSSL Applink hazard. Settings/.env is consulted for
the pattern list only when --patterns is omitted.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

# scout.token_ids is import-light (no aiohttp) — safe on Windows.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scout.token_ids import match_universe_exclude  # noqa: E402


@dataclass(frozen=True)
class ContaminatedTrade:
    trade_id: int
    token_id: str
    signal_type: str
    status: str
    pnl_usd: float | None
    pattern: str


@dataclass
class ReportResult:
    patterns: list[str]
    total_closed: int
    contaminated: list[ContaminatedTrade]

    @property
    def count(self) -> int:
        return len(self.contaminated)

    @property
    def total_pnl_usd(self) -> float:
        return round(sum(t.pnl_usd or 0.0 for t in self.contaminated), 2)


def build_report(conn: sqlite3.Connection, patterns: list[str]) -> ReportResult:
    """Scan CLOSED paper_trades and collect universe-contaminated rows.

    A CLOSED trade is any row whose status begins with ``closed`` (the paper
    trader writes closed_tp / closed_sl / closed_expired / ... variants). Open
    trades are excluded — the VALIDATE clause counts realised contamination.
    Pattern matching is done in Python via the shared matcher (not SQL LIKE) so
    a future pattern containing a LIKE wildcard can never change the result.
    """
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT id, token_id, signal_type, status, pnl_usd "
        "FROM paper_trades WHERE status LIKE 'closed%'"
    )
    rows = cur.fetchall()
    contaminated: list[ContaminatedTrade] = []
    for r in rows:
        pattern = match_universe_exclude(patterns, r["token_id"] or "")
        if pattern is not None:
            contaminated.append(
                ContaminatedTrade(
                    trade_id=r["id"],
                    token_id=r["token_id"],
                    signal_type=r["signal_type"],
                    status=r["status"],
                    pnl_usd=r["pnl_usd"],
                    pattern=pattern,
                )
            )
    return ReportResult(
        patterns=list(patterns),
        total_closed=len(rows),
        contaminated=contaminated,
    )


def format_report(result: ReportResult) -> str:
    line = "=" * 64
    out = [
        line,
        "ALR-03 universe-contamination report (READ-ONLY)",
        line,
        f"exclude patterns   : {result.patterns}",
        f"closed trades      : {result.total_closed}",
        f"contaminated closed: {result.count}",
        f"contaminated PnL   : ${result.total_pnl_usd:,.2f}",
    ]
    if result.contaminated:
        out.append("-" * 64)
        out.append(f"{'id':>8}  {'pnl_usd':>12}  {'pattern':<14}  token_id")
        for t in result.contaminated:
            pnl = "n/a" if t.pnl_usd is None else f"{t.pnl_usd:,.2f}"
            out.append(f"{t.trade_id:>8}  {pnl:>12}  {t.pattern:<14}  {t.token_id}")
    out.append(line)
    out.append("REPORT ONLY - no rows were deleted or modified.")
    return "\n".join(out)


def _load_patterns(explicit: str | None) -> list[str]:
    """Resolve the exclude patterns (one universe definition).

    --patterns wins when given (keeps the report hermetic / offline-runnable).
    Otherwise load the operator's live value from Settings/.env; fall back to
    the config field default when Settings can't be constructed (e.g. run
    off-host without env). Never hardcodes the pattern string here.
    """
    if explicit is not None:
        return [p.strip() for p in explicit.split(",") if p.strip()]
    from scout.config import Settings

    try:
        return list(Settings().ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS)
    except Exception:
        return list(Settings.model_fields["ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS"].default)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ALR-03 read-only universe-contamination backfill count.",
    )
    parser.add_argument("--db", default="scout.db", help="path to sqlite DB")
    parser.add_argument(
        "--patterns",
        default=None,
        help=(
            "comma-separated exclude patterns (default: Settings/.env). "
            "Values starting with '-' need the --patterns=VALUE form."
        ),
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"error: db not found: {db_path}", file=sys.stderr)
        return 2

    patterns = _load_patterns(args.patterns)
    # Read-only URI connection: the report can never mutate prod data.
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        result = build_report(conn, patterns)
    finally:
        conn.close()
    print(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
