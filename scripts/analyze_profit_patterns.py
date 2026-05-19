"""Read-only profit-pattern segmentation for paper trades.

Usage:
    python scripts/analyze_profit_patterns.py --db scout.db
    python scripts/analyze_profit_patterns.py --db scout.db --since "2026-05-01 14:06:00"
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


UNKNOWN = "unknown"


def _parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
            return parsed
        except ValueError:
            pass
    return None


def _load_json(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _walk_values(data: Any, keys: set[str]) -> Iterable[Any]:
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in keys:
                yield value
            yield from _walk_values(value, keys)
    elif isinstance(data, list):
        for item in data:
            yield from _walk_values(item, keys)


def _first(data: dict[str, Any], names: Iterable[str]) -> Any:
    lowered = {name.lower() for name in names}
    for value in _walk_values(data, lowered):
        if value not in (None, ""):
            return value
    return None


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _norm_handle(value: Any) -> str:
    if value in (None, ""):
        return UNKNOWN
    text = str(value).strip()
    if not text:
        return UNKNOWN
    if text.startswith("$"):
        return UNKNOWN
    if text.startswith("@"):
        return text.lower()
    return f"@{text.lower()}"


def _bucket_money(value: float | None, cuts: list[tuple[float, str]], label: str) -> str:
    if value is None or value <= 0:
        return f"{label}:unknown"
    for limit, name in cuts:
        if value < limit:
            return f"{label}:{name}"
    return f"{label}:{cuts[-1][1]}+"


def _mcap_bucket(value: float | None) -> str:
    return _bucket_money(
        value,
        [
            (50_000, "<50k"),
            (100_000, "50-100k"),
            (250_000, "100-250k"),
            (500_000, "250-500k"),
            (1_000_000, "500k-1m"),
            (5_000_000, "1-5m"),
            (10_000_000, "5-10m"),
            (50_000_000, "10-50m"),
        ],
        "mcap",
    )


def _liq_bucket(value: float | None) -> str:
    return _bucket_money(
        value,
        [
            (10_000, "<10k"),
            (25_000, "10-25k"),
            (50_000, "25-50k"),
            (100_000, "50-100k"),
            (250_000, "100-250k"),
            (1_000_000, "250k-1m"),
        ],
        "liq",
    )


def _age_bucket(days: float | None) -> str:
    if days is None or days < 0:
        return "age:unknown"
    hours = days * 24.0
    if hours < 1:
        return "age:<1h"
    if hours < 6:
        return "age:1-6h"
    if hours < 24:
        return "age:6-24h"
    if days < 3:
        return "age:1-3d"
    if days < 7:
        return "age:3-7d"
    if days < 30:
        return "age:7-30d"
    return "age:30d+"


def _freshness_bucket(minutes: float | None) -> str:
    if minutes is None or minutes < 0:
        return "fresh:unknown"
    if minutes < 5:
        return "fresh:<5m"
    if minutes < 30:
        return "fresh:5-30m"
    if minutes < 120:
        return "fresh:30m-2h"
    if minutes < 1440:
        return "fresh:2-24h"
    return "fresh:24h+"


def _giveback_bucket(peak_pct: float | None, pnl_pct: float | None) -> str:
    if peak_pct is None or pnl_pct is None:
        return "giveback:unknown"
    giveback = max(0.0, peak_pct - pnl_pct)
    if peak_pct < 5:
        return "giveback:no_peak_<5"
    if giveback < 5:
        return "giveback:<5pp"
    if giveback < 15:
        return "giveback:5-15pp"
    if giveback < 30:
        return "giveback:15-30pp"
    if giveback < 60:
        return "giveback:30-60pp"
    return "giveback:60pp+"


def _combo_parts(signal_combo: Any, signal_type: str, signal_data: dict[str, Any]) -> list[str]:
    raw = signal_combo or _first(signal_data, ["signal_combo", "combo", "signals_fired"])
    parts: list[str] = []
    if isinstance(raw, list):
        parts = [str(x) for x in raw if x]
    elif raw:
        text = str(raw)
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    parts = [str(x) for x in parsed if x]
            except Exception:
                parts = []
        if not parts:
            parts = [p for p in re.split(r"[+,|;/\s]+", text) if p]
    if not parts:
        parts = [signal_type]
    return sorted({p.strip().lower() for p in parts if p.strip()})


def _table_columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in cur.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def _load_candidates(cur: sqlite3.Cursor) -> dict[str, sqlite3.Row]:
    if not _table_columns(cur, "candidates"):
        return {}
    rows = cur.execute("SELECT * FROM candidates").fetchall()
    by_key: dict[str, sqlite3.Row] = {}
    for row in rows:
        for key in (
            "contract_address",
            "ticker",
            "token_name",
            "coingecko_id",
            "coin_id",
            "token_id",
        ):
            value = row[key] if key in row.keys() else None
            if value:
                by_key[str(value).lower()] = row
    return by_key


def _load_tg(cur: sqlite3.Cursor) -> dict[int, sqlite3.Row]:
    cols = _table_columns(cur, "tg_social_signals")
    if "paper_trade_id" not in cols:
        return {}
    return {
        int(row["paper_trade_id"]): row
        for row in cur.execute(
            "SELECT * FROM tg_social_signals WHERE paper_trade_id IS NOT NULL"
        )
    }


def _load_tg_messages(cur: sqlite3.Cursor) -> dict[int, sqlite3.Row]:
    if not _table_columns(cur, "tg_social_messages"):
        return {}
    return {int(row["id"]): row for row in cur.execute("SELECT * FROM tg_social_messages")}


def _load_x_alerts(cur: sqlite3.Cursor) -> dict[str, list[sqlite3.Row]]:
    cols = _table_columns(cur, "narrative_alerts_inbound")
    if "resolved_coin_id" not in cols:
        return {}
    alerts: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in cur.execute(
        "SELECT * FROM narrative_alerts_inbound WHERE resolved_coin_id IS NOT NULL"
    ):
        alerts[str(row["resolved_coin_id"]).lower()].append(row)
    for rows in alerts.values():
        rows.sort(key=lambda r: _parse_time(r["tweet_ts"]) or dt.datetime.min)
    return alerts


def _nearest_x_alert(
    alerts: dict[str, list[sqlite3.Row]], token_id: str, opened_at: dt.datetime | None
) -> sqlite3.Row | None:
    rows = alerts.get(token_id.lower(), [])
    if not rows:
        return None
    if opened_at is None:
        return rows[-1]
    before = [
        row
        for row in rows
        if (_parse_time(row["tweet_ts"]) or dt.datetime.min) <= opened_at
    ]
    return before[-1] if before else rows[0]


@dataclass
class Trade:
    id: int
    pnl_usd: float
    pnl_pct: float | None
    dims: dict[str, str]


def _candidate_for_trade(
    candidates: dict[str, sqlite3.Row], row: sqlite3.Row, data: dict[str, Any]
) -> sqlite3.Row | None:
    keys = [
        row["token_id"],
        row["symbol"],
        row["name"],
        _first(data, ["contract_address", "address", "ca"]),
        _first(data, ["ticker", "symbol"]),
    ]
    for key in keys:
        if key and str(key).lower() in candidates:
            return candidates[str(key).lower()]
    return None


def _build_trades(cur: sqlite3.Cursor, since: str | None) -> tuple[list[Trade], dict[str, Any]]:
    candidates = _load_candidates(cur)
    tg_by_trade = _load_tg(cur)
    tg_messages = _load_tg_messages(cur)
    x_alerts = _load_x_alerts(cur)

    params: list[Any] = []
    where = "status LIKE 'closed_%' AND pnl_usd IS NOT NULL"
    if since:
        where += " AND datetime(opened_at) >= datetime(?)"
        params.append(since)

    rows = cur.execute(f"SELECT * FROM paper_trades WHERE {where}", params).fetchall()
    trades: list[Trade] = []
    coverage = defaultdict(int)
    for row in rows:
        data = _load_json(row["signal_data"])
        opened_at = _parse_time(row["opened_at"])
        cand = _candidate_for_trade(candidates, row, data)
        tg = tg_by_trade.get(int(row["id"]))
        tg_msg = tg_messages.get(int(tg["message_pk"])) if tg else None
        x_alert = _nearest_x_alert(x_alerts, str(row["token_id"]), opened_at)

        signal_type = str(row["signal_type"])
        parts = _combo_parts(row["signal_combo"], signal_type, data)
        combo = "+".join(parts)
        confluence = len(parts)
        locked_stack = _num(row["conviction_locked_stack"] if "conviction_locked_stack" in row.keys() else None)
        if locked_stack is not None:
            confluence = max(confluence, int(locked_stack))

        mcap = _num(_first(data, ["market_cap_usd", "market_cap", "mcap", "mcap_at_sighting", "alert_market_cap"]))
        liq = _num(_first(data, ["liquidity_usd", "liquidity", "liquidityUsd", "entry_liquidity_usd"]))
        age_days = _num(_first(data, ["token_age_days", "age_days", "age_in_days"]))

        if tg and mcap is None:
            mcap = _num(tg["mcap_at_sighting"] if "mcap_at_sighting" in tg.keys() else None)
        if cand:
            if mcap is None and "market_cap_usd" in cand.keys():
                mcap = _num(cand["market_cap_usd"])
            if liq is None and "liquidity_usd" in cand.keys():
                liq = _num(cand["liquidity_usd"])
            if age_days is None and "token_age_days" in cand.keys():
                age_days = _num(cand["token_age_days"])

        freshness_min = None
        first_seen = _parse_time(_first(data, ["first_seen_at", "detected_at", "created_at"]))
        if first_seen and opened_at:
            freshness_min = (opened_at - first_seen).total_seconds() / 60.0
        if freshness_min is None and tg_msg and opened_at:
            posted = _parse_time(tg_msg["posted_at"] if "posted_at" in tg_msg.keys() else None)
            if posted:
                freshness_min = (opened_at - posted).total_seconds() / 60.0
        if freshness_min is None and x_alert and opened_at:
            tweet_ts = _parse_time(x_alert["tweet_ts"])
            if tweet_ts:
                freshness_min = (opened_at - tweet_ts).total_seconds() / 60.0

        x_handle = _norm_handle(
            _first(data, ["tweet_author", "x_handle", "twitter_handle", "author", "source_handle"])
            or (x_alert["tweet_author"] if x_alert else None)
        )
        tg_channel = _norm_handle(
            _first(data, ["channel_handle", "source_channel_handle", "telegram_channel"])
            or (tg["source_channel_handle"] if tg else None)
        )

        peak_pct = _num(row["peak_pct"])
        pnl_pct = _num(row["pnl_pct"])

        dims = {
            "detected_by_combo": combo,
            "signal_type": signal_type,
            "x_handle": x_handle,
            "tg_channel": tg_channel,
            "mcap_bucket": _mcap_bucket(mcap),
            "liquidity_bucket": _liq_bucket(liq),
            "age_bucket": _age_bucket(age_days),
            "freshness_bucket": _freshness_bucket(freshness_min),
            "peak_giveback_bucket": _giveback_bucket(peak_pct, pnl_pct),
            "source_confluence_count": f"confluence:{confluence}",
        }
        for key, value in [
            ("mcap", mcap),
            ("liquidity", liq),
            ("age", age_days),
            ("freshness", freshness_min),
            ("x_handle", None if x_handle == UNKNOWN else x_handle),
            ("tg_channel", None if tg_channel == UNKNOWN else tg_channel),
            ("signal_combo", row["signal_combo"]),
        ]:
            if value not in (None, "", UNKNOWN) and (not isinstance(value, (int, float)) or value > 0):
                coverage[key] += 1

        trades.append(
            Trade(
                id=int(row["id"]),
                pnl_usd=float(row["pnl_usd"]),
                pnl_pct=pnl_pct,
                dims=dims,
            )
        )

    meta = {
        "closed_count": len(trades),
        "coverage": dict(coverage),
        "table_counts": {
            name: cur.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in [
                "paper_trades",
                "tg_social_signals",
                "tg_social_messages",
                "narrative_alerts_inbound",
                "candidates",
            ]
            if _table_columns(cur, name)
        },
    }
    return trades, meta


def _rows_for_price_source(
    cur: sqlite3.Cursor, table: str, coin_id: str, received_at: str
) -> list[tuple[dt.datetime, float]]:
    if not _table_columns(cur, table):
        return []
    specs = {
        "gainers_snapshots": ("snapshot_at", "price_at_snapshot"),
        "losers_snapshots": ("snapshot_at", "price_at_snapshot"),
        "volume_history_cg": ("recorded_at", "price"),
        "volume_spikes": ("detected_at", "price"),
        "momentum_7d": ("detected_at", "current_price"),
    }
    time_col, price_col = specs[table]
    rows = cur.execute(
        f"""SELECT {time_col} AS ts, {price_col} AS price
            FROM {table}
            WHERE coin_id = ?
              AND {price_col} IS NOT NULL
              AND datetime({time_col}) BETWEEN datetime(?, '-24 hours')
                                         AND datetime(?, '+24 hours')""",
        (coin_id, received_at, received_at),
    ).fetchall()
    parsed: list[tuple[dt.datetime, float]] = []
    for row in rows:
        ts = _parse_time(row["ts"])
        price = _num(row["price"])
        if ts and price and price > 0:
            parsed.append((ts, price))
    return parsed


def _price_at_alert(cur: sqlite3.Cursor, coin_id: str, received_at: str) -> float | None:
    received_dt = _parse_time(received_at)
    sources: list[tuple[dt.datetime, float]] = []
    for table in (
        "gainers_snapshots",
        "losers_snapshots",
        "volume_history_cg",
        "volume_spikes",
        "momentum_7d",
    ):
        sources.extend(_rows_for_price_source(cur, table, coin_id, received_at))
    if not sources:
        return None
    if received_dt is None:
        return sorted(sources, key=lambda item: item[0], reverse=True)[0][1]
    before = [(ts, price) for ts, price in sources if ts <= received_dt]
    if before:
        return sorted(before, key=lambda item: item[0], reverse=True)[0][1]
    return sorted(sources, key=lambda item: item[0])[0][1]


def _current_price(cur: sqlite3.Cursor, coin_id: str) -> float | None:
    if not _table_columns(cur, "price_cache"):
        return None
    row = cur.execute(
        "SELECT current_price FROM price_cache WHERE coin_id = ? AND current_price IS NOT NULL LIMIT 1",
        (coin_id,),
    ).fetchone()
    return _num(row["current_price"]) if row else None


def _resolve_x_coin_id(cur: sqlite3.Cursor, row: sqlite3.Row) -> tuple[str | None, str]:
    resolved = row["resolved_coin_id"] if "resolved_coin_id" in row.keys() else None
    if resolved:
        return str(resolved), "resolved_coin_id"
    return None, "unresolved_no_resolved_coin_id"


def _x_alert_outcomes(cur: sqlite3.Cursor, since: str | None) -> tuple[list[Trade], dict[str, Any]]:
    if not _table_columns(cur, "narrative_alerts_inbound"):
        return [], {"x_status_counts": {}}
    where = "1=1"
    params: list[Any] = []
    if since:
        where += " AND datetime(received_at) >= datetime(?)"
        params.append(since)
    rows = cur.execute(f"SELECT * FROM narrative_alerts_inbound WHERE {where}", params).fetchall()
    resolved_rows: list[tuple[sqlite3.Row, str, str]] = []
    for row in rows:
        coin_id, status = _resolve_x_coin_id(cur, row)
        if coin_id:
            resolved_rows.append((row, coin_id, status))

    coin_ids = sorted({coin_id for _row, coin_id, _status in resolved_rows})
    current_prices = _load_current_prices(cur, coin_ids)
    source_prices = _load_price_sources(cur, coin_ids)

    trades: list[Trade] = []
    status_counts: dict[str, int] = defaultdict(int)
    status_counts["unresolved_or_ambiguous"] = len(rows) - len(resolved_rows)
    for row, coin_id, status in resolved_rows:
        entry = _price_at_alert_from_sources(source_prices.get(coin_id, []), row["received_at"])
        current = current_prices.get(coin_id)
        if entry is None:
            status_counts["no_entry_price"] += 1
            continue
        if current is None:
            status_counts["no_current_price"] += 1
            continue
        gain_pct = ((current - entry) / entry) * 100.0
        pnl_usd = 300.0 * gain_pct / 100.0
        status_counts["priced"] += 1
        author = _norm_handle(row["tweet_author"])
        theme = str(row["narrative_theme"] or UNKNOWN)
        urgency = str(row["urgency_signal"] or UNKNOWN)
        trades.append(
            Trade(
                id=int(row["id"]),
                pnl_usd=pnl_usd,
                pnl_pct=gain_pct,
                dims={
                    "x_handle": author,
                    "narrative_theme": theme,
                    "urgency_signal": urgency,
                    "x_resolution_status": status,
                },
            )
        )
    return trades, {"x_status_counts": dict(status_counts), "x_alert_count": len(rows)}


def _sql_in_placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def _load_current_prices(cur: sqlite3.Cursor, coin_ids: list[str]) -> dict[str, float]:
    if not coin_ids or not _table_columns(cur, "price_cache"):
        return {}
    rows = cur.execute(
        f"""SELECT coin_id, current_price
            FROM price_cache
            WHERE coin_id IN ({_sql_in_placeholders(coin_ids)})
              AND current_price IS NOT NULL""",
        coin_ids,
    ).fetchall()
    out: dict[str, float] = {}
    for row in rows:
        price = _num(row["current_price"])
        if price and price > 0:
            out[str(row["coin_id"])] = price
    return out


def _load_price_sources(
    cur: sqlite3.Cursor, coin_ids: list[str]
) -> dict[str, list[tuple[dt.datetime, float]]]:
    if not coin_ids:
        return {}
    specs = {
        "gainers_snapshots": ("snapshot_at", "price_at_snapshot"),
        "losers_snapshots": ("snapshot_at", "price_at_snapshot"),
        "volume_history_cg": ("recorded_at", "price"),
        "volume_spikes": ("detected_at", "price"),
        "momentum_7d": ("detected_at", "current_price"),
    }
    out: dict[str, list[tuple[dt.datetime, float]]] = defaultdict(list)
    placeholders = _sql_in_placeholders(coin_ids)
    for table, (time_col, price_col) in specs.items():
        if not _table_columns(cur, table):
            continue
        rows = cur.execute(
            f"""SELECT coin_id, {time_col} AS ts, {price_col} AS price
                FROM {table}
                WHERE coin_id IN ({placeholders})
                  AND {price_col} IS NOT NULL""",
            coin_ids,
        ).fetchall()
        for row in rows:
            ts = _parse_time(row["ts"])
            price = _num(row["price"])
            if ts and price and price > 0:
                out[str(row["coin_id"])].append((ts, price))
    for values in out.values():
        values.sort(key=lambda item: item[0])
    return out


def _price_at_alert_from_sources(
    sources: list[tuple[dt.datetime, float]], received_at: str
) -> float | None:
    received_dt = _parse_time(received_at)
    if not sources:
        return None
    if received_dt is None:
        return sources[-1][1]
    lower = received_dt - dt.timedelta(hours=24)
    upper = received_dt + dt.timedelta(hours=24)
    window = [(ts, price) for ts, price in sources if lower <= ts <= upper]
    if not window:
        return None
    before = [(ts, price) for ts, price in window if ts <= received_dt]
    if before:
        return before[-1][1]
    return window[0][1]


def _stats(trades: list[Trade]) -> dict[str, Any]:
    n = len(trades)
    net = sum(t.pnl_usd for t in trades)
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    losses = sum(1 for t in trades if t.pnl_usd <= 0)
    return {
        "n": n,
        "net": net,
        "avg": net / n if n else 0.0,
        "win_pct": (wins / n * 100.0) if n else 0.0,
        "losses": losses,
    }


def _group(trades: list[Trade], keys: tuple[str, ...], min_n: int) -> list[tuple[str, dict[str, Any]]]:
    grouped: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        label = " | ".join(trade.dims[key] for key in keys)
        grouped[label].append(trade)
    rows = [(label, _stats(items)) for label, items in grouped.items() if len(items) >= min_n]
    rows.sort(key=lambda item: (item[1]["net"], item[1]["avg"]), reverse=True)
    return rows


def _fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def _fmt_row(label: str, stat: dict[str, Any]) -> str:
    return (
        f"| {label} | {stat['n']} | {_fmt_money(stat['net'])} | "
        f"{_fmt_money(stat['avg'])} | {stat['win_pct']:.1f}% |"
    )


def _print_table(title: str, rows: list[tuple[str, dict[str, Any]]], limit: int) -> None:
    print(f"\n### {title}\n")
    print("| Pattern | n | Net P&L | Avg P&L | Win rate |")
    print("|---|---:|---:|---:|---:|")
    for label, stat in rows[:limit]:
        print(_fmt_row(label, stat))


def _print_x_section(cur: sqlite3.Cursor, since: str | None, min_n: int, limit: int) -> None:
    x_trades, x_meta = _x_alert_outcomes(cur, since)
    overall = _stats(x_trades)
    print("\n## X alert outcomes ($300 notional, dashboard method)\n")
    print(f"- X alerts in window: {x_meta.get('x_alert_count', 0)}")
    print(f"- Priced X alerts: {overall['n']}")
    print(f"- Resolution/status counts: `{json.dumps(x_meta.get('x_status_counts', {}), sort_keys=True)}`")
    print(
        f"- Priced outcome: net {_fmt_money(overall['net'])}, avg {_fmt_money(overall['avg'])}, "
        f"win {overall['win_pct']:.1f}%"
    )
    for key in (("x_handle",), ("narrative_theme",), ("urgency_signal",), ("x_resolution_status",)):
        rows = _group(x_trades, key, min_n)
        _print_table("X " + " / ".join(key), rows, limit)


def _print_tg_section(cur: sqlite3.Cursor, since: str | None, min_n: int, limit: int) -> None:
    if not _table_columns(cur, "tg_social_signals"):
        return
    params: list[Any] = []
    where = "p.status LIKE 'closed_%' AND p.pnl_usd IS NOT NULL"
    if since:
        where += " AND datetime(p.opened_at) >= datetime(?)"
        params.append(since)
    rows = cur.execute(
        f"""SELECT s.source_channel_handle, s.resolution_state, p.id, p.pnl_usd, p.pnl_pct
            FROM tg_social_signals s
            JOIN paper_trades p ON p.id = s.paper_trade_id
            WHERE {where}""",
        params,
    ).fetchall()
    trades = [
        Trade(
            id=int(row["id"]),
            pnl_usd=float(row["pnl_usd"]),
            pnl_pct=_num(row["pnl_pct"]),
            dims={
                "tg_channel": _norm_handle(row["source_channel_handle"]),
                "tg_resolution_state": str(row["resolution_state"] or UNKNOWN),
            },
        )
        for row in rows
    ]
    status = _stats(trades)
    linked_total = cur.execute(
        "SELECT COUNT(*) FROM tg_social_signals WHERE paper_trade_id IS NOT NULL"
    ).fetchone()[0]
    print("\n## TG social closed-trade outcomes\n")
    print(f"- TG signals linked to any paper trade: {linked_total}")
    print(f"- Closed TG trades in window: {status['n']}")
    print(
        f"- Closed TG outcome: net {_fmt_money(status['net'])}, avg {_fmt_money(status['avg'])}, "
        f"win {status['win_pct']:.1f}%"
    )
    for key in (("tg_channel",), ("tg_resolution_state",), ("tg_channel", "tg_resolution_state")):
        rows = _group(trades, key, min_n)
        _print_table("TG " + " / ".join(key), rows, limit)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db")
    parser.add_argument("--since")
    parser.add_argument("--min-n", type=int, default=3)
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    trades, meta = _build_trades(cur, args.since)
    overall = _stats(trades)

    print("# Profit-pattern segmentation")
    print()
    print(f"- DB: `{args.db}`")
    print(f"- Since: `{args.since or 'all closed trades'}`")
    print(f"- Closed trades analyzed: {overall['n']}")
    print(
        f"- Overall: net {_fmt_money(overall['net'])}, avg {_fmt_money(overall['avg'])}, "
        f"win {overall['win_pct']:.1f}%"
    )
    print(f"- Source table counts: `{json.dumps(meta['table_counts'], sort_keys=True)}`")
    print(f"- Field coverage among analyzed trades: `{json.dumps(meta['coverage'], sort_keys=True)}`")

    dimensions = [
        ("detected_by_combo",),
        ("signal_type",),
        ("x_handle",),
        ("tg_channel",),
        ("mcap_bucket",),
        ("liquidity_bucket",),
        ("age_bucket",),
        ("freshness_bucket",),
        ("peak_giveback_bucket",),
        ("source_confluence_count",),
        ("signal_type", "mcap_bucket"),
        ("signal_type", "liquidity_bucket"),
        ("signal_type", "freshness_bucket"),
        ("signal_type", "peak_giveback_bucket"),
        ("signal_type", "source_confluence_count"),
        ("mcap_bucket", "liquidity_bucket"),
        ("signal_type", "mcap_bucket", "liquidity_bucket"),
        ("signal_type", "tg_channel"),
        ("signal_type", "x_handle"),
    ]

    profitable: list[tuple[str, dict[str, Any]]] = []
    junk: list[tuple[str, dict[str, Any]]] = []
    for keys in dimensions:
        grouped = _group(trades, keys, args.min_n)
        prefix = " + ".join(keys)
        for label, stat in grouped:
            tagged = (f"{prefix}: {label}", stat)
            if stat["net"] > 0 and stat["avg"] > 0:
                profitable.append(tagged)
            if stat["net"] < 0 and stat["avg"] < 0:
                junk.append(tagged)

    profitable.sort(key=lambda item: (item[1]["net"], item[1]["avg"]), reverse=True)
    junk.sort(key=lambda item: (item[1]["net"], item[1]["avg"]))

    _print_table(f"Top profitable patterns (min n={args.min_n})", profitable, args.limit)
    _print_table(f"Worst junk patterns (min n={args.min_n})", junk, args.limit)

    print("\n## Dimension breakdowns")
    for keys in dimensions[:10]:
        rows = _group(trades, keys, args.min_n)
        _print_table(" / ".join(keys), rows, args.limit)

    print("\n## Notes")
    print("- Unknown buckets are included; if a requested field is mostly unknown, treat that as a dashboard instrumentation gap, not a trading conclusion.")
    print("- Source confluence is derived from `signal_combo` parts, capped upward by `conviction_locked_stack` when present.")
    print("- Peak giveback is `max(0, peak_pct - pnl_pct)` in percentage points.")

    _print_x_section(cur, args.since, args.min_n, args.limit)
    _print_tg_section(cur, args.since, args.min_n, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
