"""Build daily and real-time Telegram alert messages."""

from __future__ import annotations

from scout.narrative.models import CategoryAcceleration, NarrativePrediction


def format_heating_alert(
    accel: CategoryAcceleration,
    predictions: list[NarrativePrediction],
    top_3_coins: str,
) -> str:
    """Format a real-time heating narrative Telegram alert."""
    lines = [
        f"Narrative Heating: {accel.name}",
        f"Acceleration: {accel.previous_velocity:+.1f}% -> {accel.current_velocity:+.1f}% (+{accel.acceleration:.1f}%)",
        f"Volume growth: +{accel.volume_growth_pct:.0f}% in 6h",
        "",
        "Top picks (haven't pumped yet):",
    ]
    scored = [p for p in predictions if not p.is_control]
    for i, p in enumerate(scored[:5], 1):
        lines.append(
            f"{i}. {p.symbol} (${p.market_cap_at_prediction/1e6:.0f}M, "
            f"${p.price_at_prediction}) — Fit: {p.narrative_fit_score}/100 [{p.confidence}]"
        )
        if p.reasoning:
            lines.append(f'   "{p.reasoning[:100]}"')

    lines.append(f"\nCategory leaders: {top_3_coins}")
    if predictions:
        lines.append(f"Market regime: {predictions[0].market_regime}")
    if accel.coin_count_change is not None and accel.coin_count_change < -5:
        lines.append(
            f"Warning: coin count dropped by {abs(accel.coin_count_change)} (survivorship risk)"
        )
    return "\n".join(lines)


def format_daily_digest(
    heating: list[str],
    cooling: list[str],
    picks_today: int,
    categories_today: int,
    yesterday_results: list[dict],
    hit_rate: float,
    reflection: str,
    changes: list[dict],
    true_alpha: float,
) -> str:
    """Format the daily Telegram digest."""
    lines = [
        "Narrative Rotation — Daily Digest",
        "",
        f"HEATING: {', '.join(heating) if heating else 'None'}",
        f"COOLING: {', '.join(cooling) if cooling else 'None'}",
        "",
        f"Today's picks: {picks_today} across {categories_today} categories",
    ]

    if yesterday_results:
        hit_count = sum(1 for r in yesterday_results if r.get("outcome_class") == "HIT")
        total = len(yesterday_results)
        lines.append(f"Yesterday's results: {hit_count}/{total} ({hit_rate:.0f}%)")
        for r in yesterday_results[:5]:
            change = r.get("outcome_48h_change_pct", 0) or 0
            sign = "+" if change >= 0 else ""
            lines.append(
                f"  {r.get('symbol', '?')}: {sign}{change:.1f}% "
                f"(picked at ${r.get('price_at_prediction', 0):.4f})"
            )

    lines.append(f"\nTrue alpha: {true_alpha:+.1f}pp vs random baseline")
    if reflection:
        lines.append(f'\nAgent insight: "{reflection[:200]}"')
    if changes:
        lines.append(f"Strategy changes: {len(changes)}")
        for c in changes[:3]:
            lines.append(
                f"  {c.get('key', '?')}: {c.get('new_value', '?')} — {c.get('reason', '')[:80]}"
            )
    else:
        lines.append("Strategy changes: None")

    return "\n".join(lines)
