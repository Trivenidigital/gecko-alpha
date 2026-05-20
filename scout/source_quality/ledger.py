"""Durable TG/X source-call outcome ledger.

This module is deliberately read/write-local to ``source_calls``. It does not
alter trade opening, alerting, or classifier behavior.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import aiosqlite
import structlog

log = structlog.get_logger(__name__)

FORWARD_FIELDS = (
    "forward_30m_pct",
    "forward_1h_pct",
    "forward_6h_pct",
    "forward_24h_pct",
)

WINDOWS: dict[str, tuple[timedelta, timedelta, int]] = {
    "forward_30m_pct": (timedelta(minutes=30), timedelta(minutes=45), 15 * 60),
    "forward_1h_pct": (timedelta(hours=1), timedelta(minutes=90), 30 * 60),
    "forward_6h_pct": (timedelta(hours=6), timedelta(hours=7), 60 * 60),
    "forward_24h_pct": (timedelta(hours=24), timedelta(hours=28), 60 * 60),
}


@dataclass(frozen=True)
class LagCheckResult:
    ok: bool
    threshold_minutes: int
    unledgered_tg: int
    unledgered_x: int


@dataclass(frozen=True)
class SourceQualityRow:
    source_type: str
    source_id: str
    raw_calls: int
    distinct_clusters: int
    eligible_distinct_clusters: int
    duplicate_rate: float
    coverage_rate: float
    unresolvable_rate: float
    avg_forward_30m_pct: float | None
    avg_strategy_pnl_usd: float | None
    rank_status: str
    missing_reason_counts: dict[str, int]
    per_horizon_eligible_counts: dict[str, int]


def parse_utc(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _normal_symbol(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lstrip("$").upper() or None


def _identity(row: Any) -> tuple[str, str]:
    token_id = _row_get(row, "token_id")
    if token_id:
        return str(token_id), "token_id"
    contract = _row_get(row, "contract_address")
    chain = _row_get(row, "chain")
    if contract:
        return f"{chain or ''}|{str(contract).lower()}", "contract"
    symbol = _normal_symbol(_row_get(row, "symbol"))
    if symbol:
        return symbol, "symbol"
    return str(_row_get(row, "source_event_id")), "source_event"


def _cluster_key(
    *, source_type: str, source_id: str, cluster_identity: str, call_ts: datetime
) -> str:
    day = call_ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
    raw = "|".join((source_type, source_id, cluster_identity, day))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _missing(field: str, reason: str) -> dict[str, str]:
    return {"field": field, "reason": reason}


def _status_from_missing(
    *,
    now: datetime,
    call_ts: datetime,
    price_rows: list[dict[str, Any]],
    values: dict[str, float | None],
    missing: list[dict[str, str]],
) -> str:
    if not price_rows:
        return "unresolvable"
    if any(
        m["reason"] == "pending_window" for m in missing if m["field"] in FORWARD_FIELDS
    ):
        return "pending"
    if all(values.get(field) is not None for field in FORWARD_FIELDS):
        return "complete"
    # Keep age in the signature explicit: "partial" means all required windows
    # are mature relative to now, with coverage holes still visible.
    _ = (now, call_ts)
    return "partial"


async def _fetch_snapshot_rows(
    conn: aiosqlite.Connection, token_id: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in ("gainers_snapshots", "losers_snapshots"):
        cur = await conn.execute(
            f"SELECT coin_id, price_at_snapshot, snapshot_at, '{table}' AS source "
            f"FROM {table} WHERE coin_id = ? AND price_at_snapshot IS NOT NULL",
            (token_id,),
        )
        for row in await cur.fetchall():
            snapshot_at = parse_utc(row["snapshot_at"])
            if snapshot_at is None:
                continue
            rows.append(
                {
                    "price": row["price_at_snapshot"],
                    "snapshot_at": snapshot_at,
                    "source": table,
                }
            )
    rows.sort(key=lambda r: r["snapshot_at"])
    return rows


def _compute_outcome(
    *,
    call_ts: datetime,
    now: datetime,
    price_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    missing: list[dict[str, str]] = []
    values: dict[str, float | None] = {field: None for field in FORWARD_FIELDS}
    snapshots: dict[str, str | None] = {
        field.replace("_pct", "_snapshot_at"): None for field in FORWARD_FIELDS
    }
    horizons: dict[str, int | None] = {
        field.replace("_pct", "_observed_horizon_sec"): None for field in FORWARD_FIELDS
    }

    at_or_before = [row for row in price_rows if row["snapshot_at"] <= call_ts]
    at_call = at_or_before[-1] if at_or_before else None
    price_at_call = at_call["price"] if at_call else None
    price_age_sec = (
        int((call_ts - at_call["snapshot_at"]).total_seconds()) if at_call else None
    )
    price_source = at_call["source"] if at_call else None
    price_snapshot_at = _iso(at_call["snapshot_at"]) if at_call else None

    for field, (start_delta, end_delta, max_age_sec) in WINDOWS.items():
        if now < call_ts + start_delta:
            missing.append(_missing(field, "pending_window"))
            continue
        if price_at_call is None:
            reason = "no_time_series" if not price_rows else "stale_at_call"
            missing.append(_missing(field, reason))
            continue
        if price_age_sec is None or price_age_sec > max_age_sec:
            missing.append(_missing(field, "stale_at_call"))
            continue
        window_start = call_ts + start_delta
        window_end = call_ts + end_delta
        candidates = [
            row
            for row in price_rows
            if window_start <= row["snapshot_at"] <= window_end
        ]
        if not candidates:
            missing.append(_missing(field, "sparse_forward_window"))
            continue
        chosen = candidates[0]
        values[field] = ((chosen["price"] - price_at_call) / price_at_call) * 100.0
        snapshot_key = field.replace("_pct", "_snapshot_at")
        horizon_key = field.replace("_pct", "_observed_horizon_sec")
        snapshots[snapshot_key] = _iso(chosen["snapshot_at"])
        horizons[horizon_key] = int((chosen["snapshot_at"] - call_ts).total_seconds())

    extrema_rows = [
        row
        for row in price_rows
        if call_ts <= row["snapshot_at"] <= call_ts + timedelta(hours=24)
    ]
    max_favorable = None
    max_adverse = None
    time_to_peak = None
    if price_at_call and extrema_rows:
        pct_rows = [
            (((row["price"] - price_at_call) / price_at_call) * 100.0, row)
            for row in extrema_rows
        ]
        max_pct, peak_row = max(pct_rows, key=lambda item: item[0])
        min_pct, _ = min(pct_rows, key=lambda item: item[0])
        max_favorable = max_pct
        max_adverse = min_pct
        time_to_peak = (peak_row["snapshot_at"] - call_ts).total_seconds() / 60.0

    status = _status_from_missing(
        now=now,
        call_ts=call_ts,
        price_rows=price_rows,
        values=values,
        missing=missing,
    )
    if status == "complete":
        missing = []

    return {
        "price_at_call": price_at_call,
        "price_at_call_snapshot_at": price_snapshot_at,
        "price_source": price_source,
        "price_age_sec": price_age_sec,
        **values,
        **snapshots,
        **horizons,
        "max_favorable_pct_24h": max_favorable,
        "max_adverse_pct_24h": max_adverse,
        "time_to_peak_min": time_to_peak,
        "outcome_status": status,
        "missing_fields": json.dumps(missing, separators=(",", ":")),
    }


async def backfill_source_calls(conn: aiosqlite.Connection) -> dict[str, int]:
    await conn.execute("PRAGMA foreign_keys=ON")
    inserted = 0
    updated = 0

    tg_rows = await _fetch_tg_rows(conn)
    x_rows = await _fetch_x_rows(conn)
    for row in [*tg_rows, *x_rows]:
        existed = await _exists(conn, row["source_type"], row["source_event_id"])
        payload = await _build_base_payload(conn, row)
        await _upsert_source_call(conn, payload)
        if existed:
            updated += 1
        else:
            inserted += 1

    await _recompute_duplicate_ranks(conn)
    await conn.commit()
    log.info(
        "source_calls_backfill_summary",
        inserted=inserted,
        updated=updated,
        tg_seen=len(tg_rows),
        x_seen=len(x_rows),
    )
    return {
        "inserted": inserted,
        "updated": updated,
        "tg_seen": len(tg_rows),
        "x_seen": len(x_rows),
    }


async def _fetch_tg_rows(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cur = await conn.execute("""
        SELECT
            'tg' AS source_type,
            s.source_channel_handle AS source_id,
            CAST(s.id AS TEXT) AS source_event_id,
            s.token_id,
            s.symbol,
            s.contract_address,
            s.chain,
            m.posted_at AS call_ts,
            s.created_at AS observed_at,
            s.resolution_state AS resolved_state,
            s.mcap_at_sighting AS mcap_at_call,
            s.paper_trade_id AS tg_paper_trade_id
        FROM tg_social_signals s
        JOIN tg_social_messages m ON m.id = s.message_pk
        """)
    return [dict(row) for row in await cur.fetchall()]


async def _fetch_x_rows(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cur = await conn.execute("""
        SELECT
            'x' AS source_type,
            tweet_author AS source_id,
            event_id AS source_event_id,
            resolved_coin_id AS token_id,
            extracted_cashtag AS symbol,
            extracted_ca AS contract_address,
            extracted_chain AS chain,
            tweet_ts AS call_ts,
            received_at AS observed_at,
            CASE WHEN resolved_coin_id IS NOT NULL THEN 'resolved' ELSE 'unresolved' END
                AS resolved_state,
            NULL AS mcap_at_call,
            NULL AS tg_paper_trade_id
        FROM narrative_alerts_inbound
        """)
    return [dict(row) for row in await cur.fetchall()]


async def _exists(conn: aiosqlite.Connection, source_type: str, event_id: str) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM source_calls WHERE source_type=? AND source_event_id=?",
        (source_type, event_id),
    )
    return await cur.fetchone() is not None


async def _build_base_payload(
    conn: aiosqlite.Connection, row: dict[str, Any]
) -> dict[str, Any]:
    call_ts = parse_utc(row["call_ts"])
    observed_at = parse_utc(row["observed_at"])
    if call_ts is None:
        raise ValueError("source call missing call_ts")
    source_id = row["source_id"] or "unknown"
    cluster_identity, cluster_kind = _identity(row)
    paper_trade_id = None
    linkage_method = "none"
    linkage_confidence = "none"
    candidate_count = 0
    conflict_count = 0
    if row["source_type"] == "tg" and row.get("tg_paper_trade_id") is not None:
        paper_trade_id = row["tg_paper_trade_id"]
        linkage_method = "direct_tg"
        linkage_confidence = "direct"
        candidate_count = 1
    elif row["source_type"] == "x":
        paper_trade_id, candidate_count = await _find_x_trade_link(conn, row)
        if paper_trade_id is not None:
            linkage_method = "heuristic_x"
            linkage_confidence = "heuristic"
            conflict_count = max(0, candidate_count - 1)

    call_kind = "unknown"
    if row.get("contract_address"):
        call_kind = "ca_call"
    elif row.get("symbol"):
        call_kind = "cashtag_only"

    return {
        "source_type": row["source_type"],
        "source_id": source_id,
        "source_event_id": row["source_event_id"],
        "token_id": row.get("token_id"),
        "symbol": _normal_symbol(row.get("symbol")),
        "contract_address": row.get("contract_address"),
        "chain": row.get("chain"),
        "call_ts": row["call_ts"],
        "observed_at": row.get("observed_at"),
        "ingest_delay_sec": (
            int((observed_at - call_ts).total_seconds()) if observed_at else None
        ),
        "call_kind": call_kind,
        "cluster_identity": cluster_identity,
        "cluster_identity_kind": cluster_kind,
        "duplicate_cluster_key": _cluster_key(
            source_type=row["source_type"],
            source_id=source_id,
            cluster_identity=cluster_identity,
            call_ts=call_ts,
        ),
        "duplicate_rank_in_cluster": 1,
        "resolved_state": row.get("resolved_state") or "unresolved",
        "mcap_at_call": row.get("mcap_at_call"),
        "linked_paper_trade_id": paper_trade_id,
        "linkage_candidate_count": candidate_count,
        "linkage_conflict_count": conflict_count,
        "linkage_method": linkage_method,
        "linkage_confidence": linkage_confidence,
        "outcome_status": "pending",
        "missing_fields": json.dumps(
            [_missing(field, "pending_window") for field in FORWARD_FIELDS],
            separators=(",", ":"),
        ),
    }


async def _find_x_trade_link(
    conn: aiosqlite.Connection, row: dict[str, Any]
) -> tuple[int | None, int]:
    token_id = row.get("token_id")
    observed_at = parse_utc(row.get("observed_at"))
    if not token_id or observed_at is None:
        return None, 0
    end = observed_at + timedelta(hours=1)
    cur = await conn.execute(
        "SELECT id, opened_at FROM paper_trades WHERE token_id=? ORDER BY id",
        (token_id,),
    )
    ids = []
    for trade in await cur.fetchall():
        opened_at = parse_utc(trade["opened_at"])
        if opened_at is not None and observed_at <= opened_at <= end:
            ids.append(trade["id"])
    return (ids[0] if ids else None), len(ids)


async def _upsert_source_call(
    conn: aiosqlite.Connection, payload: dict[str, Any]
) -> None:
    columns = list(payload)
    placeholders = ", ".join("?" for _ in columns)
    update_columns = [
        col for col in columns if col not in {"source_type", "source_event_id"}
    ]
    updates = ", ".join(f"{col}=excluded.{col}" for col in update_columns)
    await conn.execute(
        f"INSERT INTO source_calls ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(source_type, source_event_id) DO UPDATE SET {updates}, "
        "updated_at=datetime('now')",
        tuple(payload[col] for col in columns),
    )


async def _recompute_duplicate_ranks(conn: aiosqlite.Connection) -> None:
    cur = await conn.execute(
        "SELECT id, duplicate_cluster_key, call_ts FROM source_calls"
    )
    clusters: dict[str, list[Any]] = {}
    for row in await cur.fetchall():
        clusters.setdefault(row["duplicate_cluster_key"], []).append(row)
    for rows in clusters.values():
        ordered = sorted(
            rows,
            key=lambda row: (
                parse_utc(row["call_ts"]) or datetime.max.replace(tzinfo=timezone.utc),
                row["id"],
            ),
        )
        for rank, row in enumerate(ordered, start=1):
            await conn.execute(
                "UPDATE source_calls SET duplicate_rank_in_cluster=? WHERE id=?",
                (rank, row["id"]),
            )


async def refresh_source_call_outcomes(
    conn: aiosqlite.Connection, *, now: datetime | None = None
) -> dict[str, int]:
    now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cur = await conn.execute("SELECT id, token_id, call_ts FROM source_calls")
    rows = await cur.fetchall()
    updated = 0
    for row in rows:
        call_ts = parse_utc(row["call_ts"])
        token_id = row["token_id"]
        if call_ts is None:
            continue
        price_rows = await _fetch_snapshot_rows(conn, token_id) if token_id else []
        outcome = _compute_outcome(call_ts=call_ts, now=now_dt, price_rows=price_rows)
        await _update_outcome(conn, row["id"], outcome)
        updated += 1
    await conn.commit()
    return {"updated": updated}


async def _update_outcome(
    conn: aiosqlite.Connection, source_call_id: int, outcome: dict[str, Any]
) -> None:
    columns = list(outcome)
    set_clause = ", ".join(f"{col}=?" for col in columns)
    await conn.execute(
        f"UPDATE source_calls SET {set_clause}, updated_at=datetime('now') WHERE id=?",
        tuple(outcome[col] for col in columns) + (source_call_id,),
    )


async def compute_source_quality_summary(
    conn: aiosqlite.Connection,
    *,
    min_sample: int = 10,
    min_coverage_rate: float = 0.50,
    source_type: str | None = None,
) -> list[SourceQualityRow]:
    params: list[Any] = []
    where = ""
    if source_type is not None:
        where = "WHERE sc.source_type = ?"
        params.append(source_type)
    cur = await conn.execute(
        f"""
        SELECT sc.*, pt.pnl_usd AS strategy_pnl_usd
        FROM source_calls sc
        LEFT JOIN paper_trades pt ON pt.id = sc.linked_paper_trade_id
        {where}
        ORDER BY sc.source_type, sc.source_id
        """,
        params,
    )
    groups: dict[tuple[str, str], list[Any]] = {}
    for row in await cur.fetchall():
        groups.setdefault((row["source_type"], row["source_id"]), []).append(row)

    summaries: list[SourceQualityRow] = []
    for (stype, sid), rows in groups.items():
        raw_calls = len(rows)
        cluster_keys = {row["duplicate_cluster_key"] for row in rows}
        eligible_clusters = {
            row["duplicate_cluster_key"]
            for row in rows
            if row["duplicate_rank_in_cluster"] == 1
            and row["forward_30m_pct"] is not None
        }
        unresolvable = sum(1 for row in rows if row["outcome_status"] == "unresolvable")
        duplicate_rate = (
            0.0 if raw_calls == 0 else 1.0 - (len(cluster_keys) / raw_calls)
        )
        coverage_rate = (
            0.0 if not cluster_keys else len(eligible_clusters) / len(cluster_keys)
        )
        avg_forward = _avg(row["forward_30m_pct"] for row in rows)
        pnl_rows = [
            row["strategy_pnl_usd"]
            for row in rows
            if row["strategy_pnl_usd"] is not None
            and not (stype == "x" and row["linkage_conflict_count"] > 0)
        ]
        missing_counts: dict[str, int] = {}
        for row in rows:
            for item in _loads_missing(row["missing_fields"]):
                reason = str(item.get("reason", "unknown"))
                missing_counts[reason] = missing_counts.get(reason, 0) + 1
        horizon_counts = {
            field: sum(1 for row in rows if row[field] is not None)
            for field in FORWARD_FIELDS
        }
        if len(eligible_clusters) < min_sample:
            rank_status = "insufficient_sample"
        elif coverage_rate < min_coverage_rate:
            rank_status = "biased_low_coverage"
        else:
            rank_status = "rankable_resolvable_cg_board_cohort"
        summaries.append(
            SourceQualityRow(
                source_type=stype,
                source_id=sid,
                raw_calls=raw_calls,
                distinct_clusters=len(cluster_keys),
                eligible_distinct_clusters=len(eligible_clusters),
                duplicate_rate=duplicate_rate,
                coverage_rate=coverage_rate,
                unresolvable_rate=0.0 if raw_calls == 0 else unresolvable / raw_calls,
                avg_forward_30m_pct=avg_forward,
                avg_strategy_pnl_usd=_avg(pnl_rows),
                rank_status=rank_status,
                missing_reason_counts=missing_counts,
                per_horizon_eligible_counts=horizon_counts,
            )
        )
    return summaries


def _loads_missing(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _avg(values: Iterable[float | None]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


async def check_source_calls_lag(
    conn: aiosqlite.Connection,
    *,
    now: datetime | None = None,
    threshold_minutes: int = 30,
) -> LagCheckResult:
    now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now_dt - timedelta(minutes=threshold_minutes)

    tg_rows = await _fetch_tg_rows(conn)
    x_rows = await _fetch_x_rows(conn)
    unledgered_tg = await _count_unledgered(conn, tg_rows, cutoff)
    unledgered_x = await _count_unledgered(conn, x_rows, cutoff)
    return LagCheckResult(
        ok=unledgered_tg == 0 and unledgered_x == 0,
        threshold_minutes=threshold_minutes,
        unledgered_tg=unledgered_tg,
        unledgered_x=unledgered_x,
    )


async def _count_unledgered(
    conn: aiosqlite.Connection, rows: list[dict[str, Any]], cutoff: datetime
) -> int:
    total = 0
    for row in rows:
        observed_at = parse_utc(row.get("observed_at")) or parse_utc(row.get("call_ts"))
        if observed_at is None or observed_at > cutoff:
            continue
        if not await _exists(conn, row["source_type"], row["source_event_id"]):
            total += 1
    return total
