"""Cross-surface conviction scoring (BL-NEW-CROSS-SURFACE-CONVICTION-SCORE).

Read-only ranking primitive that turns the noisy gainers firehose into a
winner shortlist by counting how many independent detectors confirmed a coin
EARLY (>= CONVICTION_EARLY_LEAD_MINUTES before it crossed +20%/24h).
"""

from scout.conviction.cross_surface import (
    SURFACE_LEAD_COLUMNS,
    TIER_ORDER,
    ConvictionResult,
    cross_surface_conviction,
)

__all__ = [
    "SURFACE_LEAD_COLUMNS",
    "TIER_ORDER",
    "ConvictionResult",
    "cross_surface_conviction",
]
