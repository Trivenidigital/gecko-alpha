"""Cross-surface conviction scorer (pure, no DB/IO).

Validated on srilu (full history 2026-04-15 → 2026-06-12, 723 tracked gainers,
see tasks/design_cross_surface_conviction_2026_06_12.md): the count of
independent detectors that confirmed a coin >= 24h BEFORE it crossed +20%/24h
is the dominant winner discriminator (≥4 early surfaces → ~21% 3x-rate vs ~1%
for ≤1), and it is PREDICTIVE not coincident (93% of winners' confirmations
fired ≥24h early). This module scores a single tracker row; ranking/surfacing
lives in the read-only /api/conviction/shortlist endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass

# The 8 independent detection surfaces tracked in gainers_comparisons, mapped to
# their lead-minutes column. Order is the tie-break order for `contributing`.
SURFACE_LEAD_COLUMNS: dict[str, str] = {
    "chains": "chains_lead_minutes",
    "pipeline": "pipeline_lead_minutes",
    "narrative": "narrative_lead_minutes",
    "spikes": "spikes_lead_minutes",
    "momentum": "momentum_lead_minutes",
    "slow_burn": "slow_burn_lead_minutes",
    "acceleration": "acceleration_lead_minutes",
    "velocity": "velocity_lead_minutes",
}

# Tier labels (ordered low → high) for filtering/comparison.
TIER_ORDER: tuple[str, ...] = ("low", "watch", "high")


@dataclass(frozen=True)
class ConvictionResult:
    """Conviction for one tracker row.

    ``early_count`` — surfaces that confirmed >= early-lead before appearance.
    ``score``       — Σ per-surface weight over the early-confirming surfaces.
    ``tier``        — ``high`` / ``watch`` / ``low`` from the count gates.
    ``contributing``— the early-confirming surface names (in SURFACE order).
    """

    early_count: int
    score: float
    tier: str
    contributing: tuple[str, ...]


def _row_get(row, key: str):
    """Safe accessor for dict OR sqlite3.Row (missing key → None, never raises)."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _surface_weight(surface: str, settings) -> float:
    """Per-surface weight. v1: equal 1.0 for every surface (the validated lever
    is the COUNT, not surface identity). A future ``CONVICTION_SURFACE_WEIGHTS``
    mapping can override; absent/invalid entries fall back to 1.0."""
    weights = getattr(settings, "CONVICTION_SURFACE_WEIGHTS", None)
    if isinstance(weights, dict):
        try:
            return float(weights[surface])
        except (KeyError, TypeError, ValueError):
            return 1.0
    return 1.0


def _tier(early_count: int, settings) -> str:
    high = getattr(settings, "CONVICTION_HIGH_TIER_MIN_SURFACES", 4)
    watch = getattr(settings, "CONVICTION_WATCH_TIER_MIN_SURFACES", 2)
    if early_count >= high:
        return "high"
    if early_count >= watch:
        return "watch"
    return "low"


def cross_surface_conviction(row, settings) -> ConvictionResult:
    """Score one gainers_comparisons-shaped row by early cross-surface confirmation.

    A surface counts when ``detected_by_<surface>`` is truthy AND its
    ``<surface>_lead_minutes`` is a finite number >= ``CONVICTION_EARLY_LEAD_MINUTES``
    (inclusive). Null/missing leads or columns degrade to "not early" — this
    function never raises, so it is safe over partially-populated rows.
    """
    early_lead = getattr(settings, "CONVICTION_EARLY_LEAD_MINUTES", 1440)
    contributing: list[str] = []
    score = 0.0
    for surface, lead_col in SURFACE_LEAD_COLUMNS.items():
        if not _row_get(row, f"detected_by_{surface}"):
            continue
        lead = _row_get(row, lead_col)
        try:
            lead_val = float(lead)
        except (TypeError, ValueError):
            continue
        # Guard against NaN (NaN >= x is False, but be explicit) + require early.
        if lead_val != lead_val or lead_val < early_lead:
            continue
        contributing.append(surface)
        score += _surface_weight(surface, settings)
    return ConvictionResult(
        early_count=len(contributing),
        score=round(score, 4),
        tier=_tier(len(contributing), settings),
        contributing=tuple(contributing),
    )
