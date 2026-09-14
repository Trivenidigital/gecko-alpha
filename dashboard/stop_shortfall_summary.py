"""Bounded historical paper descriptives; never a trading input."""

import asyncio
import math
import statistics
import threading
import time
from collections import Counter
from datetime import datetime, timezone

import aiosqlite
import structlog

from dashboard.db import _ro_db
from dashboard.stop_shortfall import classify_stop_shortfall

WORK_SECONDS = 3.0
REQUEST_SECONDS = 5.0
MAX_ROWS = 50000
_TRADE_FIELDS = (
    "id",
    "status",
    "exit_provenance",
    "price_source",
    "closed_at",
    "entry_price",
    "exit_price",
    "quantity",
    "remaining_qty",
    "amount_usd",
    "realized_pnl_usd",
    "conviction_locked_at",
    "leg_1_filled_at",
    "leg_2_filled_at",
)
_SNAPSHOT_FIELDS = ("entry_snapshot_version", "sl_pct_at_entry")


class SummaryUnavailable(Exception):
    """Sanitized public failure category."""


def _bounded(column: str) -> tuple[str, str]:
    oversized = f"(typeof({column}) IN ('text','blob') AND length(CAST({column} AS BLOB)) > 4096)"
    return f"CASE WHEN {oversized} THEN NULL ELSE {column} END", oversized


