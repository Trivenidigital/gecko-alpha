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
    "liquidity_source_at_entry",
    "liquidity_confidence_at_entry",
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
    # Post-fold review Vector A Important finding: must read the SAME key
    # list the actionability classifier reads, or the snapshot silently
    # records mcap_usd_at_entry=None for any signal_data shape that uses
    # the longer-tail keys. The most common case is tg_social, whose
    # dispatcher constructs signal_data with `mcap_at_sighting` (not
    # `mcap`). Reusing actionability._extract_mcap directly keeps the two
    # in lockstep — any future key added to the classifier propagates
    # automatically.
    from scout.trading.actionability import _extract_mcap as _actionability_extract_mcap

    return _actionability_extract_mcap(signal_data)


def _extract_liquidity(signal_data: dict[str, Any]) -> float | None:
    value = signal_data.get("liquidity_usd")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _read_enriched_liquidity(
    db, *, contract_address: str | None, chain: str | None
) -> tuple[float | None, str | None, str | None]:
    """Read at-entry liquidity from the candidates enrichment columns written
    by the DexScreener backfill cron (scripts/backfill_dexscreener_liquidity.py).

    The CG cohort never carries `liquidity_usd` in signal_data — CoinGecko's
    feed doesn't surface pool liquidity (scout/models.py hardcodes 0.0) — so
    this enrichment read is the only source for those trades.

    Returns (liquidity_usd, source, confidence):
      - no candidates row, or never visited by the writer → (None, None, None)
        (a genuine gap → counts toward entry_snapshot_complete=0)
      - writer visited, no DEX match → (None, source, 'dex_no_match') etc.
        (known-absent → NOT a gap)
      - writer found liquidity → (value, source, 'definite'/'multi_chain')
    """
    if not contract_address or not chain:
        return (None, None, None)
    cur = await db._conn.execute(
        "SELECT liquidity_usd_enriched, liquidity_enriched_source, "
        "liquidity_enriched_confidence FROM candidates "
        "WHERE LOWER(contract_address)=LOWER(?) AND chain=? "
        "LIMIT 1",
        (contract_address, chain),
    )
    row = await cur.fetchone()
    if row is None:
        return (None, None, None)
    raw_value, source, confidence = row[0], row[1], row[2]
    value: float | None
    try:
        value = float(raw_value) if raw_value is not None else None
    except (TypeError, ValueError):
        value = None
    return (value, source, confidence)


def _source_confluence_count(signal_combo: str | None) -> int | None:
    if not signal_combo:
        return None
    parts = [p.strip() for p in signal_combo.split("+") if p.strip()]
    return len(set(parts)) if parts else None


async def _read_first_seen_at(
    db, *, contract_address: str | None, chain: str | None
) -> str | None:
    # Post-fold Vector B Minor #3: unbounded read of `candidates.first_seen_at`
    # depends on the ingestion invariant that first_seen_at is monotonic
    # per (contract_address, chain). This is NOT strictly enforced today —
    # `_upsert_candidate` uses INSERT OR REPLACE and the model default is
    # `now()` per construction (see BL-NEW-ACTIONABILITY-CANDIDATES-FIRST-
    # SEEN-PRESERVE). For trades on tokens that have been re-ingested,
    # `token_age_days_at_entry` is a LOWER BOUND, not the earliest sighting.
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


async def _read_tg_channel_at_entry(db, *, token_id: str, opened_at: str) -> str | None:
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


_TRAIL_FIELDS_ALLOWED: frozenset[str] = frozenset({"trail_pct", "trail_pct_low_peak"})


