"""Numeric coercion for upstream payloads and stored feature JSON."""

from __future__ import annotations

import math


def num(v) -> float | None:
    """Finite float from a number or numeric string; anything else is None.

    ``bool`` is rejected (it is an ``int``), as are NaN and infinities.
    """
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def as_int(v) -> int | None:
    x = num(v)
    return int(x) if x is not None else None


def finite(x) -> bool:
    """True for a real finite number (not a bool, not a numeric string)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
