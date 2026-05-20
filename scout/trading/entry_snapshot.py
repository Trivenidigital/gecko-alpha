"""BL-NEW-ACTIONABILITY-ENTRY-SNAPSHOT-FOUNDATION.

Point-in-time entry-fact stamping for paper_trades. See
tasks/design_actionability_entry_snapshot_foundation_2026_05_20.md
(PR #199) for the design + review-fold log.

No future-state leakage: reads only at-or-before-open state.
No historical mixing: version literal "v1" pins the live-stamp cohort;
any backfill must use a distinct version (e.g., "v1-backfill").
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

log = structlog.get_logger()

ENTRY_SNAPSHOT_VERSION = "v1"

OPTIONAL_FIELDS: tuple[str, ...] = (
    "mcap_usd_at_entry",
    "mcap_bucket_at_entry",
    "liquidity_usd_at_entry",
    "token_age_days_at_entry",
    "first_seen_at_at_entry",
    "detected_by_combo_at_entry",
    "source_confluence_count_at_entry",
    "tg_channel_at_entry",
    "trail_pct_at_entry",
    "trail_pct_low_peak_at_entry",
)


def _applicable_optional_fields(signal_type: str) -> tuple[str, ...]:
    """Return the subset of OPTIONAL_FIELDS that ARE expected to resolve for
    this signal_type. Fields outside this subset never count as 'missing'
    even when None — they're 'not applicable' for this signal_type.

    tg_channel_at_entry only applies to tg_social trades; for any other
    signal_type, a NULL tg_channel is correct, not missing."""
    if signal_type == "tg_social":
        return OPTIONAL_FIELDS
    return tuple(f for f in OPTIONAL_FIELDS if f != "tg_channel_at_entry")

COLUMN_ORDER: tuple[str, ...] = (
    "paper_trade_id",
    "entry_snapshot_version",
    "entry_snapshot_complete",
    "entry_snapshot_missing_fields",
    "captured_at",
    "signal_type",
    "mcap_usd_at_entry",
    "mcap_bucket_at_entry",
    "liquidity_usd_at_entry",
    "token_age_days_at_entry",
    "first_seen_at_at_entry",
    "detected_by_combo_at_entry",
    "source_confluence_count_at_entry",
    "tg_channel_at_entry",
    "actionability_version_at_entry",
    "actionability_reason_at_entry",
    "actionable_at_entry",
    "tp_pct_at_entry",
    "sl_pct_at_entry",
    "trail_pct_at_entry",
    "trail_pct_low_peak_at_entry",
)


# Mcap bands MUST mirror scout/trading/actionability.py:34-52. Duplicated here
# rather than imported to keep entry_snapshot self-contained for cohort joins
# without cross-module coupling.
def _mcap_bucket(mcap: float | None) -> str | None:
    if mcap is None or mcap <= 0:
        return None
    if mcap < 1_000_000:
        return "under_1m"
    if mcap < 5_000_000:
        return "1_5m"
    if mcap < 10_000_000:
        return "5_10m"
    if mcap < 50_000_000:
        return "10_50m"
    if mcap < 250_000_000:
        return "50_250m"
    return "above_250m"


def _extract_mcap(signal_data: dict[str, Any]) -> float | None:
    for key in ("mcap", "market_cap", "market_cap_usd"):
        value = signal_data.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _extract_liquidity(signal_data: dict[str, Any]) -> float | None:
    value = signal_data.get("liquidity_usd")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _source_confluence_count(signal_combo: str | None) -> int | None:
    if not signal_combo:
        return None
    parts = [p.strip() for p in signal_combo.split("+") if p.strip()]
    return len(set(parts)) if parts else None


async def _read_first_seen_at(
    db, *, contract_address: str | None, chain: str | None
) -> str | None:
    if not contract_address or not chain:
        return None
    cur = await db._conn.execute(
        "SELECT first_seen_at FROM candidates "
        "WHERE LOWER(contract_address)=LOWER(?) AND chain=? "
        "LIMIT 1",
        (contract_address, chain),
    )
    row = await cur.fetchone()
    return row[0] if row else None


def _token_age_days(opened_at: str, first_seen_at: str | None) -> float | None:
    if not first_seen_at:
        return None
    try:
        opened_dt = datetime.fromisoformat(opened_at)
        first_dt = datetime.fromisoformat(first_seen_at)
    except ValueError:
        return None
    if opened_dt.tzinfo is None:
        opened_dt = opened_dt.replace(tzinfo=timezone.utc)
    if first_dt.tzinfo is None:
        first_dt = first_dt.replace(tzinfo=timezone.utc)
    delta = opened_dt - first_dt
    return delta.total_seconds() / 86400.0


async def _read_tg_channel_at_entry(
    db, *, token_id: str, opened_at: str
) -> str | None:
    # Vector B C1 fix: temporal bound forbids post-open social-signal rows
    # from polluting the at-entry channel.
    cur = await db._conn.execute(
        "SELECT source_channel_handle FROM tg_social_signals "
        "WHERE token_id=? AND created_at <= ? "
        "ORDER BY created_at DESC LIMIT 1",
        (token_id, opened_at),
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def _read_trail_at_entry(
    db, *, signal_type: str, field_name: str, opened_at: str
) -> float | None:
    """Vector B C2 fix: audit-replay against signal_params_audit with
    applied_at <= opened_at bound. Falls back to current signal_params
    only when no audit history exists (seed-baseline case)."""
    cur = await db._conn.execute(
        "SELECT new_value FROM signal_params_audit "
        "WHERE signal_type=? AND field_name=? AND applied_at <= ? "
        "ORDER BY applied_at DESC LIMIT 1",
        (signal_type, field_name, opened_at),
    )
    row = await cur.fetchone()
    if row is not None and row[0] is not None:
        try:
            return float(row[0])
        except (TypeError, ValueError):
            pass
    cur = await db._conn.execute(
        f"SELECT {field_name} FROM signal_params WHERE signal_type=?",
        (signal_type,),
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


async def build_entry_snapshot(
    db,
    *,
    opened_at: str,
    signal_type: str,
    signal_data: dict[str, Any],
    signal_combo: str | None,
    tp_pct: float,
    sl_pct: float,
    actionable_value: int,
    actionability_reason: str | None,
    actionability_version: str | None,
    contract_address: str | None,
    chain: str | None,
    settings,
) -> dict[str, Any]:
    """Compute the snapshot payload.

    `opened_at` (ISO-8601 UTC) is the authoritative trade-open timestamp;
    queries against mutating tables (tg_social_signals, signal_params_audit)
    are bounded by `<= opened_at` so post-open rows cannot leak into the
    snapshot (Vector B C1 + C2 fixes).
    """
    mcap = _extract_mcap(signal_data)
    liquidity = _extract_liquidity(signal_data)
    first_seen = await _read_first_seen_at(
        db, contract_address=contract_address, chain=chain
    )
    token_age = _token_age_days(opened_at, first_seen)

    tg_channel = None
    if signal_type == "tg_social":
        # token_id is what tg_social_signals.token_id is keyed by; the trade
        # is opened under that same token_id, available via contract_address
        # for non-coingecko chains; for tg_social the canonical token_id is
        # the coingecko id passed in as contract_address by the caller.
        tg_channel = await _read_tg_channel_at_entry(
            db, token_id=contract_address or "", opened_at=opened_at
        )

    trail_pct = await _read_trail_at_entry(
        db, signal_type=signal_type, field_name="trail_pct", opened_at=opened_at
    )
    trail_pct_low_peak = await _read_trail_at_entry(
        db,
        signal_type=signal_type,
        field_name="trail_pct_low_peak",
        opened_at=opened_at,
    )

    return {
        "signal_type": signal_type,
        "mcap_usd_at_entry": mcap,
        "mcap_bucket_at_entry": _mcap_bucket(mcap),
        "liquidity_usd_at_entry": liquidity,
        "token_age_days_at_entry": token_age,
        "first_seen_at_at_entry": first_seen,
        "detected_by_combo_at_entry": signal_combo,
        "source_confluence_count_at_entry": _source_confluence_count(signal_combo),
        "tg_channel_at_entry": tg_channel,
        "actionability_version_at_entry": actionability_version,
        "actionability_reason_at_entry": actionability_reason,
        "actionable_at_entry": actionable_value,
        "tp_pct_at_entry": tp_pct,
        "sl_pct_at_entry": sl_pct,
        "trail_pct_at_entry": trail_pct,
        "trail_pct_low_peak_at_entry": trail_pct_low_peak,
    }


async def stamp_entry_snapshot(
    db,
    *,
    trade_id: int,
    opened_at: str,
    signal_type: str,
    signal_data: dict[str, Any],
    signal_combo: str | None,
    tp_pct: float,
    sl_pct: float,
    actionable_value: int,
    actionability_reason: str | None,
    actionability_version: str | None,
    contract_address: str | None,
    chain: str | None,
    settings,
) -> None:
    """INSERT a row into paper_trade_entry_snapshots.

    Plain INSERT (not INSERT OR IGNORE) — duplicate-PK is structurally
    unreachable in the normal hot-path; if it ever fires (test fixture
    replay, future backfill PR mistake), the PK collision raises into the
    outer try/except where it's logged. INSERT OR IGNORE would silently
    mask the error signal (Vector B I1).

    `captured_at` is writer-time and slightly after `opened_at`; analytics
    should use paper_trades.opened_at when bucketing by trade time
    (Vector B M2).
    """
    snapshot = await build_entry_snapshot(
        db,
        opened_at=opened_at,
        signal_type=signal_type,
        signal_data=signal_data,
        signal_combo=signal_combo,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        actionable_value=actionable_value,
        actionability_reason=actionability_reason,
        actionability_version=actionability_version,
        contract_address=contract_address,
        chain=chain,
        settings=settings,
    )

    applicable = _applicable_optional_fields(signal_type)
    missing = [k for k in applicable if snapshot.get(k) is None]
    snapshot["paper_trade_id"] = trade_id
    snapshot["entry_snapshot_version"] = ENTRY_SNAPSHOT_VERSION
    snapshot["captured_at"] = datetime.now(timezone.utc).isoformat()
    snapshot["entry_snapshot_missing_fields"] = json.dumps(missing)
    snapshot["entry_snapshot_complete"] = 1 if not missing else 0

    placeholders = ", ".join("?" for _ in COLUMN_ORDER)
    columns = ", ".join(COLUMN_ORDER)
    await db._conn.execute(
        f"INSERT INTO paper_trade_entry_snapshots ({columns}) VALUES ({placeholders})",
        tuple(snapshot[col] for col in COLUMN_ORDER),
    )
    await db._conn.commit()
