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

from scout.identity import CANONICAL_SEMANTICS
from scout.identity_recompute import (
    CREDIT_BEARING as RECOMPUTE_CREDIT_BEARING,
)

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


def _chains_evidence_is_trusted(row) -> bool:
    """Ruling C's effective rule for whether a chains lead may earn credit.

    ``legacy_prefix`` does NOT mean "bad". It means the row predates the tier
    column and its provenance needs resolving -- which the versioned
    recomputation does. Treating every legacy row as untrusted would drop 826 of
    1188 production rows and take tier_high from 341 to 187, mostly as a
    metadata artefact rather than a change in detection quality.

        native canonical_v1                        -> trust
        legacy + recompute says verified canonical -> trust
        legacy + recompute says prefix-only        -> refuse (the ~4% target)
        legacy + ambiguous / indeterminate         -> refuse, but as UNRESOLVED
                                                      rather than as disproven
        legacy + no recompute available yet        -> refuse

    The last two are refusals of the same shape and deliberately so: unverified
    provenance earns nothing. But they are not claims that the detection was
    fuzzy, and the recompute table records which is which so the distinction
    survives in the evidence even though it does not change the score.
    """
    if _row_get(row, "chains_identity_semantics") == CANONICAL_SEMANTICS:
        return True
    return _row_get(row, "chains_recompute_status") in RECOMPUTE_CREDIT_BEARING


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
        # Ruling C: prefix similarity must not determine EARLY-DETECTION WIN
        # CLAIMS, and `early_count` is exactly such a claim. A chains lead on a
        # `legacy_prefix` row was derived by prefix matching, so it earns no
        # conviction credit here -- a fabricated 6.07-day lead would otherwise
        # clear the 1440-minute gate outright and inflate the tier.
        #
        # Rows whose semantics is unknown (NULL, e.g. written by rolled-back
        # code) are treated as legacy: unverified provenance must not earn
        # credit. Other surfaces are unaffected -- their leads never came from
        # this predicate.
        # Counterpart to the note in scout/gainers/tracker.py::get_gainers_stats,
        # which deliberately does NOT filter: that reports the historical record
        # of what the system did, this computes a forward-looking score now.
        if surface == "chains" and not _chains_evidence_is_trusted(row):
            continue
        lead = _row_get(row, lead_col)
        if surface == "chains":
            # Score the lead that was VERIFIED, not the legacy one.
            #
            # For a recovered legacy row the recompute establishes a canonical
            # lead; `chains_lead_minutes` still holds the ORIGINAL,
            # prefix-derived value. Using the latter decouples the claim from
            # the number: a row verified at 7200 minutes but carrying a legacy
            # 100 was refused by this gate despite being exactly the row the
            # recomputation exists to recover. It also runs the other way -- a
            # legacy lead inflated by a fuzzy match would be scored on evidence
            # that was never verified.
            recomputed = _row_get(row, "chains_canonical_lead")
            if recomputed is not None:
                lead = recomputed
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
