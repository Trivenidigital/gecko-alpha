#!/usr/bin/env python3
"""Freshness SLO check for trade_decision_events.

The table is only expected to advance for enabled paper-trading signals when
their source snapshot tables have fresh rows. If an enabled source has fresh
rows but no fresh decision events, the dispatcher instrumentation is likely
disconnected.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class SignalSource:
    table: str
    timestamp_col: str
    enabled_setting: str | None = None


SIGNAL_SOURCES = {
    "gainers_early": SignalSource("gainers_snapshots", "snapshot_at"),
    "losers_contrarian": SignalSource(
        "losers_snapshots",
        "snapshot_at",
        "PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED",
    ),
    "trending_catch": SignalSource(
        "trending_snapshots",
        "snapshot_at",
        "PAPER_SIGNAL_TRENDING_CATCH_ENABLED",
    ),
}

DEFAULT_SIGNALS = tuple(SIGNAL_SOURCES)


def _iso_cutoff(minutes: float) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(minutes=minutes))
        .replace(microsecond=0)
        .replace(tzinfo=None)
        .isoformat(sep=" ")
    )


def _parse_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    text = str(value).strip().casefold()
    if text in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def _load_flag_values(env_file: Path) -> dict[str, object]:
    values: dict[str, object] = {}
    if env_file.exists():
        values.update(dotenv_values(env_file))
    values.update(os.environ)
    return values


def _enabled_flags(env_file: Path) -> dict[str, bool]:
    values = _load_flag_values(env_file)
    return {
        "TRADING_ENABLED": _parse_bool(values.get("TRADING_ENABLED"), False),
        "PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED": _parse_bool(
            values.get("PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED"), True
        ),
        "PAPER_SIGNAL_TRENDING_CATCH_ENABLED": _parse_bool(
            values.get("PAPER_SIGNAL_TRENDING_CATCH_ENABLED"), True
        ),
    }


def _parse_signals(raw: str) -> tuple[list[str], list[str]]:
    signals = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [signal for signal in signals if signal not in SIGNAL_SOURCES]
    return signals, unknown


def check(
    db_path: Path,
    lookback_minutes: float,
    *,
    signals: list[str] | None = None,
    env_file: Path = Path(".env"),
) -> tuple[int, dict]:
    if not db_path.exists():
        return 4, {"ok": False, "status": "db_missing", "db": str(db_path)}

    checked_signals = signals or list(DEFAULT_SIGNALS)
    unknown = [signal for signal in checked_signals if signal not in SIGNAL_SOURCES]
    if unknown:
        return 5, {"ok": False, "status": "unknown_signal", "unknown_signals": unknown}

    flags = _enabled_flags(env_file)
    if not flags["TRADING_ENABLED"]:
        return 0, {
            "ok": True,
            "status": "trading_disabled",
            "lookback_minutes": lookback_minutes,
            "checked_signals": [],
            "skipped_disabled_signals": checked_signals,
            "flags": flags,
        }

    active_signals = []
    skipped_disabled = []
    for signal in checked_signals:
        setting = SIGNAL_SOURCES[signal].enabled_setting
        if setting and not flags[setting]:
            skipped_disabled.append(signal)
        else:
            active_signals.append(signal)

    cutoff = _iso_cutoff(lookback_minutes)
    per_signal: dict[str, dict[str, int]] = {}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        for signal in active_signals:
            source = SIGNAL_SOURCES[signal]
            source_count = conn.execute(
                f"""SELECT COUNT(*) AS n
                    FROM {source.table}
                    WHERE datetime({source.timestamp_col}) >= datetime(?)
                      AND coin_id NOT IN (
                          SELECT token_id FROM paper_trades
                          WHERE signal_type = ? AND status = 'open'
                      )""",
                (cutoff, signal),
            ).fetchone()["n"]
            decision_count = conn.execute(
                """SELECT COUNT(*) AS n
                   FROM trade_decision_events
                   WHERE signal_type = ?
                     AND datetime(created_at) >= datetime(?)""",
                (signal, cutoff),
            ).fetchone()["n"]
            per_signal[signal] = {
                "recent_source_rows": int(source_count),
                "recent_decisions": int(decision_count),
            }
    except sqlite3.Error as exc:
        return 3, {"ok": False, "status": "sqlite_error", "error": str(exc)}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    body = {
        "ok": True,
        "status": "ok",
        "lookback_minutes": lookback_minutes,
        "checked_signals": active_signals,
        "skipped_disabled_signals": skipped_disabled,
        "signals": per_signal,
    }
    if not active_signals:
        body["status"] = "all_requested_signals_disabled"
        return 0, body

    if all(v["recent_source_rows"] == 0 for v in per_signal.values()):
        body["status"] = "idle_no_recent_source_rows"
        return 0, body

    missing = [
        signal
        for signal, counts in per_signal.items()
        if counts["recent_source_rows"] > 0 and counts["recent_decisions"] == 0
    ]
    if missing:
        body["ok"] = False
        body["status"] = "missing_recent_decisions"
        body["missing_signals"] = missing
        return 2, body
    return 0, body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db", type=Path)
    parser.add_argument("--lookback-minutes", default=15.0, type=float)
    parser.add_argument("--signals", default=",".join(DEFAULT_SIGNALS))
    parser.add_argument("--env-file", default=Path(".env"), type=Path)
    args = parser.parse_args(argv)

    signals, unknown = _parse_signals(args.signals)
    if unknown:
        print(
            json.dumps(
                {"ok": False, "status": "unknown_signal", "unknown_signals": unknown},
                sort_keys=True,
            )
        )
        return 5

    code, body = check(
        args.db,
        args.lookback_minutes,
        signals=signals,
        env_file=args.env_file,
    )
    print(json.dumps(body, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
