from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActionabilityDecision:
    actionable: bool
    reason: str
    version: str = "v1"


def evaluate_actionability_v1(
    *,
    signal_type: str,
    signal_data: dict[str, Any],
    signal_combo: str | None,
    conviction_stack: int = 0,
) -> ActionabilityDecision:
    mcap = _extract_mcap(signal_data)
    if mcap is None or mcap <= 0:
        if signal_type == "chain_completed":
            return ActionabilityDecision(
                True, "v1_pass_chain_completed_mcap_unknown_exception"
            )
        return ActionabilityDecision(False, "v1_block_missing_mcap")

    confluence = max(
        _source_confluence_count(signal_combo or signal_type),
        int(conviction_stack or 0),
    )

    if signal_type in {"narrative_prediction", "chain_completed", "volume_spike"}:
        if 10_000_000 <= mcap < 50_000_000:
            return ActionabilityDecision(True, "v1_pass_core_signal_mcap_10_50m")
        if mcap >= 50_000_000:
            return ActionabilityDecision(True, "v1_pass_core_signal_mcap_50m_plus")
        return ActionabilityDecision(False, "v1_block_core_signal_mcap_below_10m")

    if signal_type == "gainers_early":
        if 5_000_000 <= mcap < 10_000_000:
            return ActionabilityDecision(False, "v1_block_gainers_early_mcap_5_10m")
        if confluence >= 3:
            return ActionabilityDecision(False, "v1_block_gainers_early_confluence_3")
        if mcap >= 50_000_000:
            return ActionabilityDecision(True, "v1_pass_gainers_early_mcap_50m_plus")
        if mcap >= 10_000_000:
            return ActionabilityDecision(
                False, "v1_block_gainers_early_mcap_10_50m_observe"
            )
        return ActionabilityDecision(False, "v1_block_gainers_early_not_50m_plus")

    if signal_type == "losers_contrarian":
        return ActionabilityDecision(False, "v1_block_losers_contrarian_exploratory")
    if signal_type == "trending_catch":
        return ActionabilityDecision(False, "v1_block_trending_catch_low_n")
    if signal_type == "tg_social":
        return ActionabilityDecision(False, "v1_block_tg_social_low_n")

    return ActionabilityDecision(False, "v1_block_unknown_signal_type")


def _extract_mcap(signal_data: dict[str, Any]) -> float | None:
    for key in (
        "mcap",
        "market_cap",
        "market_cap_usd",
        "mcap_at_sighting",
        "alert_market_cap",
    ):
        value = signal_data.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _source_confluence_count(signal_combo: str) -> int:
    import re

    parts = [part for part in re.split(r"[+,|;/\s]+", signal_combo) if part]
    return max(1, len(set(parts)))
