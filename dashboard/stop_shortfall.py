"""Display-only historical paper price comparison; never a trading input."""

import math
from datetime import datetime, timezone


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _utc(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def classify_stop_shortfall(row, cutover, evidence_error=None):
    """Compare recorded paper exit prices with the frozen entry stop.

    Prices already include modeled paper slippage. This is neither execution
    slippage nor overshoot of the terminal stop. Never coerce absent evidence.
    """
    result = dict(state="unavailable", exclusion_reason=None, entry_stop_pct=None,
                  recorded_exit_return_pct=None, shortfall_pp=None,
                  basis="historical_paper_experimental")

    def excluded(reason, state="unavailable"):
        return result | {"state": state, "exclusion_reason": reason}

    if row.get("status") != "closed_sl":
        return excluded("not_stop_exit", "not_applicable")
    if row.get("exit_provenance") == "stop_gap_model":
        return excluded("modeled_exit", "modeled")
    if evidence_error:
        return excluded(evidence_error)
    if row.get("exit_provenance") != "market":
        return excluded("exit_not_market")
    source = row.get("price_source")
    if not isinstance(source, str) or not source.strip() or source.strip().lower() == "legacy":
        return excluded("price_source_unverified")
    if row.get("entry_snapshot_version") != "v1":
        return excluded("entry_snapshot_unverified")
    closed, boundary = _utc(row.get("closed_at")), _utc(cutover)
    if closed is None or boundary is None:
        return excluded("timestamp_unverified")
    if closed < boundary:
        return excluded("pre_cutover")
    fields = ("entry_price", "exit_price", "sl_pct_at_entry", "quantity",
              "remaining_qty", "amount_usd", "realized_pnl_usd")
    if not all(_finite(row.get(key)) for key in fields):
        return excluded("invalid_numeric_evidence")
    entry, exit_price, stop, qty, remaining, amount, realized = (row[k] for k in fields)
    if entry <= 0 or exit_price < 0 or not 0 < stop <= 100 or qty <= 0 or remaining < 0 or amount <= 0:
        return excluded("invalid_numeric_evidence")
    # Missing modification columns are not proof that no modification occurred.
    if any(key not in row for key in ("conviction_locked_at", "leg_1_filled_at", "leg_2_filled_at")):
        return excluded("invalid_numeric_evidence")
    if row["conviction_locked_at"] is not None:
        return excluded("conviction_modified")
    if (row["leg_1_filled_at"] is not None or row["leg_2_filled_at"] is not None
            or not math.isclose(remaining, qty, rel_tol=1e-9, abs_tol=0) or realized != 0):
        return excluded("partial_exit")
    notional = qty * entry
    if not _finite(notional) or not math.isclose(notional, amount, rel_tol=1e-9, abs_tol=0):
        return excluded("inconsistent_notional")
    recorded_return = 100 * (exit_price / entry - 1)
    shortfall = max(0.0, -recorded_return - stop)
    if not _finite(recorded_return) or not _finite(shortfall):
        return excluded("invalid_numeric_evidence")
    return result | dict(state="available", entry_stop_pct=stop,
                         recorded_exit_return_pct=recorded_return, shortfall_pp=shortfall)


async def enrich_stop_shortfalls(db, rows):
    """Enrich only this history page; keep rows on schema/query failures."""
    if not rows:
        return
    import aiosqlite
    import structlog

    error, evidence, cutover = None, {}, None
    try:
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        cursor = await db.execute(
            f"""SELECT p.*, s.entry_snapshot_version, s.sl_pct_at_entry
                FROM paper_trades p LEFT JOIN paper_trade_entry_snapshots s
                ON s.paper_trade_id = p.id WHERE p.id IN ({placeholders})""", ids)
        evidence = {row["id"]: dict(row) for row in await cursor.fetchall()}
        cursor = await db.execute(
            "SELECT cutover_ts FROM paper_migrations WHERE name = ?", ("price_provenance_v1",))
        marker = await cursor.fetchone()
        cutover = marker[0] if marker else None
    except Exception as exc:
        # Isolate enrichment from history's legacy catch-all. Unknown failures
        # remain visible in both API state and logs instead of erasing rows.
        missing = isinstance(exc, aiosqlite.OperationalError) and str(exc).startswith(
            ("no such table:", "no such column:"))
        error = "evidence_schema_unavailable" if missing else "evidence_query_failed"
        if not missing:
            structlog.get_logger().warning("stop_shortfall_evidence_query_failed", error_type=type(exc).__name__)
    for row in rows:
        row["stop_shortfall"] = classify_stop_shortfall(
            evidence.get(row["id"], row), cutover, evidence_error=error)
