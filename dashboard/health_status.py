"""Pure derivation of a per-subsystem health status enum for /api/system/health.

This module is intentionally pure: stdlib + typing only, no I/O, no DB, no
``datetime.now()`` call inside the deriver. The caller injects ``now`` so the
derivation is deterministic and unit-testable with a fixed clock.

Enum (closed, 4-valued, operator decision D1 — see
``tasks/design_api_system_health_status_enum_2026_05_30.md``):

- ``down``    -- the subsystem table is UNREADABLE (genuine read error / missing
                 table). Signalled by ``_table_stats`` returning ``count == -1``.
                 This is the ONLY source of ``down``.
- ``unknown`` -- table readable but we cannot honestly assess freshness:
                 no SLO defined for it, OR the table is empty (count == 0), OR
                 ``latest`` is missing/unparseable while an SLO is defined.
                 With the SLO map shipping EMPTY, this is the common case today.
- ``degraded``-- SLO defined, table non-empty, ``latest`` parses, and the age
                 strictly exceeds the SLO.
- ``ok``      -- SLO defined, table non-empty, ``latest`` parses, age within the
                 SLO (boundary ``age == SLO`` resolves to ``ok``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Mapping, Optional

SubsystemStatus = Literal["ok", "degraded", "down", "unknown"]

# The read-error sentinel ``_table_stats`` returns for a genuinely unreadable
# table. Kept here (next to the only consumer of its meaning) as a named
# constant so the contract is explicit on both sides.
READ_ERROR_COUNT_SENTINEL = -1


def _parse_latest(latest: object) -> Optional[datetime]:
    """Parse a ``_table_stats`` ``latest`` value into a tz-aware UTC datetime.

    ``latest`` is the raw ``MAX(time_col)`` value from SQLite: an ISO-8601
    string when rows exist, else ``None``. Naive timestamps are normalized to
    UTC; a trailing ``Z`` is accepted. Returns ``None`` when the value is
    missing or cannot be parsed (the caller maps that to ``unknown`` when an SLO
    is defined). Mirrors the parse in ``get_source_calls_health``.
    """
    if not isinstance(latest, str):
        return None
    try:
        parsed = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def derive_subsystem_status(
    stats: Mapping,
    slo_minutes: Optional[int],
    now: datetime,
) -> SubsystemStatus:
    """Derive a single subsystem's status from its ``_table_stats`` dict.

    Args:
        stats: one table's ``{"count": int, "latest": str | None}``.
        slo_minutes: per-subsystem freshness budget in minutes, or ``None`` when
            no SLO is defined for the table (the empty-map default).
        now: injected current time (tz-aware UTC recommended). Never read from
            the wall clock inside this function.

    Returns:
        One of ``"ok"``, ``"degraded"``, ``"down"``, ``"unknown"``.
    """
    count = stats.get("count")

    # 1. Genuine read error / missing table -> down (the only source of down).
    if count == READ_ERROR_COUNT_SENTINEL:
        return "down"

    # 2. No SLO defined -> we do not guess freshness. (Common case today.)
    if slo_minutes is None:
        return "unknown"

    # 3. Empty present table -> cannot assess freshness with zero rows.
    if not isinstance(count, int) or count <= 0:
        return "unknown"

    # 4. SLO defined + non-empty: need a parseable latest to compute age.
    parsed = _parse_latest(stats.get("latest"))
    if parsed is None:
        return "unknown"

    age_minutes = (now - parsed).total_seconds() / 60.0

    # 5/6. Strictly-greater than SLO -> degraded; boundary (==) -> ok.
    if age_minutes > slo_minutes:
        return "degraded"
    return "ok"
