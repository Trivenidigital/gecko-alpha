"""Backfill Minara command-emission rows from journalctl JSON lines."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from scout.db import Database


def _format_amount_for_key(amount: Any) -> str:
    if isinstance(amount, float) and amount.is_integer():
        return str(int(amount))
    return str(amount)


def parse_minara_emission_line(line: str) -> dict[str, Any] | None:
    """Parse one structlog JSON line for minara_alert_command_emitted."""
    raw = line.strip()
    if not raw:
        return None
    if "{" in raw:
        raw = raw[raw.index("{") :]
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if obj.get("event") != "minara_alert_command_emitted":
        return None
    coin_id = obj.get("coin_id")
    chain = obj.get("chain")
    amount_usd = obj.get("amount_usd")
    emitted_at = obj.get("timestamp")
    if not coin_id or not chain or amount_usd is None or not emitted_at:
        return None
    amount_key = _format_amount_for_key(amount_usd)
    source_event_id = obj.get("source_event_id")
    if not source_event_id:
        source_event_id = f"journalctl:{emitted_at}:{coin_id}:{chain}:{amount_key}"
    return {
        "coin_id": str(coin_id),
        "chain": str(chain),
        "amount_usd": amount_usd,
        "emitted_at": str(emitted_at),
        "source_event_id": str(source_event_id),
    }


async def backfill_file(db_path: Path, journal_path: Path, *, apply: bool) -> int:
    rows = []
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        parsed = parse_minara_emission_line(line)
        if parsed is not None:
            rows.append(parsed)
    if not apply:
        return len(rows)

    db = Database(db_path)
    await db.initialize()
    try:
        inserted = 0
        for row in rows:
            did_insert = await db.record_minara_alert_emission(
                paper_trade_id=None,
                tg_alert_log_id=None,
                signal_type="unknown_historical_backfill",
                coin_id=row["coin_id"],
                chain=row["chain"],
                amount_usd=row["amount_usd"],
                command_text=None,
                emitted_at=row["emitted_at"],
                source_event_id=row["source_event_id"],
                source="journalctl_backfill",
            )
            inserted += int(did_insert)
        return inserted
    finally:
        await db.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill minara_alert_emissions from journalctl JSON lines."
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    return parser


async def _main_async() -> int:
    args = _build_parser().parse_args()
    count = await backfill_file(args.db, args.journal, apply=args.apply)
    action = "inserted" if args.apply else "matched"
    print(f"{action}={count}")
    return 0


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
