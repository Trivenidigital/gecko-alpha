"""Pure prospective conviction scorer (no DB/IO).

Mirrors ``cross_surface``'s tier gates but scores by detection AGE
(``now - first_detection``) instead of lead-before-appearance, because a
prospective coin has no pump-time to be "early" relative to. A surface is
SUSTAINED when its age >= ``CONVICTION_EARLY_LEAD_MINUTES`` (counts toward
``early_count`` + tier); FRESH when ``0 <= age < threshold`` (emerging, counted
separately, NEVER toward tier). None/negative/NaN ages are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass

from scout.conviction.cross_surface import SURFACE_LEAD_COLUMNS


@dataclass(frozen=True)
class ProspectiveResult:
    early_count: int
    fresh_count: int
    tier: str
    contributing: tuple[str, ...]


def _tier(early_count: int, settings) -> str:
    # Mirrors cross_surface._tier (same validated, config-driven gates).
    high = getattr(settings, "CONVICTION_HIGH_TIER_MIN_SURFACES", 4)
    watch = getattr(settings, "CONVICTION_WATCH_TIER_MIN_SURFACES", 2)
    if early_count >= high:
        return "high"
    if early_count >= watch:
        return "watch"
    return "low"


def score_prospective(first_detection_ages: dict, settings) -> ProspectiveResult:
    """Score one coin's per-surface first-detection ages (minutes)."""
    early_lead = getattr(settings, "CONVICTION_EARLY_LEAD_MINUTES", 1440)
    contributing: list[str] = []
    fresh = 0
    for surface in SURFACE_LEAD_COLUMNS:  # deterministic SURFACE order
        age = first_detection_ages.get(surface)
        try:
            age_val = float(age)
        except (TypeError, ValueError):
            continue
        if age_val != age_val or age_val < 0:  # NaN or negative → ignore
            continue
        if age_val >= early_lead:
            contributing.append(surface)
        else:
            fresh += 1
    return ProspectiveResult(
        early_count=len(contributing),
        fresh_count=fresh,
        tier=_tier(len(contributing), settings),
        contributing=tuple(contributing),
    )