async def _read_trail_at_entry(
    db, *, signal_type: str, field_name: str, opened_at: str
) -> float | None:
    """Vector B C2 fix: audit-replay against signal_params_audit with
    applied_at <= opened_at bound. Falls back to current signal_params
    ONLY when no audit row exists with applied_at <= opened_at — never
    when an audit row exists but its new_value is unparseable (Vector B
    I-B1 fix: unparseable-fallback would silently leak post-open
    signal_params, defeating the temporal bound)."""
    # Whitelist guard (Vector A Minor #3): only known trail fields allowed
    # in the f-string SQL site below.
    if field_name not in _TRAIL_FIELDS_ALLOWED:
        raise ValueError(
            f"_read_trail_at_entry: field_name={field_name!r} not in "
            f"{_TRAIL_FIELDS_ALLOWED}"
        )

    # Use COUNT-then-fetch so we distinguish "no audit history" (→ fallback
    # to seed) from "audit row exists but unparseable" (→ return None, no
    # fallback).
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_params_audit "
        "WHERE signal_type=? AND field_name=? AND applied_at <= ?",
        (signal_type, field_name, opened_at),
    )
    audit_count = (await cur.fetchone())[0]

    if audit_count > 0:
        # Audit history exists at-or-before opened_at; the at-entry value
        # MUST come from there. If the latest row is unparseable, return
        # None (the unparseable-fallback to current signal_params would
        # leak post-open recalibrations).
        cur = await db._conn.execute(
            "SELECT new_value FROM signal_params_audit "
            "WHERE signal_type=? AND field_name=? AND applied_at <= ? "
            "ORDER BY applied_at DESC LIMIT 1",
            (signal_type, field_name, opened_at),
        )
        row = await cur.fetchone()
        if row is None or row[0] is None:
            return None
        try:
            return float(row[0])
        except (TypeError, ValueError):
            log.warning(
                "entry_snapshot_trail_unparseable",
                signal_type=signal_type,
                field_name=field_name,
                opened_at=opened_at,
                raw=row[0],
            )
            return None

    # No audit history → seed-baseline still in effect; signal_params holds
    # the seed value, which provably equals the at-entry value because
    # nothing has changed since seed.
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
    candidate_contract_address = contract_address
    if signal_type == "tg_social":
        raw_contract = signal_data.get("contract_address")
        if isinstance(raw_contract, str) and raw_contract.strip():
            candidate_contract_address = raw_contract.strip()

    # Liquidity: prefer a value carried in signal_data (DEX-sourced trades
    # measure it at signal time); otherwise fall back to the candidates
    # enrichment columns (the CG cohort's only source). Provenance is recorded
    # so a NULL can be told apart: 'definite' value vs 'dex_no_match'
    # known-absent vs writer-never-ran gap (None confidence).
    liquidity = _extract_liquidity(signal_data)
    if liquidity is not None:
        liquidity_source: str | None = "signal_data"
        liquidity_confidence: str | None = "definite"
    else:
        (
            liquidity,
            liquidity_source,
            liquidity_confidence,
        ) = await _read_enriched_liquidity(
            db, contract_address=candidate_contract_address, chain=chain
        )
    first_seen = await _read_first_seen_at(
        db, contract_address=candidate_contract_address, chain=chain
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
        "liquidity_source_at_entry": liquidity_source,
        "liquidity_confidence_at_entry": liquidity_confidence,
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

    Post-fold Vector B Minor #2: any enrichment read (chain_matches /
    price_cache via paper._enrich_actionability_signal_data) runs at
    T_enrich, which is microseconds AFTER opened_at. The captured mcap
    reflects the value the CLASSIFIER saw, not the strictly-at-opened_at
    value. The two are coupled (snapshot ↔ classifier coherence), which
    is the intended I-B2 contract; if strict-at-opened_at is ever
    required, both readers would need a temporal-snapshot table.

    Post-fold Vector B Minor #1: if enrichment succeeds but the classifier
    subsequently raises, the snapshot still captures the enriched mcap
    while `actionability_reason_at_entry` is recorded as "v1_error".
    This is internally consistent (both facts true) and intentional.
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
    missing = []
    for k in applicable:
        if snapshot.get(k) is not None:
            continue
        # A NULL liquidity is only a real gap when we have no provenance at
        # all (writer never visited). When the enrichment writer DID run but
        # found no DEX pair, liquidity_confidence_at_entry is set (e.g.
        # 'dex_no_match') — that's a known-absent fact, not a missing field.
        if (
            k == "liquidity_usd_at_entry"
            and snapshot.get("liquidity_confidence_at_entry") is not None
        ):
            continue
        missing.append(k)
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
