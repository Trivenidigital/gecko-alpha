"""Combo-key derivation for paper-trading signal aggregation.

Single derivation site per spec D20. Pair-capped (spec D2).
"""

from __future__ import annotations

import structlog

log = structlog.get_logger()


def build_combo_key(signal_type: str, signals: list[str] | None) -> str:
    """Build combo_key = signal_type + (at most 1) alphabetically-first extra signal.

    Extras beyond the first are dropped and logged for Sprint 2 analysis.
    Output is 'sorted(parts)' joined by '+', so keys are order-insensitive.

    Normalization: signal_type and each signal entry are stripped and
    lower-cased so that 'VOLUME_SPIKE' and 'volume_spike' produce the same key.
    Empty / whitespace-only entries in signals are silently dropped.
    """
    signal_type = signal_type.strip().lower()
    parts = {signal_type}
    dropped: list[str] = []
    if signals:
        extras = sorted(
            s.strip().lower()
            for s in signals
            if s and s.strip() and s.strip().lower() != signal_type
        )
        if extras:
            parts.add(extras[0])
            dropped = extras[1:]
    if dropped:
        log.info(
            "combo_key_truncated_signals",
            signal_type=signal_type,
            kept=sorted(parts - {signal_type})[0] if len(parts) > 1 else None,
            dropped=dropped,
        )
    return "+".join(sorted(parts))
