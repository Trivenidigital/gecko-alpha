"""Pure classifier functions: funding flip + OI spike. No I/O."""

import math
from datetime import datetime

from scout.perp.schemas import Exchange, PerpAnomaly


def classify_funding_flip(
    *,
    prev_rate: float | None,
    new_rate: float,
    exchange: Exchange,
    symbol: str,
    ticker: str,
    observed_at: datetime,
    min_magnitude_pct: float,
) -> PerpAnomaly | None:
    """Fire when funding rate flips sign and |new_rate| >= threshold.

    Edge case: ``0.0 -> -0.0001`` IS treated as a flip (0.0 is classified
    as non-negative by ``>= 0``), so a rate leaving the exactly-zero state
    toward negative fires. This is deliberate: in practice funding is
    almost never exactly 0.0, and treating it as positive keeps the
    classifier's branch logic simple and symmetric.
    """
    if not math.isfinite(new_rate):
        return None
    if prev_rate is not None and not math.isfinite(prev_rate):
        return None
    if prev_rate is None:
        return None
    if (prev_rate >= 0) == (new_rate >= 0):
        return None
    magnitude = abs(new_rate) * 100.0  # rate is fractional; convert to pct
    if magnitude < min_magnitude_pct:
        return None
    return PerpAnomaly(
        exchange=exchange,
        symbol=symbol,
        ticker=ticker,
        kind="funding_flip",
        magnitude=magnitude,
        baseline=prev_rate,
        observed_at=observed_at,
    )


def classify_oi_spike(
    *,
    current_oi: float,
    baseline_oi: float | None,
    exchange: Exchange,
    symbol: str,
    ticker: str,
    observed_at: datetime,
    sample_count: int,
    min_samples: int,
    spike_ratio: float,
) -> PerpAnomaly | None:
    """Fire when current OI / baseline >= spike_ratio past warmup."""
    if baseline_oi is None or baseline_oi <= 0 or not math.isfinite(baseline_oi):
        return None
    if not math.isfinite(current_oi):
        return None
    if sample_count < min_samples:
        return None
    ratio = current_oi / baseline_oi
    if ratio < spike_ratio:
        return None
    return PerpAnomaly(
        exchange=exchange,
        symbol=symbol,
        ticker=ticker,
        kind="oi_spike",
        magnitude=ratio,
        baseline=baseline_oi,
        observed_at=observed_at,
    )