async def _finish(task: asyncio.Task) -> None:
    """Await cleanup even through repeated request cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


async def _read(conn, check):
    for table, required in (
        ("paper_trades", set(_TRADE_FIELDS)),
        ("paper_trade_entry_snapshots", {"paper_trade_id", *_SNAPSHOT_FIELDS}),
        ("paper_migrations", {"name", "cutover_ts"}),
    ):
        check()
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        columns = await cursor.fetchall()
        if not required.issubset({row["name"] for row in columns}):
            raise SummaryUnavailable("evidence_schema_unavailable")
        if table == "paper_trade_entry_snapshots":
            if [r["name"] for r in columns if r["pk"]] != ["paper_trade_id"]:
                raise SummaryUnavailable("duplicate_snapshot_identity")
    value, oversized = _bounded("cutover_ts")
    cursor = await conn.execute(
        f"SELECT {value}, {oversized} FROM paper_migrations WHERE name=? LIMIT 2",
        ("price_provenance_v1",),
    )
    markers = await cursor.fetchall()
    if len(markers) != 1 or markers[0][1]:
        raise SummaryUnavailable("cutover_unavailable")
    cutover = markers[0][0]
    try:
        if (
            not isinstance(cutover, str)
            or datetime.fromisoformat(cutover.replace("Z", "+00:00")).tzinfo is None
        ):
            raise ValueError
    except (ValueError, OverflowError):
        raise SummaryUnavailable("cutover_unavailable") from None
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status='closed_sl'"
    )
    total = (await cursor.fetchone())[0]
    if total > MAX_ROWS:
        raise SummaryUnavailable("population_too_large")
    fields = [(f"p.{f}", f) for f in _TRADE_FIELDS] + [
        (f"s.{f}", f) for f in _SNAPSHOT_FIELDS
    ]
    projections, bounds = [], []
    for column, name in fields:
        value, oversized = _bounded(column)
        projections.append(f"{value} AS {name}")
        bounds.append(oversized)
    cursor = await conn.execute(
        f"SELECT {','.join(projections)}, ({' OR '.join(bounds)}) AS input_oversized "
        "FROM paper_trades p LEFT JOIN paper_trade_entry_snapshots s ON s.paper_trade_id=p.id "
        "WHERE p.status='closed_sl'"
    )
    states, reasons, values = Counter(), Counter(), []
    while batch := await cursor.fetchmany(128):
        for raw in batch:
            check()
            row = dict(raw)
            if row.pop("input_oversized"):
                raise SummaryUnavailable("input_bounds_exceeded")
            result = classify_stop_shortfall(row, cutover)
            state = result["state"]
            if state not in {"available", "modeled", "unavailable"}:
                raise SummaryUnavailable("invalid_aggregate")
            states[state] += 1
            if state == "available":
                values.append(result["shortfall_pp"])
            else:
                reasons[result["exclusion_reason"]] += 1
        check()
        await asyncio.sleep(0)
    await cursor.close()
    check()
    if sum(states.values()) != total:
        raise SummaryUnavailable("invalid_aggregate")
    try:
        mean = statistics.fmean(values) if values else None
        check()
        median = statistics.median(values) if values else None
        if any(v is not None and not math.isfinite(v) for v in (mean, median)):
            raise OverflowError
    except (OverflowError, ValueError):
        raise SummaryUnavailable("invalid_aggregate") from None
    check()
    return cutover, dict(
        state="available" if values else "no_eligible_rows" if total else "empty",
        total_stop_rows=total,
        eligible_rows=states["available"],
        modeled_rows=states["modeled"],
        unavailable_rows=states["unavailable"],
        exclusions_by_reason=dict(reasons),
        mean_shortfall_pp=mean,
        median_shortfall_pp=median,
    )


async def get_stop_shortfall_summary(db_path: str) -> dict:
    """Read one complete bounded snapshot or return explicit unavailability."""
    deadline, stopped = time.monotonic() + WORK_SECONDS, threading.Event()

    def expired():
        return stopped.is_set() or time.monotonic() >= deadline

    def check():
        if expired():
            raise SummaryUnavailable("query_timeout")

    meta = dict(
        ok=False,
        generated_at=datetime.now(timezone.utc).isoformat(),
        read_only=True,
        basis="historical_paper_experimental",
        scope="all_stored_closed_sl",
        independent_of_table_filters=True,
        cutover_ts=None,
        data_missing_reason=None,
    )
    data = dict(
        state="unavailable",
        total_stop_rows=None,
        eligible_rows=None,
        modeled_rows=None,
        unavailable_rows=None,
        exclusions_by_reason=None,
        mean_shortfall_pp=None,
        median_shortfall_pp=None,
    )
    try:
        async with asyncio.timeout(REQUEST_SECONDS):
            context = _ro_db(db_path)
            conn = await context.__aenter__()
            try:
                await conn.execute("PRAGMA query_only=ON")
                await conn.execute("PRAGMA busy_timeout=100")
                await conn.set_progress_handler(lambda: int(expired()), 1000)
                await conn.execute("BEGIN")
                cutover, result = await _read(conn, check)
                check()
            finally:
                # Interrupt BEFORE queued close; keep handler installed until SQL stops.
                stopped.set()

                async def close():
                    try:
                        await conn.interrupt()
                    finally:
                        await context.__aexit__(None, None, None)

                await _finish(asyncio.create_task(close()))
            if time.monotonic() >= deadline:
                raise SummaryUnavailable("query_timeout")
            data = result
            meta.update(ok=True, cutover_ts=cutover)
    except Exception as exc:
        if isinstance(exc, SummaryUnavailable):
            reason = str(exc)
        elif isinstance(exc, FileNotFoundError):
            reason = "database_unavailable"
        elif isinstance(exc, TimeoutError) or (
            isinstance(exc, aiosqlite.OperationalError) and "interrupted" in str(exc)
        ):
            reason = "query_timeout"
        elif isinstance(exc, aiosqlite.OperationalError) and str(exc).startswith(
            ("no such table:", "no such column:")
        ):
            reason = "evidence_schema_unavailable"
        else:
            reason = "query_failed"
        meta["data_missing_reason"] = reason
        structlog.get_logger().warning(
            "stop_shortfall_summary_unavailable",
            reason=reason,
            error_type=type(exc).__name__,
            exc_info=reason == "query_failed",
        )
    return dict(meta=meta, data=data)
